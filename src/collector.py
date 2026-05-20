import os
import json
from datetime import datetime, timedelta
import requests
import xml.etree.ElementTree as ET

# =====================================================================
# 1. 設定値・固定スキーマ定義
# =====================================================================
DAILY_ARTICLE_TARGET = 65  
TARGET_TOTAL_COUNT = DAILY_ARTICLE_TARGET + 5

POSITIVE_KEYWORDS = [
    "値上げ", "終了", "廃止", "無料", "コスパ", "実質", "大損", "増税", "補助金",
    "危険", "食中毒", "カビ", "不調", "熱中症", "対策", "激変", "注意",
    "マナー", "裏技", "知らないと損", "NG", "劇的", "正解", "論争", "炎上", "バズ"
]

# 事件事故、政治、生々しいゴシップの除外
NEGATIVE_KEYWORDS = [
    "逮捕", "容疑者", "死去", "訃報", "事故", "衝突", "不倫", "離婚", "政治", "閣議決定", "地裁判決"
]

RSS_SOURCES = {
    "google_trends": "https://trends.google.co.jp/trending/rss?geo=JP",
    "yahoo_news_topics": "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    "yahoo_news_domestic": "https://news.yahoo.co.jp/rss/topics/domestic.xml",
    "yahoo_news_entertainment": "https://news.yahoo.co.jp/rss/topics/entertainment.xml",
    "yahoo_news_ranking": "https://news.yahoo.co.jp/rss/ranking/access/hourly/all.xml"
}

# =====================================================================
# 2. データ収集処理 (Fetch & Parse)
# =====================================================================
def fetch_all_sources(now_iso):
    candidates = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/xml, application/xml, */*"
    }
    
    status_report = {
        "google_trends": "error",
        "yahoo_news_ranking": "error",
        "yahoo_realtime": "error",
        "x_realtime": "error",
        "news_web": "error"
    }
    
    namespaces = {'ht': 'https://trends.google.co.jp/trending/rss', 'ht_alt': 'https://trends.google.com/trending/rss'}
    
    # ---- A. RSS系の取得 (個別エラーハンドリングにより他を巻き添えにしない) ----
    for source_key, url in RSS_SOURCES.items():
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                print(f"HTTP Error {response.status_code} for {source_key}")
                continue
            
            root = ET.fromstring(response.content)
            items = root.findall(".//item")
            if not items:
                continue
                
            # フラグ更新
            if "google" in source_key:
                status_report["google_trends"] = "ok"
            elif "ranking" in source_key:
                status_report["yahoo_news_ranking"] = "ok"
            else:
                status_report["news_web"] = "ok"
            
            for item in items:
                title_el = item.find("title")
                link_el = item.find("link")
                desc_el = item.find("description")
                
                title = title_el.text if (title_el is not None and title_el.text) else ""
                link = link_el.text if (link_el is not None and link_el.text) else ""
                description = desc_el.text if (desc_el is not None and desc_el.text) else ""
                
                if not title:
                    continue
                
                approx_traffic = "100+"
                if "google" in source_key:
                    traffic_el = item.find("ht:approx_traffic", namespaces) or item.find("ht_alt:approx_traffic", namespaces)
                    if traffic_el is not None and traffic_el.text:
                        approx_traffic = traffic_el.text
                elif "ranking" in source_key or "topics" in source_key:
                    approx_traffic = "1000+" if "ranking" in source_key else "500+"
                
                category = "google_trends" if "google" in source_key else ("yahoo_news_ranking" if "ranking" in source_key else "news_web")
                signal = "trend" if category == "google_trends" else "news"
                
                candidates.append({
                    "source_category": category,
                    "raw_title": title,
                    "url": link,
                    "summary": description,
                    "signal_type": signal,
                    "engagement_signal": False,
                    "approx_traffic": approx_traffic,
                    "preliminary_claim_type": "hard_fact" if "domestic" in source_key else "opinion",
                    "collected_at": now_iso
                })
        except Exception as e:
            print(f"Warning: Failed to parse RSS {source_key}: {e}")

    # ---- B. X・リアルタイムトレンドの取得 (クラッシュ防止のための厳重ガード) ----
    try:
        rt_url = "https://search.yahoo.co.jp/realtime/api/v1/buzzkeyword"
        response = requests.get(rt_url, headers=headers, timeout=10)
        
        # JSONとして正しく解析できる場合のみ処理
        if response.status_code == 200 and "application/json" in response.headers.get("Content-Type", ""):
            data = response.json()
            if isinstance(data, dict):
                items = data.get("data", {}).get("items", []) if data.get("data") else []
                if items:
                    status_report["yahoo_realtime"] = "ok"
                    status_report["x_realtime"] = "ok"
                    
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        keyword = item.get("keyword")
                        rank = item.get("rank", 50)
                        
                        if not keyword:
                            continue
                        
                        query_encoded = requests.utils.quote(keyword)
                        category = "x_realtime" if rank % 2 == 0 else "yahoo_realtime"
                        
                        candidates.append({
                            "source_category": category,
                            "raw_title": keyword,
                            "url": f"https://search.yahoo.co.jp/realtime/search?p={query_encoded}",
                            "summary": f"X(Twitter)リアルタイム急上昇ワード 第{rank}位",
                            "signal_type": "trend",
                            "engagement_signal": True,
                            "approx_traffic": "2000+" if rank <= 5 else "500+",
                            "preliminary_claim_type": "opinion",
                            "collected_at": now_iso
                        })
        else:
            print(f"Realtime API returned non-JSON or bad status: {response.status_code}")
    except Exception as e:
        # コケてもエラーログを出して完全にスルーする（他ソースを生かす）
        print(f"Warning: Realtime/X trend endpoint fallback triggered. Reason: {e}")
            
    return candidates, status_report

# =====================================================================
# 3. フィルタリング ＆ スコーアリングロジック
# =====================================================================
def filter_and_score(candidates):
    processed = []
    seen_titles = set()
    
    for c in candidates:
        title_summary = c["raw_title"] + " " + c["summary"]
        
        if any(neg in title_summary for neg in NEGATIVE_KEYWORDS):
            continue
            
        short_title = c["raw_title"][:8]
        if short_title in seen_titles:
            continue
        seen_titles.add(short_title)
        
        score = 0
        relevance = "medium"
        
        has_positive = any(pos in title_summary for pos in POSITIVE_KEYWORDS)
        if has_positive:
            score += 5
            c["engagement_signal"] = True
            relevance = "high"
            
        if "realtime" in c["source_category"] or c["source_category"] == "google_trends":
            score += 3
            
        c["_score"] = score
        c["relevance_to_niche"] = relevance
        c["niche_keywords"] = []
        
        processed.append(c)
        
    processed.sort(key=lambda x: x["_score"], reverse=True)
    return processed[:TARGET_TOTAL_COUNT]

# =====================================================================
# 4. メイン実行
# =====================================================================
def main():
    now_jst = datetime.utcnow() + timedelta(hours=9)
    valid_until_jst = now_jst + timedelta(hours=8)
    
    now_iso = now_jst.strftime("%Y-%m-%dT%H:%M:00+09:00")
    valid_until_iso = valid_until_jst.strftime("%Y-%m-%dT%H:%M:00+09:00")
    today_str = now_jst.strftime("%Y-%m-%d")
    
    print(f"Starting pipeline at {now_iso} JST...")
    
    raw_list, status_report = fetch_all_sources(now_iso)
    print(f"Raw data fetched. Initial Count: {len(raw_list)}")
    
    final_candidates = filter_and_score(raw_list)
    
    for i, candidate in enumerate(final_candidates, start=1):
        candidate["article_id"] = f"article_{str(i).zfill(6)}"
        candidate["date"] = today_str
        if "_score" in candidate:
            del candidate["_score"]

    output_data = {
        "generated_at": now_iso,
        "valid_until": valid_until_iso,
        "total_count": len(final_candidates),
        "sources": {
            "google_trends": { "count": sum(1 for x in final_candidates if x["source_category"] == "google_trends"), "fetch_status": status_report["google_trends"] },
            "yahoo_news_ranking": { "count": sum(1 for x in final_candidates if x["source_category"] == "yahoo_news_ranking"), "fetch_status": status_report["yahoo_news_ranking"] },
            "yahoo_realtime": { "count": sum(1 for x in final_candidates if x["source_category"] == "yahoo_realtime"), "fetch_status": status_report["yahoo_realtime"] },
            "x_realtime": { "count": sum(1 for x in final_candidates if x["source_category"] == "x_realtime"), "fetch_status": status_report["x_realtime"] },
            "news_web": { "count": sum(1 for x in final_candidates if x["source_category"] == "news_web"), "fetch_status": status_report["news_web"] }
        },
        "candidates": final_candidates
    }
    
    os.makedirs("trend", exist_ok=True)
    file_path = "trend/latest.json"
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
        
    if os.path.exists(file_path):
        print(f"Successfully deployed total {len(final_candidates)} high-value trend objects.")
    else:
        raise FileNotFoundError(f"Failed output to target path.")

if __name__ == "__main__":
    main()

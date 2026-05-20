import os
import json
from datetime import datetime, timedelta
import requests
import xml.etree.ElementTree as ET

# =====================================================================
# 1. 設定値・固定スキーマ定義
# =====================================================================
DAILY_ARTICLE_TARGET = 65  # 目標数
TARGET_TOTAL_COUNT = DAILY_ARTICLE_TARGET + 5

# ポジティブキーワード（HARM・フック加点用）
POSITIVE_KEYWORDS = [
    "値上げ", "終了", "廃止", "無料", "コスパ", "実質", "大損", "増税", "補助金",
    "危険", "食中毒", "カビ", "不調", "熱中症", "対策", "激変", "注意",
    "マナー", "裏技", "知らないと損", "NG", "劇的", "正解", "論争", "炎上", "バズ"
]

# ネガティブキーワード（即座に除外用）
NEGATIVE_KEYWORDS = [
    "逮捕", "容疑者", "死去", "訃報", "事故", "衝突", "不倫", "離婚", "政治", "閣議決定", "地裁判決"
]

RSS_SOURCES = {
    "google_trends": "https://trends.google.co.jp/trending/rss?geo=JP",
    "yahoo_news_topics": "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    "yahoo_news_domestic": "https://news.yahoo.co.jp/rss/topics/domestic.xml",
    "yahoo_news_entertainment": "https://news.yahoo.co.jp/rss/topics/entertainment.xml"
}

# =====================================================================
# 2. データ収集処理 (Fetch & Parse)
# =====================================================================
def fetch_rss_candidates(now_iso):
    candidates = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    status_report = {
        "google_trends": "error",
        "yahoo_news_ranking": "unavailable",
        "yahoo_realtime": "unavailable",
        "x_realtime": "unavailable",
        "news_web": "error"
    }
    
    # XMLネームスペースのマッピング（Googleトレンド用）
    namespaces = {
        'ht': 'https://trends.google.co.jp/trending/rss',
        'ht_alt': 'https://trends.google.com/trending/rss' # 念のための代替
    }
    
    for source_key, url in RSS_SOURCES.items():
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                print(f"HTTP Error {response.status_code} for {source_key}")
                continue
            
            if "google" in source_key:
                status_report["google_trends"] = "ok"
            else:
                status_report["news_web"] = "ok"
            
            # 安全にXMLをパース
            root = ET.fromstring(response.content)
            for item in root.findall(".//item"):
                title_el = item.find("title")
                link_el = item.find("link")
                desc_el = item.find("description")
                
                title = title_el.text if (title_el is not None and title_el.text) else ""
                link = link_el.text if (link_el is not None and link_el.text) else ""
                description = desc_el.text if (desc_el is not None and desc_el.text) else ""
                
                if not title:
                    continue  # タイトルが空のものはスキップ
                
                # Google固有のトラフィック数を安全に取得
                approx_traffic = "100+"
                if "google" in source_key:
                    traffic_el = item.find("ht:approx_traffic", namespaces)
                    if traffic_el is None:
                        traffic_el = item.find("ht_alt:approx_traffic", namespaces)
                    
                    if traffic_el is not None and traffic_el.text:
                        approx_traffic = traffic_el.text
                elif "topics" in source_key:
                    approx_traffic = "500+"
                
                category = "google_trends" if "google" in source_key else "news_web"
                
                candidates.append({
                    "source_category": category,
                    "raw_title": title,
                    "url": link,
                    "summary": description,
                    "signal_type": "trend" if category == "google_trends" else "news",
                    "engagement_signal": False,
                    "approx_traffic": approx_traffic,
                    "preliminary_claim_type": "hard_fact" if "domestic" in source_key else "opinion",
                    "collected_at": now_iso
                })
        except Exception as e:
            print(f"Error parsing {source_key}: {e}")
            
    return candidates, status_report

# =====================================================================
# 3. フィルタリング ＆ スコーアリングロジック
# =====================================================================
def filter_and_score(candidates):
    processed = []
    seen_titles = set()
    
    for c in candidates:
        title_summary = c["raw_title"] + " " + c["summary"]
        
        # 1. ネガティブフィルタ
        if any(neg in title_summary for neg in NEGATIVE_KEYWORDS):
            continue
            
        # 2. 重複排除 (前方8文字)
        short_title = c["raw_title"][:8]
        if short_title in seen_titles:
            continue
        seen_titles.add(short_title)
        
        # 3. スコアリング
        score = 0
        relevance = "medium"
        
        has_positive = any(pos in title_summary for pos in POSITIVE_KEYWORDS)
        if has_positive:
            score += 5
            c["engagement_signal"] = True
            relevance = "high"
            
        if c["source_category"] == "google_trends":
            score += 2
            
        c["_score"] = score
        c["relevance_to_niche"] = relevance
        c["niche_keywords"] = []
        
        processed.append(c)
        
    processed.sort(key=lambda x: x["_score"], reverse=True)
    return processed[:TARGET_TOTAL_COUNT]

# =====================================================================
# 4. メイン実行 ＆ 統合出力
# =====================================================================
def main():
    now_jst = datetime.utcnow() + timedelta(hours=9)
    valid_until_jst = now_jst + timedelta(hours=8)
    
    now_iso = now_jst.strftime("%Y-%m-%dT%H:%M:00+09:00")
    valid_until_iso = valid_until_jst.strftime("%Y-%m-%dT%H:%M:00+09:00")
    today_str = now_jst.strftime("%Y-%m-%d")
    
    print(f"Starting collection at {now_iso} JST...")
    
    raw_list, status_report = fetch_rss_candidates(now_iso)
    print(f"Successfully fetched raw data. Count: {len(raw_list)}")
    
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
            "yahoo_news_ranking": { "count": 0, "fetch_status": status_report["yahoo_news_ranking"] },
            "yahoo_realtime": { "count": 0, "fetch_status": status_report["yahoo_realtime"] },
            "x_realtime": { "count": 0, "fetch_status": status_report["x_realtime"] },
            "news_web": { "count": sum(1 for x in final_candidates if x["source_category"] == "news_web"), "fetch_status": status_report["news_web"] }
        },
        "candidates": final_candidates
    }
    
    os.makedirs("trend", exist_ok=True)
    file_path = "trend/latest.json"
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
        
    if os.path.exists(file_path):
        print(f"Successfully updated single file: {file_path} (Count: {len(final_candidates)})")
    else:
        raise FileNotFoundError(f"CRITICAL: Failed to write {file_path}")

if __name__ == "__main__":
    main()

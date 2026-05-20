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
def fetch_rss_candidates():
    candidates = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    for source_key, url in RSS_SOURCES.items():
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                continue
            
            root = ET.fromstring(response.content)
            for item in root.findall(".//item"):
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                description = item.find("description").text if item.find("description") is not None else ""
                
                category = "google_trends" if "google" in source_key else "news_web"
                
                candidates.append({
                    "source_category": category,
                    "raw_title": title,
                    "url": link,
                    "summary": description,
                    "signal_type": "trend" if category == "google_trends" else "news",
                    "preliminary_claim_type": "hard_fact" if "domestic" in source_key else "opinion",
                    "engagement_signal": False
                })
        except Exception as e:
            print(f"Error fetching {source_key}: {e}")
            
    return candidates

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
            
        # 2. 重複排除 (前方一致)
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
# 4. メイン実行 ＆ 単一ファイル出力・検証
# =====================================================================
def main():
    # 日本時間 (UTC+9) の算出
    now_jst = datetime.utcnow() + timedelta(hours=9)
    valid_until_jst = now_jst + timedelta(hours=8) # 8時間後を有効期限とする
    
    today_str = now_jst.strftime("%Y-%m-%d")
    
    print(f"Starting collection at {now_jst.isoformat()} JST...")
    
    # データ収集とフィルタ
    raw_list = fetch_rss_candidates()
    final_candidates = filter_and_score(raw_list)
    
    # IDの付与と一時キーの削除
    for i, candidate in enumerate(final_candidates, start=1):
        candidate["article_id"] = f"article_{str(i).zfill(6)}"
        candidate["date"] = today_str
        if "_score" in candidate:
            del candidate["_score"]

    # ご提示いただいたメタ情報付きの出力形式へラッピング
    output_data = {
        "generated_at": now_jst.strftime("%Y-%m-%dT%H:%M:%00+09:00"),
        "valid_until": valid_until_jst.strftime("%Y-%m-%dT%H:%M:%00+09:00"),
        "total_count": len(final_candidates),
        "sources": {
            "google_trends": { "count": sum(1 for x in final_candidates if x["source_category"] == "google_trends"), "fetch_status": "ok" },
            "yahoo_news_ranking": { "count": 0, "fetch_status": "unavailable" },
            "yahoo_realtime": { "count": 0, "fetch_status": "unavailable" },
            "x_realtime": { "count": 0, "fetch_status": "unavailable" },
            "news_web": { "count": sum(1 for x in final_candidates if x["source_category"] == "news_web"), "fetch_status": "ok" }
        },
        "candidates": final_candidates
    }
    
    # 保存先ディレクトリの作成と書き込み
    os.makedirs("trend", exist_ok=True)
    file_path = "trend/latest.json"
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
        
    # READ検証
    if os.path.exists(file_path):
        print(f"Successfully updated single file: {file_path} (Count: {len(final_candidates)})")
    else:
        print(f"CRITICAL: Failed to write {file_path}")

if __name__ == "__main__":
    main()

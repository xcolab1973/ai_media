import os
import re
import json
from datetime import datetime
import requests
import xml.etree.ElementTree as ET

# =====================================================================
# 1. 設定値・固定スキーマ定義
# =====================================================================
DAILY_ARTICLE_TARGET = 65  # 目標数 (上位 daily_article_target + 5 = 70件採用)
TARGET_TOTAL_COUNT = DAILY_ARTICLE_TARGET + 5

# ポジティブキーワード（HARM・フック加点用）
POSITIVE_KEYWORDS = [
    # Money
    "値上げ", "終了", "廃止", "無料", "コスパ", "実質", "大損", "増税", "補助金",
    # Health
    "危険", "食中毒", "カビ", "不調", "熱中症", "対策", "激変", "注意",
    # Relation / Ambition
    "マナー", "裏技", "知らないと損", "NG", "劇的", "正解", "論争", "炎上", "バズ"
]

# ネガティブキーワード（即座に除外/low判定用）
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
            
            # XMLパース
            root = ET.fromstring(response.content)
            for item in root.findall(".//item"):
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                description = item.find("description").text if item.find("description") is not None else ""
                
                # スキーマに合わせたマッピング
                category = "google_trends" if "google" in source_key else "news_web"
                
                candidates.append({
                    "source_category": category,
                    "raw_title": title,
                    "url": link,
                    "summary": description,
                    "signal_type": "trend" if category == "google_trends" else "news",
                    "preliminary_claim_type": "hard_fact" if "domestic" in source_key else "opinion",
                    "engagement_signal": False  # 初期値。後段のフィルタでTrue制御
                })
        except Exception as e:
            print(f"Error fetching {source_key}: {e}")
            
    return candidates

def fetch_x_tiktok_trends():
    """ X(リアルタイム)やTikTok連動トレンドをサンプリングする拡張用モック """
    additional_candidates = []
    return additional_candidates

# =====================================================================
# 3. フィルタリング ＆ スコーアリングロジック
# =====================================================================
def filter_and_score(candidates):
    processed = []
    seen_titles = set()
    
    for c in candidates:
        title_summary = c["raw_title"] + " " + c["summary"]
        
        # 1. ネガティブフィルタ（即座に除外）
        if any(neg in title_summary for neg in NEGATIVE_KEYWORDS):
            continue
            
        # 2. 重複排除 (簡易的なタイトル前方一致/完全一致チェック)
        short_title = c["raw_title"][:8]  # 前方8文字が被るものは同一ニュースとみなす
        if short_title in seen_titles:
            continue
        seen_titles.add(short_title)
        
        # 3. 機械的スコアリング
        score = 0
        relevance = "medium"
        
        # ポジティブキーワード（HARM判定）による加点
        has_positive = any(pos in title_summary for pos in POSITIVE_KEYWORDS)
        if has_positive:
            score += 5
            c["engagement_signal"] = True  # 条件合致でシグナルを強制ON
            relevance = "high"
            
        if c["source_category"] == "google_trends":
            score += 2
            
        c["_score"] = score  # ソート用の一時的な内部キー
        c["relevance_to_niche"] = relevance
        c["niche_keywords"] = []
        
        processed.append(c)
        
    # スコアの高い順にソート
    processed.sort(key=lambda x: x["_score"], reverse=True)
    return processed[:TARGET_TOTAL_COUNT]

# =====================================================================
# 4. メイン実行 ＆ ファイル保存（検証フェーズ含む）
# =====================================================================
def main():
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"Starting collection for {today_str}...")
    
    # データ収集
    raw_list = fetch_rss_candidates() + fetch_x_tiktok_trends()
    print(f"Raw candidates collected: {len(raw_list)}")
    
    # フィルタ ＆ スコアリング適用
    final_candidates = filter_and_score(raw_list)
    print(f"Filtered candidates (Target max 70): {len(final_candidates)}")
    
    # ローカル保存・検証ループ
    for i, candidate in enumerate(final_candidates, start=1):
        article_id = f"article_{str(i).zfill(6)}"  # 6桁固定フォーマット
        candidate["article_id"] = article_id
        candidate["date"] = today_str
        
        # 内部スコア用キーは既存スキーマにないため削除
        if "_score" in candidate:
            del candidate["_score"]
            
        # 保存先ディレクトリの作成
        dir_path = f"01_Artifacts/{today_str}/batch_001/{article_id}"
        os.makedirs(dir_path, exist_ok=True)
        file_path = os.path.join(dir_path, "00_discovery_candidate.json")
        
        # WRITE
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(candidate, f, ensure_ascii=False, indent=2)
            
        # READ検証 (存在チェック)
        if not os.path.exists(file_path):
            print(f"CRITICAL: Failed to write {file_path}")

    print("Successfully processed and verified all candidates.")

if __name__ == "__main__":
    main()

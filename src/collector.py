import os
import json
import re
from datetime import datetime, timedelta
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

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

# ニュースRSS（既存12ソース維持）
RSS_SOURCES = {
    "google_trends":         "https://trends.google.co.jp/trending/rss?geo=JP",
    "yahoo_news_topics":     "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    "yahoo_news_domestic":   "https://news.yahoo.co.jp/rss/topics/domestic.xml",
    "yahoo_news_entertainment": "https://news.yahoo.co.jp/rss/topics/entertainment.xml",
    "yahoo_news_business":   "https://news.yahoo.co.jp/rss/topics/business.xml",
    "yahoo_news_it":         "https://news.yahoo.co.jp/rss/topics/it.xml",
    "yahoo_news_local":      "https://news.yahoo.co.jp/rss/topics/local.xml",
    "yahoo_news_world":      "https://news.yahoo.co.jp/rss/topics/world.xml",
    "yahoo_news_life":       "https://news.yahoo.co.jp/rss/topics/life.xml",
    "yahoo_news_sports":     "https://news.yahoo.co.jp/rss/topics/sports.xml",
    "yahoo_news_science":    "https://news.yahoo.co.jp/rss/topics/science.xml",
    "yahoo_news_gourmet":    "https://news.yahoo.co.jp/rss/topics/gourmet.xml"
}

# 実SNSシグナルRSS
# 旧: Googleトレンド上位20件に#を付けてx_realtime/yahoo_realtimeに偽マッピング → 廃止
# 新: 本物のソーシャルエンゲージメントシグナルを直接取得
# URLはリスト形式: 先頭から順に試し、アイテムが取れた時点で採用（フォールバック方式）
SNS_SOURCES = {
    # はてなブックマーク ホットエントリー → yahoo_realtime枠
    "hatena_hotentry": [
        "https://b.hatena.ne.jp/hotentry/general.rss",  # カテゴリ別（安定）
        "https://b.hatena.ne.jp/hotentry.rss",           # 全カテゴリ（フォールバック）
    ],
    # Togetter人気まとめ → x_realtime枠
    "togetter_hot": [
        "https://togetter.com/rss/hot",
    ],
}

# =====================================================================
# 2. ユーティリティ関数
# =====================================================================

def parse_traffic_score(approx_traffic_str):
    """approx_traffic文字列をスコア加算値に変換
    1M+ → +5, 500K+ → +4, 100K+ → +3, 10K+ → +2, 1K+ → +1, それ以外 → 0
    """
    if not approx_traffic_str:
        return 0
    t = approx_traffic_str.strip().upper().replace("+", "").replace(",", "")
    multiplier = 1
    if t.endswith("M"):
        multiplier = 1_000_000
        t = t[:-1]
    elif t.endswith("K"):
        multiplier = 1_000
        t = t[:-1]
    try:
        value = float(t) * multiplier
    except ValueError:
        return 0
    if value >= 1_000_000: return 5
    if value >= 500_000:   return 4
    if value >= 100_000:   return 3
    if value >= 10_000:    return 2
    if value >= 1_000:     return 1
    return 0


def parse_freshness_score(pub_date_str, now_utc):
    """pubDate → フレッシュネス加算値 (<1h=+4, <3h=+3, <6h=+2, <12h=+1, それ以降=0)"""
    if not pub_date_str:
        return 0
    try:
        dt = parsedate_to_datetime(pub_date_str)
        # タイムゾーン情報を除去してUTC比較
        import calendar
        dt_utc = datetime.utcfromtimestamp(calendar.timegm(dt.timetuple()))
        age_hours = (now_utc - dt_utc).total_seconds() / 3600
        if age_hours < 1:  return 4
        if age_hours < 3:  return 3
        if age_hours < 6:  return 2
        if age_hours < 12: return 1
        return 0
    except Exception:
        return 0


def normalize_for_dedup(title):
    """重複排除用の正規化: 記号・スペース・ハッシュタグ記号を除去した先頭15文字"""
    cleaned = re.sub(r'[#＃\s　・「」【】『』（）()、。！？!?…—\-～~]', '', title)
    return cleaned[:15]


# =====================================================================
# 3. データ収集処理 (Fetch & Parse)
# =====================================================================
def fetch_all_sources(now_iso, now_utc):
    candidates = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    status_report = {
        "google_trends":    "error",
        "yahoo_news_ranking": "ok",
        "yahoo_realtime":   "error",
        "x_realtime":       "error",
        "news_web":         "error"
    }

    namespaces = {
        'ht':     'https://trends.google.co.jp/trending/rss',
        'ht_alt': 'https://trends.google.com/trending/rss'
    }

    # --- 通常ニュース＋Googleトレンド RSS（既存12ソース）---
    for source_key, url in RSS_SOURCES.items():
        try:
            response = requests.get(url, headers=headers, timeout=12)
            if response.status_code != 200:
                print(f"HTTP Error {response.status_code} for {source_key}")
                continue

            root = ET.fromstring(response.content)
            items = root.findall(".//item")
            if not items:
                continue

            if "google" in source_key:
                status_report["google_trends"] = "ok"
            else:
                status_report["news_web"] = "ok"

            for item in items:
                title_el   = item.find("title")
                link_el    = item.find("link")
                desc_el    = item.find("description")
                pubdate_el = item.find("pubDate")

                title       = title_el.text   if (title_el   is not None and title_el.text)   else ""
                link        = link_el.text    if (link_el    is not None and link_el.text)    else ""
                description = desc_el.text    if (desc_el    is not None and desc_el.text)    else ""
                pub_date    = pubdate_el.text  if (pubdate_el is not None and pubdate_el.text) else ""

                if not title:
                    continue

                approx_traffic = "100+"
                if "google" in source_key:
                    traffic_el = (item.find("ht:approx_traffic", namespaces)
                                  or item.find("ht_alt:approx_traffic", namespaces))
                    if traffic_el is not None and traffic_el.text:
                        approx_traffic = traffic_el.text
                elif source_key in ["yahoo_news_topics", "yahoo_news_business", "yahoo_news_life"]:
                    approx_traffic = "500+"

                if source_key in ["yahoo_news_business", "yahoo_news_it",
                                   "yahoo_news_life", "yahoo_news_science"]:
                    category    = "yahoo_news_ranking"
                    signal_type = "news"
                else:
                    category    = "google_trends" if "google" in source_key else "news_web"
                    signal_type = "trend" if category == "google_trends" else "news"

                candidates.append({
                    "source_category":      category,
                    "raw_title":            title,
                    "url":                  link,
                    "summary":              description,
                    "signal_type":          signal_type,
                    "engagement_signal":    False,
                    "approx_traffic":       approx_traffic,
                    "preliminary_claim_type": (
                        "hard_fact" if source_key in ["yahoo_news_domestic", "yahoo_news_business"]
                        else "opinion"
                    ),
                    "collected_at": now_iso,
                    "_pub_date":    pub_date,
                })
        except Exception as e:
            print(f"Warning: Skip {source_key} due to parse error: {e}")

    # --- 実SNSシグナルRSS（はてなブックマーク・Togetter）---
    sns_config = {
        "hatena_hotentry": {
            "category":    "yahoo_realtime",
            "status_key":  "yahoo_realtime",
            "summary_tmpl": "はてなブックマーク ホットエントリー — 多数のユーザーがブックマークした話題の記事",
        },
        "togetter_hot": {
            "category":    "x_realtime",
            "status_key":  "x_realtime",
            "summary_tmpl": "Togetter人気まとめ — Twitter/X上で注目が集まったツイートのまとめ記事",
        },
    }
    ATOM_NS = "http://www.w3.org/2005/Atom"

    for source_key, url_list in SNS_SOURCES.items():
        cfg = sns_config[source_key]
        fetched = False
        for url in url_list:
            if fetched:
                break
            try:
                response = requests.get(url, headers=headers, timeout=12)
                if response.status_code != 200:
                    print(f"HTTP {response.status_code} for {source_key} ({url}) — try next")
                    continue

                root = ET.fromstring(response.content)

                # RSS 2.0 の <item> を優先、なければ Atom の <entry> を試みる
                items = root.findall(".//item")
                is_atom = False
                if not items:
                    items = root.findall(f".//{{{ATOM_NS}}}entry")
                    is_atom = True
                if not items:
                    items = root.findall(".//entry")
                    is_atom = True

                if not items:
                    print(f"No items found in {source_key} ({url}) — try next")
                    continue

                print(f"  {source_key}: {len(items)} items from {url} (atom={is_atom})")
                status_report[cfg["status_key"]] = "ok"
                fetched = True

                for item in items:
                    if is_atom:
                        # Atom: <title>, <link href="...">, <summary>/<content>, <updated>/<published>
                        ns = ATOM_NS
                        title_el   = item.find(f"{{{ns}}}title") or item.find("title")
                        link_el    = item.find(f"{{{ns}}}link")  or item.find("link")
                        desc_el    = (item.find(f"{{{ns}}}summary")
                                      or item.find(f"{{{ns}}}content")
                                      or item.find("summary"))
                        pubdate_el = (item.find(f"{{{ns}}}updated")
                                      or item.find(f"{{{ns}}}published")
                                      or item.find("updated"))
                        title       = title_el.text  if (title_el   is not None and title_el.text)  else ""
                        # Atom の <link> は href 属性にURLが入る
                        if link_el is not None:
                            link = link_el.get("href", "") or (link_el.text or "")
                        else:
                            link = ""
                        description = desc_el.text   if (desc_el    is not None and desc_el.text)   else ""
                        pub_date    = pubdate_el.text if (pubdate_el is not None and pubdate_el.text) else ""
                    else:
                        # RSS 2.0: 通常フィールド
                        title_el   = item.find("title")
                        link_el    = item.find("link")
                        desc_el    = item.find("description")
                        pubdate_el = item.find("pubDate")
                        title       = title_el.text   if (title_el   is not None and title_el.text)   else ""
                        link        = link_el.text    if (link_el    is not None and link_el.text)    else ""
                        description = desc_el.text    if (desc_el    is not None and desc_el.text)    else ""
                        pub_date    = pubdate_el.text  if (pubdate_el is not None and pubdate_el.text) else ""

                    if not title:
                        continue

                    candidates.append({
                        "source_category":      cfg["category"],
                        "raw_title":            title,
                        "url":                  link,
                        "summary":              description if description else cfg["summary_tmpl"],
                        "signal_type":          "trend",
                        "engagement_signal":    True,
                        "approx_traffic":       "100+",
                        "preliminary_claim_type": "opinion",
                        "collected_at": now_iso,
                        "_pub_date":    pub_date,
                    })

            except Exception as e:
                print(f"Warning: Skip {source_key} ({url}): {e}")

        if not fetched:
            print(f"Warning: All URLs failed for {source_key}")

    return candidates, status_report


# =====================================================================
# 4. フィルタリング ＆ スコアリングロジック
# =====================================================================
def filter_and_score(candidates, now_utc):
    # --- クロスソースボーナス用: 正規化タイトル → 出現ソースカテゴリのセット ---
    title_source_map = {}
    for c in candidates:
        key = normalize_for_dedup(c["raw_title"])
        if key not in title_source_map:
            title_source_map[key] = set()
        title_source_map[key].add(c["source_category"])

    processed = []
    seen_titles = set()

    for c in candidates:
        title_summary = c["raw_title"] + " " + c["summary"]

        # ネガティブキーワードフィルタ
        if any(neg in title_summary for neg in NEGATIVE_KEYWORDS):
            continue

        # 重複排除（正規化後先頭15文字）
        dedup_key = normalize_for_dedup(c["raw_title"])
        if dedup_key in seen_titles:
            continue
        seen_titles.add(dedup_key)

        score    = 0
        relevance = "medium"

        # ポジティブキーワード加算: +5
        has_positive = any(pos in title_summary for pos in POSITIVE_KEYWORDS)
        if has_positive:
            score += 5
            c["engagement_signal"] = True
            relevance = "high"

        # ソースカテゴリ加算: realtime/google_trends → +3
        if "realtime" in c["source_category"] or c["source_category"] == "google_trends":
            score += 3

        # approx_traffic加算（Googleトレンドのみ有効値を持つ）: 最大+5
        score += parse_traffic_score(c.get("approx_traffic", ""))

        # フレッシュネス加算: 最大+4
        score += parse_freshness_score(c.get("_pub_date", ""), now_utc)

        # クロスソースボーナス: 2ソース以上で同タイトル検出 → +2（リアルトレンド確定）
        if len(title_source_map.get(dedup_key, set())) >= 2:
            score += 2
            relevance = "high"
            c["engagement_signal"] = True

        c["_score"]              = score
        c["relevance_to_niche"]  = relevance
        c["niche_keywords"]      = []

        processed.append(c)

    processed.sort(key=lambda x: x["_score"], reverse=True)
    return processed[:TARGET_TOTAL_COUNT]


# =====================================================================
# 5. メイン実行
# =====================================================================
def main():
    now_utc       = datetime.utcnow()
    now_jst       = now_utc + timedelta(hours=9)
    valid_until_jst = now_jst + timedelta(hours=8)

    now_iso        = now_jst.strftime("%Y-%m-%dT%H:%M:00+09:00")
    valid_until_iso = valid_until_jst.strftime("%Y-%m-%dT%H:%M:00+09:00")
    today_str      = now_jst.strftime("%Y-%m-%d")

    print(f"Starting pipeline at {now_iso} JST...")

    raw_list, status_report = fetch_all_sources(now_iso, now_utc)
    print(f"Raw sources fetched completely. Total raw pool: {len(raw_list)}")

    final_candidates = filter_and_score(raw_list, now_utc)

    for i, candidate in enumerate(final_candidates, start=1):
        candidate["article_id"] = f"article_{str(i).zfill(6)}"
        candidate["date"]       = today_str
        # 内部処理用フィールドを出力から除去
        for internal_key in ("_score", "_pub_date"):
            candidate.pop(internal_key, None)

    output_data = {
        "generated_at":  now_iso,
        "valid_until":   valid_until_iso,
        "total_count":   len(final_candidates),
        "sources": {
            "google_trends":    {"count": sum(1 for x in final_candidates if x["source_category"] == "google_trends"),    "fetch_status": status_report["google_trends"]},
            "yahoo_news_ranking": {"count": sum(1 for x in final_candidates if x["source_category"] == "yahoo_news_ranking"), "fetch_status": status_report["yahoo_news_ranking"]},
            "yahoo_realtime":   {"count": sum(1 for x in final_candidates if x["source_category"] == "yahoo_realtime"),   "fetch_status": status_report["yahoo_realtime"]},
            "x_realtime":       {"count": sum(1 for x in final_candidates if x["source_category"] == "x_realtime"),       "fetch_status": status_report["x_realtime"]},
            "news_web":         {"count": sum(1 for x in final_candidates if x["source_category"] == "news_web"),         "fetch_status": status_report["news_web"]},
        },
        "candidates": final_candidates
    }

    os.makedirs("trends", exist_ok=True)
    file_path = "trends/latest.json"

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    if os.path.exists(file_path):
        print(f"Successfully deployed total {len(final_candidates)} high-value trend objects.")
    else:
        raise FileNotFoundError("Failed output to target path.")


if __name__ == "__main__":
    main()

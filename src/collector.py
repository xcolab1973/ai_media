import os
import json
import re
from datetime import datetime, timedelta, timezone
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

# =====================================================================
# 1. 設定値・固定スキーマ定義
# =====================================================================
DAILY_ARTICLE_TARGET = 65
TARGET_TOTAL_COUNT = DAILY_ARTICLE_TARGET + 5

# ソースカテゴリごとの最低保証枠（スコアに関わらずこの枠を確保）
SOURCE_MIN_QUOTA = {
    "yahoo_realtime": 5,
    "x_realtime":     5,
    "google_trends":  10,
}

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
}

SNS_SOURCES = {
    "hatena_hotentry": [
        "https://b.hatena.ne.jp/hotentry/general.rss",
        "https://b.hatena.ne.jp/hotentry/social.rss",
        "https://b.hatena.ne.jp/hotentry.rss",
        "https://news.livedoor.com/topics/rss/top.xml",
    ],
    "togetter_hot": [
        "https://togetter.com/rss/hot",
    ],
}

# =====================================================================
# 2. ユーティリティ関数
# =====================================================================

def parse_traffic_score(approx_traffic_str):
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
    """RFC 2822 / ISO 8601 両対応。tzinfo-aware 比較で JST オフセットを正確に処理。"""
    if not pub_date_str:
        return 0
    dt = None
    try:
        dt = parsedate_to_datetime(pub_date_str)
    except Exception:
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
        except Exception:
            return 0
    try:
        now_aware = now_utc.replace(tzinfo=timezone.utc)
        if dt.tzinfo is not None:
            age_hours = (now_aware - dt).total_seconds() / 3600
        else:
            age_hours = (now_utc - dt).total_seconds() / 3600
        if age_hours < 1:  return 4
        if age_hours < 3:  return 3
        if age_hours < 6:  return 2
        if age_hours < 12: return 1
        return 0
    except Exception:
        return 0


def normalize_for_dedup(title):
    cleaned = re.sub(r'[#＃\s　・「」【】『』（）()、。！？!?…—\-～~]', '', title)
    return cleaned[:15]


def _elem_text(*elements):
    """複数の Element 候補から最初に text が取れたものを返す。全滅なら ''。
    ElementTree の Element は text=None でも truthy なため、
    'el or fallback_el' パターンは使わず、text を直接チェックする。
    """
    for el in elements:
        if el is not None and el.text:
            return el.text.strip()
    return ""


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

    # --- 通常ニュース＋Googleトレンド RSS ---
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
                title       = _elem_text(item.find("title"))
                link        = _elem_text(item.find("link"))
                description = _elem_text(item.find("description"))
                pub_date    = _elem_text(item.find("pubDate"))

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
    RSS1_NS = "http://purl.org/rss/1.0/"
    DC_NS   = "http://purl.org/dc/elements/1.1/"

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

                items = root.findall(".//item")
                is_atom = False
                is_rss1 = False
                if not items:
                    items = root.findall(f".//{{{ATOM_NS}}}entry")
                    is_atom = bool(items)
                if not items:
                    items = root.findall(".//entry")
                    is_atom = bool(items)
                if not items:
                    items = root.findall(f".//{{{RSS1_NS}}}item")
                    is_rss1 = bool(items)

                if not items:
                    print(f"No items found in {source_key} ({url}) — try next")
                    continue

                print(f"  {source_key}: {len(items)} items from {url} (atom={is_atom}, rss1={is_rss1})")
                status_report[cfg["status_key"]] = "ok"
                fetched = True

                # ---- RSS1 デバッグ: 最初のアイテムの全タグを出力 ----
                if is_rss1 and items:
                    first = items[0]
                    child_tags = [child.tag for child in first]
                    print(f"  RSS1 first item child tags: {child_tags[:8]}")
                    # title テキスト候補を全試行
                    t_rss1 = first.find(f"{{{RSS1_NS}}}title")
                    t_bare = first.find("title")
                    print(f"  RSS1 title candidates: rss1_ns={t_rss1!r}(text={getattr(t_rss1,'text',None)!r}), bare={t_bare!r}(text={getattr(t_bare,'text',None)!r})")

                appended = 0
                skipped_no_title = 0

                for item in items:
                    if is_atom:
                        ns = ATOM_NS
                        title = _elem_text(
                            item.find(f"{{{ns}}}title"),
                            item.find("title")
                        )
                        link_el = item.find(f"{{{ns}}}link") or item.find("link")
                        link = (link_el.get("href", "") if link_el is not None else "") or _elem_text(link_el)
                        description = _elem_text(
                            item.find(f"{{{ns}}}summary"),
                            item.find(f"{{{ns}}}content"),
                            item.find("summary")
                        )
                        pub_date = _elem_text(
                            item.find(f"{{{ns}}}updated"),
                            item.find(f"{{{ns}}}published"),
                            item.find("updated")
                        )
                    elif is_rss1:
                        # RSS 1.0: _elem_text で NS 付き → bare の順に試みる
                        # これにより text=None の truthy Element 問題を回避
                        title = _elem_text(
                            item.find(f"{{{RSS1_NS}}}title"),
                            item.find("title")
                        )
                        link = _elem_text(
                            item.find(f"{{{RSS1_NS}}}link"),
                            item.find("link")
                        )
                        description = _elem_text(
                            item.find(f"{{{RSS1_NS}}}description"),
                            item.find("description")
                        )
                        pub_date = _elem_text(
                            item.find(f"{{{DC_NS}}}date"),
                            item.find("pubDate")
                        )
                    else:
                        # RSS 2.0 / namespace なし RSS 1.0
                        # pubDate がない場合は dc:date にもフォールバック
                        title = _elem_text(item.find("title"))
                        link  = _elem_text(item.find("link"))
                        description = _elem_text(item.find("description"))
                        pub_date = _elem_text(
                            item.find("pubDate"),
                            item.find(f"{{{DC_NS}}}date")
                        )

                    if not title:
                        skipped_no_title += 1
                        continue

                    appended += 1
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

                print(f"  {source_key}: appended={appended} skipped_no_title={skipped_no_title}")

            except Exception as e:
                import traceback
                print(f"Warning: Skip {source_key} ({url}): {e}")
                traceback.print_exc()

        if not fetched:
            print(f"Warning: All URLs failed for {source_key}")

    return candidates, status_report


# =====================================================================
# 4. フィルタリング ＆ スコアリングロジック
# =====================================================================
def filter_and_score(candidates, now_utc):
    from collections import Counter

    # デバッグ: filter前のカテゴリ別件数
    raw_cats = Counter(c["source_category"] for c in candidates)
    print(f"  filter input by category: {dict(raw_cats)}")

    # クロスソースボーナス用
    title_source_map = {}
    for c in candidates:
        key = normalize_for_dedup(c["raw_title"])
        if key not in title_source_map:
            title_source_map[key] = set()
        title_source_map[key].add(c["source_category"])

    processed = []
    seen_titles = set()
    neg_count  = 0
    dedup_count = 0

    for c in candidates:
        title_summary = c["raw_title"] + " " + c["summary"]

        if any(neg in title_summary for neg in NEGATIVE_KEYWORDS):
            neg_count += 1
            if c["source_category"] == "yahoo_realtime":
                neg_word = next(neg for neg in NEGATIVE_KEYWORDS if neg in title_summary)
                print(f"  [NEGKW] yahoo_realtime: word={neg_word!r} title={c['raw_title'][:30]!r}")
            continue

        dedup_key = normalize_for_dedup(c["raw_title"])
        if dedup_key in seen_titles:
            dedup_count += 1
            if c["source_category"] == "yahoo_realtime":
                print(f"  [DEDUP] yahoo_realtime: key={dedup_key!r} title={c['raw_title'][:30]!r}")
            continue
        seen_titles.add(dedup_key)

        score    = 0
        relevance = "medium"

        has_positive = any(pos in title_summary for pos in POSITIVE_KEYWORDS)
        if has_positive:
            score += 5
            c["engagement_signal"] = True
            relevance = "high"

        if "realtime" in c["source_category"] or c["source_category"] == "google_trends":
            score += 3

        score += parse_traffic_score(c.get("approx_traffic", ""))

        freshness = parse_freshness_score(c.get("_pub_date", ""), now_utc)
        score += freshness

        if len(title_source_map.get(dedup_key, set())) >= 2:
            score += 2
            relevance = "high"
            c["engagement_signal"] = True

        c["_score"]              = score
        c["_freshness"]          = freshness
        c["relevance_to_niche"]  = relevance
        c["niche_keywords"]      = []

        processed.append(c)

    processed.sort(key=lambda x: x["_score"], reverse=True)

    cat_counts = Counter(x["source_category"] for x in processed)
    print(f"  filter_and_score: neg={neg_count} dedup={dedup_count} passed={len(processed)}")
    print(f"  category counts (before quota): {dict(cat_counts)}")

    # yahoo_realtime サンプル出力（スコア・freshness・pub_date）
    sample = [x for x in processed if x["source_category"] == "yahoo_realtime"][:3]
    for s in sample:
        print(f"  [OK] yahoo_realtime: score={s['_score']} freshness={s['_freshness']} pub={s.get('_pub_date','')!r} title={s['raw_title'][:30]!r}")

    # ソースカテゴリごとの最低保証枠を確保
    guaranteed = []
    for cat, min_count in SOURCE_MIN_QUOTA.items():
        cat_items = [x for x in processed if x["source_category"] == cat][:min_count]
        guaranteed.extend(cat_items)

    guaranteed_ids = {id(x) for x in guaranteed}
    rest = [x for x in processed if id(x) not in guaranteed_ids]
    final = (guaranteed + rest)[:TARGET_TOTAL_COUNT]
    final.sort(key=lambda x: x["_score"], reverse=True)

    cat_counts_final = Counter(x["source_category"] for x in final)
    print(f"  category counts (after quota, top {TARGET_TOTAL_COUNT}): {dict(cat_counts_final)}")

    return final


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
        for internal_key in ("_score", "_pub_date", "_freshness"):
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

    # --- note product line feed (2026-05-29) ---
    # note 製品ラインは clean/B2B とトレンドデータを分離する方針。
    # 現状は同一データを note_latest.json にも出力し、product_line マーカーを付与する。
    # 将来 note_categories.json によるフィルタ等で内容を分岐させる際はここを拡張する。
    note_output = dict(output_data)
    note_output["product_line"] = "note"
    note_file_path = "trends/note_latest.json"
    with open(note_file_path, "w", encoding="utf-8") as f:
        json.dump(note_output, f, ensure_ascii=False, indent=2)
    if not os.path.exists(note_file_path):
        raise FileNotFoundError("Failed output to note target path.")
    print("Also deployed note_latest.json for the note product line.")


if __name__ == "__main__":
    main()

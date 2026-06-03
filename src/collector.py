import os
import json
import re
import copy
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
# ※ source_category の 5 リテラルは Discovery / quota / 出力 sources ブロックが依存する契約値。
#    実ソース（はてブ/livedoor/Togetter）の違いは raw_source_key / source_label で表現する（リネーム禁止）。
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

# --- note 専用リランク用キーワード（latest.json=B2B には影響しない） ---
# note は消費者向け・体験型・暮らし/エバーグリーン寄り。下記を加点。
NOTE_POSITIVE_KEYWORDS = [
    "体験", "暮らし", "節約", "コスパ", "レシピ", "作り方", "やってみた", "収納", "掃除",
    "育児", "子育て", "健康", "ダイエット", "対策", "コツ", "裏技", "失敗", "後悔",
    "買ってよかった", "使ってみた", "比較", "ランキング", "おすすめ", "習慣", "片付け", "時短"
]
# note では速報ニュース性・政治・災害・経済指標を減点（記事化に向かないため）。
NOTE_HARD_NEWS_KEYWORDS = [
    "速報", "首相", "政権", "関税", "外交", "選挙", "国会", "死亡", "地震", "台風",
    "豪雨", "被告", "裁判", "容疑", "日経平均", "株価", "為替", "ドル円", "出生数",
    "戦争", "攻撃", "砲撃", "ミサイル"
]

# 読みやすいソースラベル（raw_source_key → source_label）
SOURCE_LABELS = {
    "google_trends":            "Google トレンド",
    "yahoo_news_topics":        "Yahoo!ニュース トピックス",
    "yahoo_news_domestic":      "Yahoo!ニュース 国内",
    "yahoo_news_entertainment": "Yahoo!ニュース エンタメ",
    "yahoo_news_business":      "Yahoo!ニュース 経済",
    "yahoo_news_it":            "Yahoo!ニュース IT",
    "yahoo_news_local":         "Yahoo!ニュース 地域",
    "yahoo_news_world":         "Yahoo!ニュース 国際",
    "yahoo_news_life":          "Yahoo!ニュース ライフ",
    "yahoo_news_sports":        "Yahoo!ニュース スポーツ",
    "yahoo_news_science":       "Yahoo!ニュース 科学",
    "hatena_hotentry":          "はてなブックマーク ホットエントリー",
    "livedoor_topics":          "livedoor トピックス",
    "togetter_hot":             "Togetter 人気まとめ",
}

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


def _first_element(*elements):
    """複数の Element 候補から最初に None でないものを返す。全滅なら None。
    Atom の <link> のように text/子要素を持たない（=truthy 判定が False になりうる）
    Element でも安全に拾うため、'el or fallback' ではなく is not None で判定する。
    """
    for el in elements:
        if el is not None:
            return el
    return None


def classify_topic_category(title, summary, signal_type, source_category):
    """preliminary_claim_type とは独立の補助分類。後工程の選別精度向上用。
    返り値: hard_fact / analysis / social_reaction / personal_story / medical_science / policy / opinion
    ※ preliminary_claim_type の enum（hard_fact/opinion 等）は変更しない。これは追加フィールド。
    """
    text = f"{title} {summary}"

    medical = ["医療", "治療", "がん", "癌", "新薬", "治験", "ワクチン", "手術", "疾患",
               "脳", "細胞", "タンパク質", "臨床", "学会", "発症", "症状", "免疫"]
    policy = ["政策", "首相", "増税", "減税", "消費税", "補助金", "関税", "法案", "政府",
              "省庁", "規制", "予算", "閣議", "国会", "選挙", "外交", "条例", "制度"]
    hard_fact = ["速報", "過去最少", "過去最多", "統計", "発表", "地震", "台風", "豪雨",
                 "株価", "日経平均", "為替", "ドル円", "値上がり", "値下がり", "死亡", "出生数"]
    personal = ["してみた", "した話", "やってみた", "日記", "泣いた", "我が家", "私は", "僕は",
                "体験", "買ってよかった", "後悔", "失敗談"]
    social = ["まとめ", "話題", "炎上", "論争", "賛否", "反応", "ツイート", "という声", "物議"]
    analysis = ["理由", "なぜ", "本当の", "徹底", "解説", "考察", "比較", "ランキング", "とは"]

    # 優先度順（誤分類を避けるため強い特徴から判定）
    if any(k in text for k in medical):
        return "medical_science"
    if any(k in text for k in policy):
        return "policy"
    if any(k in text for k in hard_fact) or signal_type == "news":
        return "hard_fact"
    if any(k in text for k in personal) or source_category in ("yahoo_realtime", "x_realtime"):
        # SNS 由来は体験/反応が多いが、まず personal を優先しつつ social へフォールバック
        if any(k in text for k in personal):
            return "personal_story"
        if any(k in text for k in social):
            return "social_reaction"
        return "social_reaction"
    if any(k in text for k in social):
        return "social_reaction"
    if any(k in text for k in analysis):
        return "analysis"
    return "opinion"


# =====================================================================
# 3. データ収集処理 (Fetch & Parse)
# =====================================================================
def fetch_all_sources(now_iso, now_utc):
    candidates = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # 全ソースを error 初期化し、実際に 1 件以上採用できたカテゴリだけ ok にする。
    # （旧バグ: yahoo_news_ranking が "ok" 初期固定で、取得失敗でも成功扱いだった）
    status_report = {
        "google_trends":      "error",
        "yahoo_news_ranking": "error",
        "yahoo_realtime":     "error",
        "x_realtime":         "error",
        "news_web":           "error",
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

            for item in items:
                title       = _elem_text(item.find("title"))
                link        = _elem_text(item.find("link"))
                description = _elem_text(item.find("description"))
                pub_date    = _elem_text(item.find("pubDate"))

                if not title:
                    continue

                approx_traffic = "100+"
                if "google" in source_key:
                    # ht:approx_traffic はリーフ要素（子なし）。'or' だと truthy 判定で
                    # 取り逃すため _elem_text で text 優先取得する。
                    traffic_text = _elem_text(
                        item.find("ht:approx_traffic", namespaces),
                        item.find("ht_alt:approx_traffic", namespaces),
                    )
                    if traffic_text:
                        approx_traffic = traffic_text
                elif source_key in ["yahoo_news_topics", "yahoo_news_business", "yahoo_news_life"]:
                    approx_traffic = "500+"

                if source_key in ["yahoo_news_business", "yahoo_news_it",
                                   "yahoo_news_life", "yahoo_news_science"]:
                    category    = "yahoo_news_ranking"
                    signal_type = "news"
                else:
                    category    = "google_trends" if "google" in source_key else "news_web"
                    signal_type = "trend" if category == "google_trends" else "news"

                # 実際に採用したカテゴリの取得ステータスを ok にする
                status_report[category] = "ok"

                candidates.append({
                    "source_category":      category,
                    "raw_source_key":       source_key,
                    "source_label":         SOURCE_LABELS.get(source_key, source_key),
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
                    "topic_category": classify_topic_category(title, description, signal_type, category),
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
            # 成功した実フィードに応じて raw_source_key を決める（実態表示の精度向上）
            if "livedoor" in url:
                raw_source_key = "livedoor_topics"
            elif "togetter" in url:
                raw_source_key = "togetter_hot"
            else:
                raw_source_key = "hatena_hotentry"
            source_label = SOURCE_LABELS.get(raw_source_key, raw_source_key)

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
                fetched = True

                appended = 0
                skipped_no_title = 0

                for item in items:
                    if is_atom:
                        ns = ATOM_NS
                        title = _elem_text(
                            item.find(f"{{{ns}}}title"),
                            item.find("title")
                        )
                        # Atom の <link> は href 属性のみで text/子要素を持たない。
                        # 'or' は truthy 判定で取り逃すため _first_element で is not None 判定する。
                        link_el = _first_element(item.find(f"{{{ns}}}link"), item.find("link"))
                        href = link_el.get("href", "") if link_el is not None else ""
                        link = href or _elem_text(link_el)
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
                    summary_val = description if description else cfg["summary_tmpl"]
                    candidates.append({
                        "source_category":      cfg["category"],
                        "raw_source_key":       raw_source_key,
                        "source_label":         source_label,
                        "raw_title":            title,
                        "url":                  link,
                        "summary":              summary_val,
                        "signal_type":          "trend",
                        "engagement_signal":    True,
                        "approx_traffic":       "100+",
                        "preliminary_claim_type": "opinion",
                        "topic_category": classify_topic_category(title, summary_val, "trend", cfg["category"]),
                        "collected_at": now_iso,
                        "_pub_date":    pub_date,
                    })

                # 1 件以上採用できた時だけ ok にする
                if appended > 0:
                    status_report[cfg["status_key"]] = "ok"

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

    # ソースカテゴリごとの最低保証枠を確保（B2B=latest.json 用・既存挙動を維持）
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

    debug_info = {
        "raw_by_category":     dict(raw_cats),
        "neg_excluded":        neg_count,
        "dedup_excluded":      dedup_count,
        "passed_total":        len(processed),
        "b2b_category_counts": dict(cat_counts_final),
    }

    # final（B2B top-70）と processed（全採用プール・note リランク用）の両方を返す
    return final, processed, debug_info


def rank_for_note(processed):
    """note_latest.json 専用の並べ替え。latest.json(B2B) には一切影響しない。
    topic_category とキーワードで体験型/暮らし系を加点、速報ニュース/政治/災害を減点する。
    """
    ranked = []
    for c in processed:
        ns = c.get("_score", 0)
        tc = c.get("topic_category", "opinion")

        # note-positive
        if tc in ("personal_story", "social_reaction"):
            ns += 4
        elif tc == "analysis":
            ns += 2
        elif tc == "medical_science":
            ns += 1
        # note-negative（速報ニュース・政治・経済指標は note に不向き）
        if tc in ("hard_fact", "policy"):
            ns -= 4
        if c.get("signal_type") == "news":
            ns -= 3

        text = c["raw_title"] + " " + c.get("summary", "")
        if any(k in text for k in NOTE_POSITIVE_KEYWORDS):
            ns += 2
        if any(k in text for k in NOTE_HARD_NEWS_KEYWORDS):
            ns -= 2

        c["_note_score"] = ns
        ranked.append(c)

    ranked.sort(key=lambda x: x["_note_score"], reverse=True)
    return ranked


# =====================================================================
# 5. メイン実行
# =====================================================================
def _finalize(candidate_list, today_str):
    """出力直前の整形：deepcopy 済みリストに article_id/date を採番し内部キーを除去する。"""
    for i, candidate in enumerate(candidate_list, start=1):
        candidate["article_id"] = f"article_{str(i).zfill(6)}"
        candidate["date"]       = today_str
        for internal_key in ("_score", "_pub_date", "_freshness", "_note_score"):
            candidate.pop(internal_key, None)
    return candidate_list


def _build_sources_block(candidate_list, status_report):
    from collections import Counter
    counts = Counter(x["source_category"] for x in candidate_list)
    return {
        cat: {"count": counts.get(cat, 0), "fetch_status": status_report[cat]}
        for cat in ("google_trends", "yahoo_news_ranking", "yahoo_realtime", "x_realtime", "news_web")
    }


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

    final_b2b_pool, processed_full, debug_info = filter_and_score(raw_list, now_utc)

    # --- B2B (latest.json)：従来どおりの選別。deepcopy で note と完全分離 ---
    b2b_candidates = _finalize([copy.deepcopy(c) for c in final_b2b_pool], today_str)

    # --- note (note_latest.json)：note 専用リランクから top-N。B2B には影響しない ---
    note_ranked = rank_for_note(processed_full)[:TARGET_TOTAL_COUNT]
    note_candidates = _finalize([copy.deepcopy(c) for c in note_ranked], today_str)

    output_data = {
        "generated_at":  now_iso,
        "valid_until":   valid_until_iso,
        "total_count":   len(b2b_candidates),
        "sources":       _build_sources_block(b2b_candidates, status_report),
        "candidates":    b2b_candidates,
    }

    os.makedirs("trends", exist_ok=True)
    file_path = "trends/latest.json"

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    if os.path.exists(file_path):
        print(f"Successfully deployed total {len(b2b_candidates)} high-value trend objects.")
    else:
        raise FileNotFoundError("Failed output to target path.")

    # --- note product line feed ---
    # note 製品ラインは B2B(latest.json) とは別ランクで出力する。
    # rank_for_note により体験型/暮らし系を優先し、速報ニュース/政治を後退させている。
    # スキーマ（フィールド構成）は latest.json と同一なので Discovery 側はそのまま解釈できる。
    note_output = {
        "generated_at":  now_iso,
        "valid_until":   valid_until_iso,
        "total_count":   len(note_candidates),
        "sources":       _build_sources_block(note_candidates, status_report),
        "candidates":    note_candidates,
        "product_line":  "note",
    }
    note_file_path = "trends/note_latest.json"
    with open(note_file_path, "w", encoding="utf-8") as f:
        json.dump(note_output, f, ensure_ascii=False, indent=2)
    if not os.path.exists(note_file_path):
        raise FileNotFoundError("Failed output to note target path.")
    print("Also deployed note_latest.json (note-prioritized ranking) for the note product line.")

    # --- debug feed（運用診断用・Discovery は読まない） ---
    from collections import Counter
    debug_output = {
        "generated_at": now_iso,
        "fetch_status": status_report,
        "raw_pool_total": len(raw_list),
        "filter": debug_info,
        "note_category_counts": dict(Counter(x["topic_category"] for x in note_candidates)),
        "b2b_topic_category_counts": dict(Counter(x["topic_category"] for x in b2b_candidates)),
        "b2b_top10": [
            {"title": x["raw_title"][:40], "source_category": x["source_category"],
             "raw_source_key": x.get("raw_source_key"), "topic_category": x.get("topic_category")}
            for x in b2b_candidates[:10]
        ],
        "note_top10": [
            {"title": x["raw_title"][:40], "source_category": x["source_category"],
             "raw_source_key": x.get("raw_source_key"), "topic_category": x.get("topic_category")}
            for x in note_candidates[:10]
        ],
    }
    debug_file_path = "trends/debug_latest.json"
    with open(debug_file_path, "w", encoding="utf-8") as f:
        json.dump(debug_output, f, ensure_ascii=False, indent=2)
    print("Also deployed debug_latest.json for diagnostics.")


if __name__ == "__main__":
    main()

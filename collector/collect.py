#!/usr/bin/env python3
"""
J-Net21 支援情報ヘッドライン（補助金・助成金）を集めて、生のままリポジトリに置く。

やること
  1. 全国＋47都道府県の検索結果ページを「掲載日の新しい順」に1ページずつ読み、
     記事IDを拾う。すでに index.json にあるIDばかりのページに当たったら、その県は終わり。
  2. 新しいIDの詳細ページを1件ずつ取り、articles/<ID>.html.gz に置く。
     script/style/nav/header/footer を落とす以外は手を入れない。項目の切り出しはしない。
  3. index.json（ID → URL・どの地域の一覧に出たか・取得日）と meta.json（実行記録）を更新する。

やらないこと
  ・制度名や期間の解釈、名寄せ、会社条件による絞り込み。全部 Claude 側（scripts/build_dataset.py）。
  ・並列取得。1件ずつ、間に待ちを入れる。J-Net21 に負荷をかけず、取りこぼしも出さないため。

失敗の扱い
  ・取得に3回失敗したページは failed.json に残し、最後に終了コード1で終わる。
    黙って件数が減ることはない。取れた分は index/articles に残る（ワークフロー側でコミットする）。

使い方
  python collector/collect.py                      # 日次。新着だけ
  python collector/collect.py --seed true          # 初回。全ページ読む
  python collector/collect.py --refresh-days 30    # 30日より古い記事を取り直す（上限 REFRESH_CAP 件）
"""

import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
ARTICLES = ROOT / "articles"
INDEX = ROOT / "index.json"
META = ROOT / "meta.json"
FAILED = ROOT / "failed.json"
DEBUG = ROOT / "debug"

BASE = "https://j-net21.smrj.go.jp/snavi2/results.php"
# href の書き方が絶対でも相対でも拾う（"/snavi2/articles/123" "articles/123" "./articles/123"）
ARTICLE_RE = re.compile(r"""href\s*=\s*["'][^"']*?articles/(\d+)""", re.I)

# GAS版と同じ検索条件：カテゴリ「補助金・助成金・融資」の種類「補助金・助成金」だけ
LIST_QUERY = {
    "search_exec": "1", "displaysort": "DESC", "displaycount": "10",
    "category": "2", "type[]": "3", "period": "1",
    "startDate": "", "endDate": "", "aggregateTag": "", "freeWord": "",
}

PREFECTURES = [
    ("全国", "00"), ("北海道", "01"), ("青森県", "02"), ("岩手県", "03"), ("宮城県", "04"),
    ("秋田県", "05"), ("山形県", "06"), ("福島県", "07"), ("茨城県", "08"), ("栃木県", "09"),
    ("群馬県", "10"), ("埼玉県", "11"), ("千葉県", "12"), ("東京都", "13"), ("神奈川県", "14"),
    ("新潟県", "15"), ("富山県", "16"), ("石川県", "17"), ("福井県", "18"), ("山梨県", "19"),
    ("長野県", "20"), ("岐阜県", "21"), ("静岡県", "22"), ("愛知県", "23"), ("三重県", "24"),
    ("滋賀県", "25"), ("京都府", "26"), ("大阪府", "27"), ("兵庫県", "28"), ("奈良県", "29"),
    ("和歌山県", "30"), ("鳥取県", "31"), ("島根県", "32"), ("岡山県", "33"), ("広島県", "34"),
    ("山口県", "35"), ("徳島県", "36"), ("香川県", "37"), ("愛媛県", "38"), ("高知県", "39"),
    ("福岡県", "40"), ("佐賀県", "41"), ("長崎県", "42"), ("熊本県", "43"), ("大分県", "44"),
    ("宮崎県", "45"), ("鹿児島県", "46"), ("沖縄県", "47"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 hojokin-collector/15")

WAIT_SEC = 1.0            # 1リクエストごとの待ち
RETRY_WAIT = (3, 10, 30)  # 失敗時の待ち（3回）
MAX_PAGES_SEED = 500      # 初回の1県あたり上限（10件/ページ）
MAX_PAGES_DAILY = 60      # 日次の1県あたり上限。既知IDで止まるので通常は数ページ
REFRESH_CAP = 300         # 1回の実行で取り直す古い記事の上限

STRIP_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "iframe")


# ──────────────────────────────────────────────────────────
#  HTTP
# ──────────────────────────────────────────────────────────
class FetchError(Exception):
    pass


def fetch(url):
    """成功すれば本文（str）。3回失敗したら FetchError。"""
    last = ""
    for attempt, wait in enumerate(RETRY_WAIT, 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=40) as res:
                if res.status >= 400:
                    raise FetchError(f"HTTP {res.status}")
                body = res.read().decode("utf-8", errors="replace")
            if len(body) < 2000:
                raise FetchError(f"本文が短すぎる（{len(body)}バイト）")
            time.sleep(WAIT_SEC)
            return body
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, FetchError) as e:
            last = str(e)
            print(f"    再試行{attempt}/{len(RETRY_WAIT)} {url} ({last})", flush=True)
            time.sleep(wait)
    raise FetchError(last)


def list_url(code, page):
    q = dict(LIST_QUERY)
    q["prefecture[]"] = code
    if page > 1:
        q["page"] = str(page)
    return BASE + "?" + urllib.parse.urlencode(q)


def article_ids(html):
    """検索結果ページに出てくる記事IDを、出現順・重複なしで返す。"""
    out = []
    for m in ARTICLE_RE.finditer(html):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def slim(html):
    """本文と無関係なタグを落とす。中身の解釈はしない。"""
    for tag in STRIP_TAGS:
        html = re.sub(rf"<{tag}\b[^>]*>[\s\S]*?</{tag}\s*>", " ", html, flags=re.I)
    html = re.sub(r"<!--[\s\S]*?-->", " ", html)
    return html


# ──────────────────────────────────────────────────────────
#  index / meta
# ──────────────────────────────────────────────────────────
def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


# ──────────────────────────────────────────────────────────
#  本体
# ──────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="false")
    ap.add_argument("--refresh-days", type=int, default=30)
    args = ap.parse_args()
    seed = str(args.seed).lower() == "true"

    ARTICLES.mkdir(exist_ok=True)
    index = load_json(INDEX, {})
    meta = load_json(META, {})
    failed = {"lists": [], "articles": []}
    started = time.time()
    today = now()

    # ── 1. 一覧を読む ──
    new_ids = {}          # id -> set(codes)  今回はじめて見たもの
    seen_this_run = {}    # id -> set(codes)  今回一覧で見たもの全部（既知も含む）
    max_pages = MAX_PAGES_SEED if seed else MAX_PAGES_DAILY

    for label, code in PREFECTURES:
        print(f"[一覧] {label}", flush=True)
        prev_ids = None
        pages_read = 0
        for page in range(1, max_pages + 1):
            try:
                html = fetch(list_url(code, page))
            except FetchError as e:
                failed["lists"].append({"地域": label, "page": page, "error": str(e)})
                print(f"  !! {label} {page}ページ目を取得できず: {e}", flush=True)
                break
            ids = article_ids(html)
            pages_read += 1
            if page == 1 and not ids:
                # 読めたのに記事リンクが1つも無い＝ページの形が想定と違う。
                # 黙って0件で終わらせず、HTMLを残して失敗にする
                DEBUG.mkdir(exist_ok=True)
                (DEBUG / f"list_{code}_p1.html").write_text(html, encoding="utf-8")
                failed["lists"].append({"地域": label, "page": 1,
                                        "error": "記事リンクが見つからない（debug/ にHTMLを保存）"})
                print(f"  !! {label} 1ページ目に記事リンクが無い。debug/list_{code}_p1.html を保存", flush=True)
                break
            if not ids or ids == prev_ids:
                break                      # 最後のページを越えた
            prev_ids = ids
            fresh = 0
            for i in ids:
                seen_this_run.setdefault(i, set()).add(code)
                if i not in index:
                    new_ids.setdefault(i, set()).add(code)
                    fresh += 1
            if not seed and fresh == 0:
                break                      # 既知のIDばかり＝新着は尽きた
        print(f"  {pages_read}ページ", flush=True)

    # 一覧で見たIDに地域コードを追記（全国と県の両方に出る制度は両方持つ）
    for i, codes in seen_this_run.items():
        rec = index.setdefault(i, {"url": f"https://j-net21.smrj.go.jp/snavi2/articles/{i}",
                                   "prefs": [], "first_seen": today, "fetched_at": ""})
        rec["prefs"] = sorted(set(rec.get("prefs", [])) | codes)

    # ── 2. 詳細を取る（新着 ＋ 古くなったもの） ──
    targets = list(new_ids.keys())
    refresh = []
    if args.refresh_days > 0:
        limit = (datetime.now(JST) - timedelta(days=args.refresh_days)).strftime("%Y-%m-%d")
        for i, rec in index.items():
            if i in new_ids:
                continue
            if not rec.get("fetched_at") or rec["fetched_at"][:10] < limit:
                refresh.append(i)
        refresh.sort(key=lambda i: index[i].get("fetched_at", ""))
        refresh = refresh[:REFRESH_CAP]
    targets += refresh
    print(f"[詳細] 新着 {len(new_ids)} 件、取り直し {len(refresh)} 件", flush=True)

    done = 0
    for n, i in enumerate(targets, 1):
        url = index[i]["url"]
        try:
            html = fetch(url)
        except FetchError as e:
            failed["articles"].append({"id": i, "url": url, "error": str(e)})
            print(f"  !! {i} 取得できず: {e}", flush=True)
            continue
        with gzip.open(ARTICLES / f"{i}.html.gz", "wt", encoding="utf-8") as f:
            f.write(slim(html))
        index[i]["fetched_at"] = today
        done += 1
        if n % 50 == 0:
            print(f"  {n}/{len(targets)}", flush=True)
            save_json(INDEX, index)        # 途中で落ちても進みを残す

    # ── 3. 記録 ──
    save_json(INDEX, index)
    have = sum(1 for i in index if (ARTICLES / f"{i}.html.gz").exists())
    meta.update({
        "最終実行": today,
        "モード": "初回全件" if seed else "日次",
        "一覧で見たID": len(seen_this_run),
        "新着": len(new_ids),
        "取り直し": len(refresh),
        "詳細取得成功": done,
        "index件数": len(index),
        "本文があるID": have,
        "本文が無いID": len(index) - have,
        "一覧取得失敗": len(failed["lists"]),
        "詳細取得失敗": len(failed["articles"]),
        "所要秒": int(time.time() - started),
    })
    save_json(META, meta)
    save_json(FAILED, failed)
    print(json.dumps(meta, ensure_ascii=False, indent=1))

    if failed["lists"] or failed["articles"]:
        print("!! 取得できなかったページがあります。failed.json を確認してください。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

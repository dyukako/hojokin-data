#!/usr/bin/env python3
"""
sources.json に登録した「入口ページ」から、同じサイト内を指定の階層までたどり、
たどり着いたページ（HTML・PDF）を生のまま pages/<地域>/ に保存する。

J-Net21 に載らない県・市・商工会議所の制度を拾うためのもの。
個別の補助金ページは登録しない。入口だけ登録し、あとは毎回たどり直す。
個別ページのURLが年度替わりで変わっても影響を受けない。

やらないこと
  ・ページの解釈、制度の切り出し。読むのは Claude（scripts/pages_text.py で本文にして読む）
  ・scope の外（別サイト、別階層）へ出ること。follow_external=true の入口だけ、外部リンクを1階層追う

失敗の扱い
  ・入口ページが取れなければ failed に記録し、最後に終了コード1（＝入口URLが変わった合図）
  ・入口より下のページが取れなくても止めない。failed に記録して続ける

使い方
  python collector/crawl_pages.py                 # sources.json の全地域
  python collector/crawl_pages.py --area 島根県   # 1地域だけ
"""

import argparse
import gzip
import hashlib
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
SOURCES = ROOT / "sources.json"
PAGES = ROOT / "pages"
PAGES_INDEX = ROOT / "pages_index.json"
PAGES_META = ROOT / "pages_meta.json"
PAGES_FAILED = ROOT / "pages_failed.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 hojokin-collector/15")
WAIT_SEC = 1.0
RETRY_WAIT = (3, 10)
MAX_PAGES_PER_SOURCE = 400      # 入口1つあたりの上限。超えたら scope が広すぎる
MAX_BYTES = 8 * 1024 * 1024     # PDF等の上限
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|svg|webp|ico|css|js|zip|xlsx?|docx?|pptx?|mp4|mp3)(\?|$)", re.I)
STRIP_TAGS = ("script", "style", "noscript", "iframe")


class FetchError(Exception):
    pass


def fetch(url):
    last = ""
    for attempt, wait in enumerate(RETRY_WAIT + (None,), 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/pdf,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=40) as res:
                ctype = (res.headers.get("Content-Type") or "").lower()
                body = res.read(MAX_BYTES + 1)
                final = res.geturl()
            if len(body) > MAX_BYTES:
                raise FetchError("サイズ上限超え")
            time.sleep(WAIT_SEC)
            return body, ctype, final
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, FetchError) as e:
            last = str(e)
            if wait is None:
                break
            time.sleep(wait)
    raise FetchError(last)


def links_of(html, base):
    out = []
    for m in re.finditer(r"""<a\b[^>]*href\s*=\s*["']([^"'#]+)["']""", html, flags=re.I):
        u = urllib.parse.urljoin(base, m.group(1).strip()).replace("&amp;", "&")
        u = u.split("#")[0]
        if not u.startswith("http") or SKIP_EXT.search(u):
            continue
        if u not in out:
            out.append(u)
    return out


def norm(u):
    """http/https と www の有無、末尾スラッシュの差を吸収して同一判定に使う"""
    p = urllib.parse.urlsplit(u)
    host = p.netloc.lower().removeprefix("www.")
    path = p.path or "/"
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    return f"{host}{path}?{p.query}" if p.query else f"{host}{path}"


def in_scope(u, scopes):
    n = norm(u)
    return any(n.startswith(norm(s)) for s in scopes)


def same_site(u, entry):
    return norm(u).split("/")[0] == norm(entry).split("/")[0]


def slim(html):
    for tag in STRIP_TAGS:
        html = re.sub(rf"<{tag}\b[^>]*>[\s\S]*?</{tag}\s*>", " ", html, flags=re.I)
    return html


def key_of(url):
    return hashlib.sha1(norm(url).encode()).hexdigest()[:12]


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def crawl_source(area, src, index, failed, today):
    entry = src["url"]
    scopes = src.get("scope", [entry])
    depth_max = int(src.get("depth", 1))
    ext_ok = bool(src.get("follow_external", False))
    seen = {norm(entry)}
    queue = [(entry, 0, None)]
    got = 0
    outdir = PAGES / area
    outdir.mkdir(parents=True, exist_ok=True)

    while queue and got < MAX_PAGES_PER_SOURCE:
        url, depth, parent = queue.pop(0)
        try:
            body, ctype, final = fetch(url)
        except FetchError as e:
            failed.append({"地域": area, "入口": src["name"], "url": url, "depth": depth, "error": str(e),
                           "入口自体": depth == 0})
            print(f"  !! {url} ({e})", flush=True)
            continue
        is_html = "html" in ctype or body[:200].lower().lstrip().startswith(b"<!doctype") or b"<html" in body[:500].lower()
        is_pdf = "pdf" in ctype or body[:5] == b"%PDF-"
        if not (is_html or is_pdf):
            continue
        k = key_of(url)
        if is_pdf:
            (outdir / f"{k}.pdf.gz").write_bytes(gzip.compress(body))
            title = ""
        else:
            html = body.decode("utf-8", errors="replace")
            m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, flags=re.I)
            title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
            with gzip.open(outdir / f"{k}.html.gz", "wt", encoding="utf-8") as f:
                f.write(slim(html))
        index[k] = {"url": url, "final_url": final, "地域": area, "入口": src["name"], "depth": depth,
                    "title": title, "type": "pdf" if is_pdf else "html", "parent": parent, "fetched_at": today}
        got += 1

        if is_pdf or depth >= depth_max:
            continue
        for u in links_of(html, final):
            n = norm(u)
            if n in seen:
                continue
            if in_scope(u, scopes):
                seen.add(n)
                queue.append((u, depth + 1, url))
            elif ext_ok and depth == 0 and not same_site(u, entry):
                # 入口から直接リンクされた外部ページだけ（1階層）。そこから先は追わない
                seen.add(n)
                queue.append((u, depth_max, url))
    if got >= MAX_PAGES_PER_SOURCE:
        print(f"  !! {src['name']}: 上限{MAX_PAGES_PER_SOURCE}ページに達した。scope が広すぎる", flush=True)
        failed.append({"地域": area, "入口": src["name"], "url": entry, "error": "ページ数上限", "入口自体": False})
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", default="")
    args = ap.parse_args()

    sources = load(SOURCES, {})
    index = load(PAGES_INDEX, {})
    meta = load(PAGES_META, {})
    failed = []
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    started = time.time()
    counts = {}

    for area, srcs in sources.items():
        if area.startswith("_") or (args.area and area != args.area):
            continue
        # その地域の前回分は捨てて取り直す（消えたページを残さないため）
        for k in [k for k, v in index.items() if v.get("地域") == area]:
            index.pop(k)
        for f in (PAGES / area).glob("*.gz") if (PAGES / area).exists() else []:
            f.unlink()
        counts[area] = 0
        for src in srcs:
            print(f"[{area}] {src['name']}", flush=True)
            n = crawl_source(area, src, index, failed, today)
            counts[area] += n
            print(f"  {n}ページ", flush=True)
        save(PAGES_INDEX, index)

    entry_fail = [f for f in failed if f.get("入口自体")]
    meta.update({"最終実行": today, "地域別ページ数": counts, "失敗": len(failed), "入口の失敗": len(entry_fail),
                 "所要秒": int(time.time() - started)})
    save(PAGES_META, meta)
    save(PAGES_FAILED, failed)
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    if entry_fail:
        print("!! 入口ページが取れていません。sources.json の URL を確認してください。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

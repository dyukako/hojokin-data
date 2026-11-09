#!/usr/bin/env python3
"""
hojokin-data の knowledge/known_programs.md に書いた「確認済みの回・版」と、同じく hojokin-data の pages/全国/ に今ある
公募要領のファイル名を比べ、回・版が変わっていたら警告を出す。

known_programs.md の制度ごとに、次の形のコメントを置く（1行）。
  <!-- check_known: name=制度名 | url_pattern=正規表現 | confirmed=回=○ 版=○ -->
  ・url_pattern … pages_index.json の url（ファイル名部分）に当てる正規表現。| で複数書ける
  ・confirmed  … known_programs.md の内容を確かめたときの回と版。「回=20 版=8」の形で書く

ファイル名から回と版を読む（2つの書き方に対応）
  ・r6_koubover8_ip20.pdf          → 版8・回20（商工会議所地区）
  ・20_通常枠_公募要領_第8版.pdf   → 回20・版8（商工会地区）
過去の回のPDFも pages/全国 に残っているので、当たったPDFのうち「いちばん新しい回・版」だけを比べる。

判定
  ・url_pattern に当たるPDFが pages/全国 に無い  → 「見つからない」（収集できていない。WebFetchで確認）
  ・いちばん新しい回・版が confirmed より新しい  → 「更新あり」（新しい公募要領を読んで判定する。known_programs.md は毎週の自動作業が更新）
  ・同じ                                          → OK

使い方
  python3 -I knowledge/check_known.py <hojokin-dataのパス>
  （スキル hojokin-screening の scripts/check_known.py はこのファイルを呼ぶだけ。判定ロジックはここだけに置く）
終了コード：更新あり・見つからないが1件でもあれば 1
"""
import json
import re
import sys
from pathlib import Path

TAG = re.compile(r"<!--\s*check_known:(.*?)-->")


def parse(md):
    out = []
    for m in TAG.finditer(md):
        d = {}
        # url_pattern の中の | を区切りと誤認しないよう、キー= の位置で切る
        body = m.group(1)
        keys = list(re.finditer(r"(name|url_pattern|confirmed)\s*=", body))
        for i, k in enumerate(keys):
            end = keys[i + 1].start() if i + 1 < len(keys) else len(body)
            val = body[k.end():end].strip()
            if val.endswith("|"):
                val = val[:-1].strip()
            d[k.group(1)] = val
        out.append(d)
    return out


def kai_han(name):
    m = re.search(r"koubover(\d+)_[a-z]+(\d+)", name)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.search(r"(\d+)_.*?第(\d+)版", name)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    data = Path(sys.argv[1])
    known = data / "knowledge" / "known_programs.md"
    if not known.exists():
        print(f"[見つからない] {known}：hojokin-data に knowledge/known_programs.md が無い。fetch_repo.sh で取り直すこと")
        sys.exit(1)
    idx = json.loads((data / "pages_index.json").read_text(encoding="utf-8"))
    pdfs = sorted({v["url"].rsplit("/", 1)[-1] for v in idx.values()
                   if v.get("地域") == "全国" and v.get("type") == "pdf"})
    pdfs_dec = {}
    from urllib.parse import unquote
    for p in pdfs:
        pdfs_dec[unquote(p)] = p

    bad = 0
    for e in parse(known.read_text(encoding="utf-8")):
        pat = re.compile(e.get("url_pattern", "$^"))
        hits = sorted(n for n in pdfs_dec if pat.search(n))
        name = e.get("name", "?")
        m = re.search(r"回\s*=\s*(\d+)\s*版\s*=\s*(\d+)", e.get("confirmed", ""))
        conf = (int(m.group(1)), int(m.group(2))) if m else None
        vers = sorted({kh for kh in (kai_han(h) for h in hits) if kh})
        if not vers:
            print(f"[見つからない] {name}：pages/全国 に公募要領が無い。公式サイトをWebFetchで確認すること")
            bad += 1
            continue
        latest = vers[-1]
        files = [h for h in hits if kai_han(h) == latest]
        if conf is None:
            print(f"[設定不備] {name}：confirmed を「回=○ 版=○」の形で書くこと")
            bad += 1
        elif latest > conf:
            print(f"[更新あり] {name}：確認済みは第{conf[0]}回 第{conf[1]}版、今あるのは第{latest[0]}回 第{latest[1]}版（{', '.join(files)}）。"
                  f"判定の前に新しい公募要領・参考資料・Q&Aを読み、そちらに基づいて判定すること（known_programs.md は毎週の自動作業が更新する。担当者に書き換えを頼まない）")
            bad += 1
        else:
            print(f"[OK] {name}：第{conf[0]}回 第{conf[1]}版（{', '.join(files)}）")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

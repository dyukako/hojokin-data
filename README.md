# hojokin-data

J-Net21「支援情報ヘッドライン」の補助金・助成金記事を、GitHub Actions が毎日集めて
**生のまま**置いておくリポジトリ。項目の切り出し・名寄せ・会社条件による絞り込みは
一切しない。それは Claude のスキル `hojokin-screening` 側の仕事。

```
.github/workflows/collect.yml   毎日 05:30 JST に collector/collect.py を実行してコミット
collector/collect.py            収集スクリプト（Python 標準ライブラリのみ）
articles/<ID>.html.gz           記事ページの HTML（script/style/nav/header/footer を落とした以外は原文）
index.json                      ID → URL、どの地域の一覧に出たか（JIS コード。00 = 全国）、初出日、取得日
meta.json                       最終実行の記録（件数・失敗数・所要時間）
failed.json                     取得に失敗したページ。空でなければ Actions のジョブは失敗で終わる
```

## 初回

Actions タブ → 「J-Net21 収集」 → Run workflow → `seed` に `true`。
全国＋47都道府県の全ページを読み、記事を1件ずつ取る（1秒間隔）。数千件あるので1〜3時間。
終わったら以後は放置でよい。毎朝、新着だけ追加される。

## 失敗したとき

ジョブが赤くなる＝どこかのページが3回とも取れなかった。取れた分はコミット済み。
`failed.json` に URL が残っているので、Run workflow をもう一度押せば埋まる。

## Claude 側

```
bash scripts/fetch_repo.sh repo
python3 scripts/build_dataset.py repo --pref 島根県 -o data/
python3 scripts/parse_sheet.py data/補助金_全国.json data/補助金_島根県.json -o out/
```

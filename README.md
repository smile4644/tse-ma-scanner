# tse-ma-scanner

無料運用を前提に、前営業日の東証普通株・出来高上位1000銘柄について、
日足・週足・月足の9本のSMAを判定するGitHub Actionsスキャナーです。

## 判定対象
- 日足: 5 / 25 / 75 SMA
- 週足: 13 / 26 / 52 SMA
- 月足: 12 / 24 / 60 SMA
- MA方向: ↑ / → / ↓
- ローソク足位置: 上 / 接触 / 下
- 9/9 = 完全一致
- 8/9 = 準完全一致
- 7/9 = 初動候補

## 自動実行
- 平日 10:35 JST: 午前版（10:30スナップショット、`session=noon`）
- 平日 15:40 JST: 大引け版（15:30確定値）

## 出力
- `data/universe_top1000.csv`
- `results/latest_noon.json`
- `results/latest_close.json`
- `results/archive/YYYY-MM-DD_noon.json`
- `results/archive/YYYY-MM-DD_close.json`

## 注意
株価データはYahoo Financeを `yfinance` 経由で取得します。
無料・非公式データのため、遅延・欠損・取得制限が起きる可能性があります。

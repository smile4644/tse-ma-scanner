# 空売り候補の制度信用・貸株・決算リスク確認

## 自動化の範囲

買いスキャナー成功後に `TSE MA Short Scanner` が起動し、`short_evidence_collect.py` が以下の**公開データ**を取得します。

- [日本証券金融（JSF）DATA](https://www.taisyaku.jp/download/): `meigara.csv`（東証分の貸借区分）、`zandaka.csv`（融資・貸株残高）、`shina.csv`（品貸料率）、`seigenichiran.csv`（制限措置）
- [SBI証券・本日の注意銘柄](https://search.sbisec.co.jp/v2/popwin/attention/stock/margin.html): 公開されている「貸株注意喚起」「新規売停止」。**未掲載は売建可の証拠ではありません**。

ファイル冒頭の表題・日付行を除去してヘッダーを検証し、情報が古い・列が変わった・取得に失敗した場合は未確認とします。日証金データの基準日とWeb取得日は区別します。

## 誤判定を防ぐ条件

- 日証金の貸借区分「非貸借」は除外。公開の貸借区分「貸借」は、SBIの実際の注文可否を証明しません。
- 日証金の品貸料率が正、貸株残高が融資残高を超過、貸株注意喚起・申込停止・新規売停止などの否定的証拠を確認した銘柄は除外します。
- 逆日歩・株不足の「直近5件」は**異なる貸借申込日**の履歴がそろったときだけ確定します。同じCSVを繰り返し取得しても5日分とは数えません。
- 公開情報だけから `sbi_system_sellable=yes`、`jpx_lending_eligible=yes`、`sbi_public_alert_status=none`、`next_earnings_date` を推測・自動確定しません。
- **必要証拠が1項目でも欠ける銘柄は `verification_required` とし、`order_ready=false` を保持します。**

## 人による最終確認（毎回の発注前）

`data/short_trade_checks_YYYY-MM-DD.csv` に銘柄コードごとに、確認結果・確認日・根拠となる情報源を記録します。例（CSVは既存の全ヘッダーを保持してください）：

| 項目 | 入力する確認 |
| --- | --- |
| `sbi_system_sellable` / `sbi_checked_date` / `sbi_source` | SBI取引画面で「制度信用・新規売」を当日確認。一般信用・HYPER空売りの可否とは分ける |
| `jpx_lending_eligible` / `jpx_checked_date` / `jpx_source` | [JPX制度信用・貸借銘柄一覧](https://www.jpx.co.jp/listing/others/margin/)と当日までの指定・取消しを確認 |
| `sbi_public_alert_status` / `sbi_public_alert_checked_date` / `sbi_public_alert_source` | SBI公開規制一覧を当日確認。未掲載の場合も自動では `none` にしない |
| `earnings_checked_date` / `earnings_source` / `next_earnings_date` | [JPX決算発表予定日](https://www.jpx.co.jp/listing/event-schedules/financial-announcement/)または**発行会社のIR**で次回発表を照合。予定が不明なら空欄 |
| `jsf_restriction`, `jsf_last5_reverse_fees_yen`, `jsf_last5_shortage` | 日証金公式情報と直近5営業日の記録。公開データの曖昧な欄は空欄のまま |

次回決算予定が対象日から**7日以内**の場合は除外します。公表予定が変更され得るため、決算カレンダーの推定日や未確認日を安全と扱いません。

## 出力ファイル

- `results/short_evidence/YYYY-MM-DD_checks_noon.csv` / `*_checks_close.csv`: 自動証拠と手入力をマージした安全判定入力
- `results/short_evidence/YYYY-MM-DD_noon_review.csv` / `*_close_review.csv`: 銘柄別・資料別の判定一覧
- `results/short_evidence/YYYY-MM-DD_noon_status.json` / `*_close_status.json`: 件数と公式データ取得結果
- `results/short_evidence/YYYY-MM-DD_noon_snapshot.json` / `*_close_snapshot.json`: 候補銘柄の最小限の日証金履歴（公式CSV全体は保存しない）
- `results/latest_short_noon.json` / `latest_short_close.json`: 発注前の安全性フラグを含む正式JSON

GitHub Actionsの [TSE Short Evidence CI](https://github.com/smile4644/tse-ma-scanner/actions/workflows/short_evidence_ci.yml) には、日証金・SBI情報の取り込みスモークテストとCSVレビューの成果物を置きます。実際のスキャンは [TSE MA Short Scanner](https://github.com/smile4644/tse-ma-scanner/actions/workflows/tse_ma_short.yml) が買いスキャン完了後に起動します。

### 制約

GitHub ActionsはSBI証券のログイン後画面を参照できず、口座ごとの制度信用売建可否を完全自動確認することはできません。またJPXが公開する決算発表予定一覧は網羅的ではないため、各社IRの確認が必要です。スクリーニング結果はあくまで研究候補であり、実際の売建には最新の注文画面確認が必要です。

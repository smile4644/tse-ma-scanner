# 空売り証拠データの取扱い

- SBI制度信用新規売建の可否はSBI証券の実際の取引画面で当日確認する（公開の貸借銘柄リストから肯定判定しない）。
- 日証金の公式CSV（貸借銘柄、残高、品貸料率、制限措置）を確認する。欠損と遅延は「未確認」とする。
- 決算予定は発行会社IR／JPX公開情報で確認し、対象日から7日以内なら候補から除外する。
- どれか一つでも必要な証拠が欠けている場合、order_ready=falseを維持する。

参照先: https://www.taisyaku.jp/download/ / https://www.jpx.co.jp/listing/event-schedules/financial-announcement/ / https://www.sbisec.co.jp/

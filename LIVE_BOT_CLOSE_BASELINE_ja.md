# Live Bot Close Baseline

この文書を、新規live botと既存botのbot-managed close実装に対する正本とする。戦略固有のSL/TP、Close By、部分決済よりも、固定hold・シグナルclose・再開後closeの共通安全契約を定義する。

## 時刻と休場

- 固定UTCの開閉場時刻をclose可否判定に使わない。夏時間、通常週末、祝日、短縮取引をbot内の曜日・時刻表で推測しない。
- 平日の定期休場と、週末・休日の長期休場は監査上別区分とする。ただしlive closeはどちらもbrokerのfresh quoteと注文retcodeで再開を判定する。
- close期限後も、quote timestampが期限前、欠損、または前回評価以下ならstale quoteとして待機する。wall clockのpollだけで市場再開とみなさない。
- 固定時刻の週末precloseは標準機能にしない。必要な場合はbroker session calendarを別途凍結し、DST・祝日・短縮取引を含む独立backtestを通す。

## Spread Guard

- 期限後の最初のfresh quoteが許容spread以内なら即closeする。
- 一度でも許容spreadを超えた場合は、fresh quoteによる許容spread判定が3 poll連続するまで待つ。同一quoteの再読込を連続回数へ加算しない。
- 最初のfresh wide quoteから30分経過した後のfresh quoteでは、spreadにかかわらずcloseを試す。
- market-closed retcode `10018`はno-fillとしてposition/stateを保持し、60秒後に再試行する。その他の未確認・所有権不明・不正応答は従来どおりfail closedとする。
- spread上限は銘柄別にdev quote分布からPnLを見る前に固定する。USTECのbot26-29は`260 points`、`point_size=0.01`を使用する。別銘柄へ260を無条件転用しない。

## State and Audit

- defer開始時刻、最後に評価したquote timestamp、stable count、retry時刻をposition stateへ保存し、再起動後もposition reconciliationを優先する。
- `DEFER`、`DEFER_TIMEOUT`、`RESUME`、market-closed retryをtrade logへ残す。
- 新規botのself-testには、stale quote待機、wide spread待機、同一quote非加算、3回安定、30分timeout、market-closed retryを含める。

## Current Adoption

- bot26: 独立runnerで本契約を実装する。
- bot27: bot28・bot29が利用するbase runnerとして本契約を実装する。
- bot28 / bot29: composeでmountされるbot27 base runnerを利用し、各paramsで同じUSTEC上限を固定する。

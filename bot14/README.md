# bot14

Move-catcher記事の二系統反転構造を実装した正式bot14。

## ロジック

- AUDCHF、AUDUSD、GBPUSDの3通貨を有効化。EURCHFは`enabled=false`。
- Aの初回建値から0.5W動いた実行可能価格で、BをAと同方向に初期配置。
- 初期化後はA/Bとも、TP後は方向反転、SL後は同方向で再エントリー。
- 初期化後の再エントリーも、他方の建値から原則0.5Wの距離へ到達するまで待機する。
- 初期Bは他方の建値から0.5W±許容差に制限。稼働後は常時保有を優先し、ずれは警告する。
- 同方向ペアを`CAPITAL`、上側SHORT/下側LONGを`PROFIT`として状態保存。
- A/Bはそれぞれ独立した記事準拠の分解モンテカルロ数列を持つ。
- 発注後のSL/TP同期に失敗した場合はpositionをstateへ保持し、同期完了まで追加発注を停止する。
- state上のpositionがMT5から消えた場合、現在価格でDMCを更新せず、deal履歴との照合完了まで追加発注を停止する。
- 発注要求前にrequest ID付き`pending_open`を原子的保存し、応答未確認時は自動再送しない。
- 再起動時の未解決`pending_open`は、request commentが完全一致するMT5 positionだけを自動採用する。
- 決済要求前に`pending_close`を保存し、確定応答後だけDMC・次方向・position削除を一括保存する。
- MT5同期失敗時は同cycleの価格ベースexit判定へ進まない。
- 週末強制決済を有効にした場合は、確認済みpositionを1件ごとにstateへ保存する。
- stateは一時ファイルへflush/fsync後、`os.replace`で原子的に保存する。
- state破損・保存失敗時はbridge接続、OPEN、CLOSE、同cycle処理をfail-closedで停止する。

## 初期設定

- W: 有効3通貨43.0 pips（記事基準）。EURCHFにも復帰時用の43.0 pips設定を保持
- 初期オフセット: 0.5W
- ペア距離許容差: 0.6 pipsまたはspread×2の大きい方
- 初期モンテカルロ数列: `[0, 1]`
- lot倍率: 0.01、bet units上限なし
- 最大spread: 0.9 pips（上限と同値を許可）
- EURCHFは`spread < 1.4`診断でPF1.098、MDD346.1 pipsとなり、曲線も不安定だったため無効化
- 週末強制決済とニュース回避: 無効
- ライブ起動許可: 有効（500 USDデモ口座forward専用）
- 無制限bet unitsの明示許可: 有効（デモ口座で破産耐性を観測するstress条件）

## 起動前の注意

- `live_config.py`は認証情報を含むため、このフォルダには同梱していない。既存運用と同じ形式で別途配置する。
- 現在のGit設定は`live_trading_enabled=true`かつ`allow_unbounded_bet_units=true`。500 USDデモ口座forward専用で、実口座へ流用しない。
- `max_bet_units=0`のためDMC bet unitsは無制限。brokerのmargin・volume上限までlotが増え得るstress条件であり、安全性を保証しない。
- stateは`state/s14_bot_state.json`へ保存する。`state/`をDockerでディレクトリmountし、同一ディレクトリ内の原子的`os.replace`を可能にする。
- `state/`、`logs/`、取引CSVは初回起動時に新規作成する。v2からstateをコピーしない。
- 旧単一通貨stateまたはversion 3以外のstateを検出した場合は自動移行せず、安全停止する。
- 旧実装の資金管理stateは互換性がないため自動移行せず、新しい`[0,1]`数列から開始する。
- bet units上限やbrokerのvolume上限に達した場合、数列を壊さないため新規発注を停止する。
- 記事準拠設定はlot上限がなく、数列長期化時に必要証拠金が増える。ライブ稼働前にdev検証を必須とする。
- コード作成時点ではライブ発注テストを行っていない。
- deal履歴によるmissing positionの自動照合は未実装。`reconciliation_required`発生時はライブ運用者による確認が必要。
- order履歴によるOPEN timeoutの自動照合も未実装。`pending_open`が残った場合は自動再送せず照合待ちで停止する。
- Magic numberはA=`140034`、B=`140035`。

## Broker側TP/SL決済の照合

- MT5のserver-side TP/SLは、Pythonの1秒監視より先にpositionを決済する場合がある。
- state上のticketがbulk position一覧から消えた場合、同じticketを`POSITION`で個別再照会する。
- 個別再照会も明示的に`POSITION_NOT_FOUND`を返し、かつ現在の決済側価格が保存済みTP/SL境界へ到達している場合だけ、保護注文によるWIN/LOSEとして確定する。
- WINなら次方向を反転、LOSEなら同方向を維持し、DMC・position削除・次方向を一括保存する。
- Bridge timeout、個別照会でpositionが残る場合、TP/SL境界外、手動決済の可能性がある場合は自動確定せず、従来どおりfail-closedで停止する。
- 修正導入前から停止していたGBPUSDのA/B ticketは、`confirmed_missing_position_reconciliations`でsymbol・Bot種別・ticket・方向を厳密一致させ、初回起動時に一度だけ反映する。反映前stateは`state/`内へbackupする。

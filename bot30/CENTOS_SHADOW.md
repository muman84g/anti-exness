# bot30 CentOS sampled-current shadow v001

この入口は `bot30_shadow.py`、専用EAは `BotBridge_s30_readonly.mq5`、compose serviceは `exness-bot-30`。legacy `live_s30_bot.py` と `BotBridge_s30.mq5` はS23由来の履歴として保持し、新serviceから選びません。

## 制御と観測範囲

固定logical controlは `R3_CURRENT_QUEUE_CONTROL_V001`、physical factoryは `PRIORITY_R3_NORESET_V001`、engineは `H_BE_P2_PRIORITY01_V001`、lifecycleは `L_RESET`、CloseBy installは0です。native sourceは既受入source005から同一bytesでコピーし、`source_bindings.json`で照合します。新stream harnessとexecution bindingは別source identityです。旧source005の人工試験結果を新runnerの検証結果へ読み替えません。

250ms serial INFO pollで観測した現在のBid/Askを使用する `SAMPLED_CURRENT_BID_ASK_SHADOW_V001` です。未観測のpoll間価格経路は不明であり、full ordered-tick replay、実約定再現、経済採用、live readinessの証明ではありません。同一 `(time_msc,Bid,Ask)` はconsumeせず、同時刻の価格変更や逆行は拒否します。broker inventoryとvirtual native inventoryは別管理です。

初回の受入quoteのevent timestampをonline activationとして保存し、completed Bid M1を因果順に作って80本のcold warmupを通ります。過去のOct08 fixture activation、人工admissions/eventplan、手動position seedは通常入口で使いません。nativeのquote/signal/bar時計を保ち、market orderが生成された時点の実decision cutoff＋100msへeligibilityを束縛します。その時刻より厳密に後、かつ後のcursorのquoteだけをvirtual fillに使用します。実注文は発注しません。

## 時計とprovider binding

対象providerはExnessに限定します。公式[MetaTraderのtimezone](https://get.exness.help/hc/en-us/articles/360014390760-What-is-the-default-timezone-set-for-MetaTrader)と[取引時間の説明](https://get.exness.help/hc/en-us/articles/4405235684498-Instrument-trading-hours)がserver UTC+0を示しています。専用EAの `SymbolInfoTick` raw `time_msc` の無変換返却と、`EXNESS_MT5_SERVER_UTC0_V001` の非秘密bindingを組み合わせます。他providerへ流用しません。server UTCにはoffset0／DST変換なしを適用します。銘柄の取引時間にあるDSTとは区別します。

`clock_contract_v002.json` はevent／ingested／available／decision cutoffをUTC epoch millisecondsで分離します。broker original release instantは未観測なのでnullです。受信時刻はpublication upper boundとして別fieldに記録し、exact releaseと認定しません。strict response／identity／binding検証後にavailableを直接観測します。

deployment前に運用者が非秘密のexact company/serverを確認し、read-only mountの `config/shadow_v001.json` とEA inputsへ同じbinding ID/digest、expected company/serverを設定する必要があります。prefix推測や口座ID／パスワードの保存は行いません。初期company/serverは空です。unknown/mismatchは `not_evaluated` となり、native cursorやjournalを進めません。正しいbindingとfresh feedが到来すれば再開します。operator config変更は新しいconfig identityになり、既存非空journalと一致しない場合は停止します。これは実接続済みという意味ではありません。

## 保存と復旧

専用stateは `state/s30_shadow`、ログは `logs/s30_shadow`、IPCは `cmd_s30_readonly.txt`／`res_s30_readonly.txt`。OS lockで同一stateの複数runnerを拒否します。journalはtyped quoteとclock、固定run identity、full business digestを保存します。1 record最大4096 bytes、100000 records／128MiB上限です。上限到達時は履歴を保持して停止し、R3 NORESET stateを捨てたりflat resetしたりしません。

restartは同一source/config/activationでfresh controlを作り、journalの固定順prefixをreplayしてbusiness digestを照合します。native pickleやdiagnostic snapshotのinverse restoreは使いません。journal appendが完了しcheckpointだけ遅れた場合は、完全なtailをreplay・照合してcheckpointを進めます。partial／malformed tail、pin mismatch、state mismatchは保存したまま停止します。欠落quoteを生成しません。修復や別runへの切替えを自動で行いません。

## Dockerと運用前提

bot30専用derived image `exness-bot30-shadow:v001` は既存 `exness-bot:latest` のWine/MT5と `/app/entrypoint.sh` を再利用します。Linux runnerの `psutil==7.2.2` だけを専用dependency directoryへ追加します。build contextは `.dockerignore` のallowlistと明示COPYに限定し、credentials、startup.ini、legacy source、raw market data、研究ログを新contextへ含めません。base imageは既存運用imageなので、Git package検証はbase imageの内容やdigestを証明しません。実build時に信頼済みbaseを確認してください。

directoryの例外直後にその直下の未知childを再除外し、既知sourceだけを個別に戻します。directory例外だけで未知fileまで含めないための指定です。[Dockerのbuild context仕様](https://docs.docker.com/build/concepts/context/)と[公式pattern matcher](https://github.com/moby/patternmatcher/blob/main/patternmatcher.go)のparent-match規則に基づきます。今回Docker buildは未実施です。

bot30 derived imageは `PYTHONDONTWRITEBYTECODE=1` を宣言し、共有entrypointの `python3` に継承します。factoryが要求するbytecode書込み無効化をbase imageの未確認環境へ委ねません。private fixtureは同じ条件を `-B` と環境変数で固定します。

composeは既存serviceのbytesを保持してbot30を追加します。DISPLAY123／VNC5924／noVNC6104は追加時点の全service定義と衝突確認します。既存entrypointのGUI起動機構を使います。`startup.ini` は運用者がGit外で配置するread-only mountであり、今回内容・stat・hash・copyを扱いません。新EAのcompile/attachと実provider確認は別工程です。既存機構でEA attachは手動です。

今回静的に確認した共有 `entrypoint.sh` は6702 bytes／SHA256 `78f748dd5c24361623150be3a97e6de7197a3213abdad9c024aad57ba42d33b6` です。Wineのcompile呼出しは `timeout 90s ... || true` で非zeroを吸収し、selected sourceをcopyしたときに旧同名EX5を除去してから、新selected EX5の存在をrunner起動前に確認します。EX5がなければlogを表示して停止します。logの成功行はparseしません。保存済みWindows compileのOS終了コード1はWineの終了コードや起動中断を証明せず、Windowsでのlog／EX5確認もWine compile・MT5 load・IPC成功を証明しません。共有entrypointは変更していません。Git／実build時にはこの外部依存の版も確認が必要です。

EAとPythonのcommand surfaceはread-only ECHO/CAPS/INFO/POSITIONS/ORDERSだけです。CTrade／OrderSend／OPEN／CLOSE／CloseByを実装せず、unknown opcodeを拒否します。CloseBy capabilityの観測は発注対応を意味しません。OPEN/CLOSEへの代替も行いません。

固定NO_ORDER factoryは空outcomesのdefault `ExecutionPlan`を使い、quoteだけではexit reject/retryへ到達しません。retryからnative `_new_order`、共通sink emit、current accepted quoteのcutoff+100msを新order IDへ一度bindする経路は静的確認の対象です。retryの動的受入は未実施です。人工admission、state seed、outcomes/event plan注入で自然到達を装いません。自然なvirtual entry/stop/closeと非空prefix restartの試験とは証拠範囲を分けます。実order／CloseByへの接続はありません。

今回のGit公開とCentOSのpull/build/起動・MT5接続・EA attach・売買開始は別の工程です。新releaseの単位はrunner＋EA＋compose＋pins/configであり、runnerだけをlegacyへ戻すrollbackは行いません。旧accepted source005は別入口として保持します。

## 検証状態

今回の公開判定は `GIT_PACKAGE_ONLY` です。一体sourceの静的整合と非秘密allowlistを対象とし、Windows fullsuiteのPASSや機能・runtime・長時間memory leak・経済採用のGOを認定しません。candidate002は自然なentry/closeを含むmemory warmupとrepeat1後、一時ファイル削除とobserverのstatが競合して停止しました。candidate003は監視を限定修正した新identityですが、最初のwarmup quote後のcheckpoint `.new` から正本への `os.replace` がWindows `WinError 5` で失敗し、suiteは未完了です。原因は不明です。非nativeの最小比較でもobserverなしで同じ置換エラーを再現しましたが、AVやOSなどの実原因を確定した証拠ではありません。製品保存処理はwrite/flush/fsyncのfile contextを退出してからreplaceし、readerもcloseする経路です。製品retryや保存のfail-closedを緩めず、失敗・部分結果のreceiptを保持します。

CentOSのbuild・Wine compile・MT5 load・feed binding・非空state restartは未検証で、運用者の確認対象です。Gitへのpushだけが今回の完了境界であり、CentOSでの起動や売買開始を含みません。実order／CloseByは未接続です。今回のdoc-only更新では新しいnative runを実施せず、不変の実行source/pinsと既存静的reviewを再利用します。



新sourceの局所試験、独立review、freeze receiptは完成時のpublication manifestを正本とします。Docker daemonを起動していないためDocker/Wine統合実行とCentOS実機は未測定です。readonly EA source 8413 bytes／SHA256 `f43e0b0f1419b672aec9b4dd500386934353575208977d8dac683b5a730995f5` のprivate copyをMetaEditorでcompileし、保存済みlogに0 errors／0 warnings、private EX5生成を確認しました。直接OS終了コードは1で、その意味を確認できる公式根拠は未取得です。log診断成功・生成物確認と、終了コードの意味／MT5ロード・IPC動作を区別します。shared compiled EAは変更せず、実attach・口座接続は行っていません。source変更時はこの結果を再利用しません。経済比較・PnL／件数再現・採用条件は今回のpackage検証で認定しません。

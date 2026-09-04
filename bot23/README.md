# Bot23 integrated inventory and independent session overlays

## CLOSE claim recovery v33 (2026-09-04 local re-audit)

The EA must never execute a recovered CLOSE claim again. It returns the
correlated `ERR|CLOSE_RESULT_UNRESOLVED` receipt and leaves Python to reconcile
the owned POSITION/CLOSEDEAL lifecycle. The Python CLOSE submission marker is
retained across restart; the receipt is not a definitive no-fill rejection.
Normal first-time CLOSE execution and read-only claim recovery are unchanged.

EA source/binary, `EXPECTED_BRIDGE_VERSION` and `expected_bridge_version` must
be deployed together as `2026-09-04-s23-close-claim-v33`. A v32 EA is rejected
by the updated preflight. Compilation and local tests are not deployment proof.

## Local IPC recovery correction (2026-09-04)

- Correlated responses wait for EA claim cleanup within the existing response
  timeout. A confirmed result stays confirmed if cleanup stalls; Python never
  deletes the EA claim or publishes behind it.
- Definitive unpublished OPEN errors clear only their new submission receipt and
  create a recoverable inventory block, not a permanent lane disable. Unknown
  OPEN results remain consumed and require reconciliation before any resend.
- Legacy `ipc_open_not_published` blocks may clear after two consecutive complete
  flat position/order queries only with an allowlisted error, no basket/close
  intent and no pending OPEN fields. Unknown evidence remains blocked.
- ZA routing releases a reservation only when every lane did nothing and lane
  readiness or a final entry/add guard proves a transient pre-submission failure. The next poll checks
  the latest confirmed bar and all current admission/staleness gates again. This
  is not historical catch-up, a guaranteed fill, or an exemption from strategy
  restrictions. A consuming lane or ambiguous submission retains the reservation.
- Normal independent lane entries/adds are preserved. No strategy thresholds,
  quantities, switches, ownership namespace or deployment files changed.
- Tests: `test_s23_ipc_completion.py`, `test_s23_ipc_recovery.py` and the existing
  no-order regression suite. Local tests do not prove CentOS has this version.

Bridge v32 correction (2026-09-04): POSITIONS/ORDERS may read retired magic
200023 solely for cutover inventory checks. Executable ownership remains
230023-230044; legacy inventory is not adopted or tradable. Nonempty or failed
legacy queries still block startup. Runner and params require v32; compile and
attach the matching EA before restarting. Local verification is not deployment.

Close-ledger durability re-audit (2026-09-04): an existing confirmed-deal row is
filesystem-synced again, including its parent directory, before replay can consume
position state. A readable row from an interrupted fsync is not durability proof.
Strategy parameters, credentials and runtime state are unchanged.
The repeated audit also rejects malformed CSV quoting before close replay,
while preserving valid quoted multiline fields.

## Q01 completed-M5 variance-ratio release（採用・ローカル有効）

固定済み`Q01_variance_ratio_release`を、既存21レーンと分離したlane 22
（magic 230044、comment `s23_q01_l1`）へ実装しています。ローカル候補は
`bot23-integrated-session-vwap-on-t0530-edge-on-q01-v008`です。Q01の判定は有効ですが、
既存bot23の共通live設定とは別に`q01_live_trading_enabled=false`を固定し、Q01の実注文だけを
停止しています。配置・再起動・bridge attach・実口座照合・注文実行は行っていません。

broker UTCの確定Bid M5だけを使い、4本return variance / 1本return varianceの
48本比率をsignal barから1本遅延して計算します。VR 1.35以上かつ直前12本High上抜けを
LONG、Low下抜けをSHORTとし、研究runnerと同じ110本M5 warm-upとATR20正値条件を
要求します。通常の既存M1利用量420本は変更せず、Q01有効時の共通HIST取得だけを
600本へ拡張します。signal M5確定後の最初のfresh quoteから最大7分、raw spread
0.30以下をentry条件として記録します。将来、別途承認された候補でQ01専用live gateを
有効化した場合だけ0.01 lotを1件開き、broker-confirmed fillから30分でcloseします。

保有中にfresh quote間隔が300秒を超えた場合は到着quoteで即closeします。gap closeと
30分hold closeはwide spreadでも延期せず、market closed 10018ではclose意図を保存して
fresh broker quote基準で再試行します。確認済みcloseから5分をcooldownとします。
再試行stateはsource、raw/effective side、event/release/available/decision/executable、
opportunity ID、group receipt、固定expiryを完全一致で検証し、破損・未来・矛盾stateから
注文を再構成しません。

## NY 05:30 edge-break fade（研究best移植済み・有効候補）

`t0530_edge_break_fade`を既存17レーンと分離したlane 18-21
（magic 230040-230043）へ移植しています。現在のローカル候補
`bot23-integrated-session-vwap-on-t0530-edge-on-q01-v008`では
`t0530_edge_enabled=true`かつ`session_vwap_enabled=true`です。CentOS/MT5への配置、
再起動、live/forward確認はこのローカル候補の範囲外です。

確定M1の直前15本High/Lowを現在Closeが上抜けたときSHORT、下抜けたときLONGとし、
availability（M1開始+1分）が`America/New_York`の05:30以上06:00未満にある
新しいonsetだけを対象にします。各laneは0.01 lot・1 position、合計capacity 4、
実約定時刻から15分の独立deadlineです。releaseから最大5分の一時拒否再試行では、
group共通の永続受領票とlane別retry identityを使い、別laneへの同一signal再送を防ぎます。
破損した時刻・opportunity・受領票は型変換で救済せずfail closedです。

DEV全ティック再集計では研究mid、現行HIST相当Bid、新実装が139 eventすべてで
時刻・方向とも一致しました。この一致はDEV内の実装同一性証拠であり、独立holdoutや
CentOS/MT5実稼働証拠への昇格ではありません。

## NY 05:30-08:30 session-VWAP overlay（採用・ローカル有効）

DEVで固定した`session_vwap_extension_fade`を、既存12レーンとは分離した
lane 13-17（magic 230035-230039）へ実装しています。ユーザーの採用判断により
ローカル設定は`session_vwap_enabled=true`です。配置・再起動・MT5接続は別作業です。

判定はbroker UTCのM1開始時刻に1分を加えた確定・利用可能時刻を
`America/New_York`へ変換し、現地05:30以上08:30未満だけを対象にします。
したがって夏時間では09:29 UTC開始barが09:30 UTC（NY 05:30）に最初の判定対象となり、
12:29 UTC開始barは12:30 UTC（NY 08:30）なので対象外です。Bid HLC3をM1 TickVolumeで累積した日次VWAP、
ATR60（30本からwarm-up）、過去20 calendar daysの絶対Z値Q90を使い、閾値超過の新しいonsetを
逆張りします。各0.01 lot、15分hold、合計capacity 5です。

bridgeの`HISTPAGE`は1回5,000本以下で過去方向へ取得します。初回は20日coverage
まで1ページずつ埋め、その後は直近10本を更新します。取得失敗やMT5側の履歴
ロード待ちは欠損確定にせず、成功済みcacheとcursorを保持して5/15/30/60秒で
自動再試行します。その間はこのoverlayの新規entryだけを未評価として保留し、
既存時間帯のentry/exitと、既に保有するsession-VWAP positionの決済監視は継続します。
未確定M1や古いcacheから新規判定はしません。20日の時刻幅だけではreadyにせず、
最新確定M1、ATR直近60本、当日session開始以降の1分連続性も確認します。欠損時は
cacheを消さずにfull pageの再backfillへ戻り、補完できるまで候補entryだけfail closedです。
`HISTPAGE`の行幅・数値・OHLC整合性が不正なpageも部分採用せず、同じcursorから再試行します。
OPENが`10026/10027`で明確なno-fillになった場合は、その確定M1を消費せず30秒cooldown後に
同じopportunityを再評価します。曖昧なOPEN結果は再送せず、従来どおり所有position照合まで
fail closedです。15分holdのcloseで`10018`を受けた場合もpositionを保持して60秒後に再試行します。
retry対象のsignal bar・side・opportunity・有効期限はlane stateへ保存するため、待機中に次のM1へ
進んだ場合やrunner再起動後も別signalへ置換しません。OPEN確認後・basket保存前に停止した場合は、
pending identityと一致するowned positionが1件だけで、side・lot・commentと保存済み注文開始時刻の範囲まで一致するときだけ
自動復元します。注文開始時刻自体も元signalのrelease以上・固定expiry以下でなければ復元しません。保存retryはsource・signal bar・event/release/available time・side・opportunity ID・固定expiryの
相互整合がすべて一致しない限り再送・復元しません。retry expiryとsignal staleはhost UTCとbroker UTC quoteの
遅い方を使い、どちらか一方で期限切れならfail closedにします。注文開始時刻・cooldown開始・約定照合はbroker UTC quote時刻を
基準にします。全live OPEN直前にはbroker quote時刻とhost UTCの差が`max_signal_delay_minutes`以内であることも再確認し、古いquoteや未来へずれたquoteでは注文を送りません。初回OPEN確認でもposition identifier・side・lot・entry price・注文開始時刻範囲を照合します。
15分close後は、broker確認済みの実約定時刻までに利用可能だった同方向signalを
全5laneで再利用しません（close要求barではなく、close deal時刻を判定基準にします）。
CLOSEDEALはdeal ID・実約定価格・account-currency損益・entry以後のdeal時刻を検証し、不正payloadでは
basketと日次損益を進めません。正常なdealの後着時はclose照合blockだけを解除し、policy/ownership blockは保持します。
bridge v6のCLOSEDEALは同一position identifierの全OUT/OUT_BY dealを集計し、volume加重exit価格、
全slice損益・手数料と総exit volumeを返します。直接ticket不存在と総exit volume=保存lotが揃うまで全決済とみなしません。
複数ticketがUTC日付を跨ぐ場合はbroker deal時刻順に日次損益へ反映し、state配列順で日付を巻き戻しません。
後着した過去日のdealや古いentry判定時刻でも、保存済みの日次損益日付を過去へ戻しません。日次PnL値または
日付が破損している場合は既存basketの決済照合を止めず、新規basketだけを `daily_realized_state_invalid` で遮断します。
保存日付が評価UTC日より未来の場合も、すでにloss limit到達済みならloss blockを維持し、それ以外は
`daily_realized_state_invalid` として新規basketを許可しません。
ZAのpullback待機中も、signal生成時の判定だけには依存しません。実際の約定条件到達時と最終OPEN直前に
blocked UTC hour・日次loss/state gateを現在時刻で再評価し、待機中にgateが有効になった場合は未送信pendingを
解除して新規basketを作りません。これはZA固有の新規basket gateであり、独立session overlayへは流用しません。
再起動後のZA pendingは、正のtarget/ATR、opportunity ID、signal bar=event time、event+1分=release time、
release以後かつentry wait+signal遅延上限以内のexpiryがすべて一致し、現在の評価時刻がrelease以後のときだけ再利用します。
現在時刻がpersist済みreleaseより前ならclock後退を含む矛盾stateとして未送信pendingを解除します。不完全・矛盾・
異常延長stateは未送信pendingだけを解除し、保存値からbasketを作りません。
OPENが正確な`10018`かつ照合後もowned positionが0件ならmarket closedの確定no-fillとして扱い、
reconciliation blockにはせず60秒cooldownへ進みます。session-VWAPでは古いbroker quote時刻ではなく、
その試行のhost/broker admission時刻をcooldown開始に使うため、同じ休場quoteへの即時再送をしません。
dealのposition ID・symbolと、保存済み元position所有権が一致しなければ所有決済として採用しません。決済dealの
magicは決済を実行したorder側の値であり、手動決済等では元EAと異なり得るため所有判定には使いません。active position同期では
不変identifierだけでなく、実際のCLOSE送信先となるhedging ticketもlive recordと一致させます。
永続basketとlive POSITIONSは、集合化・対応付けの前にticket/identifierが正の整数かつ各行で一意であることを検証します。
重複・非数値・欠損identityは二重CLOSEや損益二重計上へ進めず、非recoverable blockとして保存します。
保存済み `open_time_epoch` は正の値を必須とし、live positionのbroker open時刻もfixed-holdに限らず全laneで必須です。
時刻欠損のまま建玉消失やwide CLOSEDEAL履歴照会へ進めません。
CLOSE直前にもticket/identifier/side/lot/symbol/magic/commentを再照合し、外部部分決済等でvolumeが変わったpositionは送信しません。
bridgeのPOSITIONSとORDERSは異なるrecord形式として個別に検証し、非有限quote、不正position type、
ticket/deal/retcode/価格が整合しないOPEN・CLOSE応答は成功として扱いません。
CLOSE送信直前にはticketごとの送信開始時刻を永続化します。確認済み応答を保存する前の停止、timeout、
壊れた成功応答、`POSITION_NOT_FOUND`は結果不明として送信意図とposition stateを保持し、positionが一時的に
見えていても自動再送しません。bridge内account/mode/permission guard、local request不正、明示allowlist内の
no-fill retcodeだけ送信開始markerを解除します。`PLACED`、`DONE_PARTIAL`、timeout、order changed、request locked、
connection、position already closed、close order exists、未知の整数retcodeは未約定と推定せず、結果不明のままです。
`10026/10027`またはbridge内trade-permission guardでCLOSEが確実に未送信となった場合は、broker quote時刻基準の
`trade_permission_retry_seconds` cooldownを全basket/trend決済経路へ適用します。同一laneの連続拒否が
`trade_permission_alert_threshold`へ達したときだけ手動確認通知を出し、単発拒否やcooldown中のpollでは通知・再送しません。
OPEN/追加注文の権限拒否回数・通知済みstateとは分離し、先行するOPEN拒否通知によって「保有positionをCLOSEできない」
通知が抑止されないようにします。CLOSE成功またはbroker確認済みflat化でCLOSE側stateだけを解除します。
その後にdirect ticket不存在とownership・volume・時刻が一致するCLOSEDEALを確認できた場合は結果不明blockを
解除します。複数ticketの一部だけ解決した場合は、その同じsync内でmarker付きticketのdealが確定し、残りの
positionとordersが完全一致するときだけ残存ticketを再armします。reason名やposition表示だけでは再送しません。
全ticketのCLOSEDEAL確定時にORDERS照会だけ失敗した場合は、決済と損益を確定したうえで旧結果不明blockを
recoverableな`orders_unavailable`へ置換し、後続pollの完全なflat position/order照合まで新規entryを止めます。
個別ticket不存在の証拠は現行bridgeの完全一致応答 `ERR|POSITION_NOT_FOUND` だけです。`ERR|10009`、`ERR|0`、
legacy表記は照会異常として扱い、CLOSEDEAL照合へ進みません。
preflightはbridge名・version・command surfaceの完全一致を要求し、bridge versionは `2026-09-04-s23-legacy-query-v32` です。v31はv30の全IPC/TICKS/ownership guard、edge lane allowlist、deadline切り下げ比較を継承し、Q01 lane 22 / magic 230044 / comment `s23_q01_l1`を所有allowlistへ追加します。さらにrequest ID、deadline、全execution数値、履歴・inventory queryの項目数と数値表現を厳密検査し、符号、指数表記、末尾文字、空値、余分なfieldを変換前に拒否します。INFO/HIST/HISTPAGE/TICKSはXAUUSDへ、履歴はM1へ、inventory queryはbot23の所有magicへ固定します。OPEN実行点ではXAUUSD、0.01 lot、SL/TPなし、deviation 50、bot23 magic/comment対応表、期待保有数、同一magicの異物、symbol取引mode、market-order可否、必要証拠金の2倍以上のfree marginを固定検査します。ACCOUNT/INFO/CAPSとposition/orderレコードは固定項目数の完全一致で解析し、区切り文字を含むcommentや拡張frameを所有・口座・quote証拠として採用しません。CLOSEはticketに加えてsymbol・magic・comment・position identifierを同一command内で再照合し、不一致時はbroker呼出し前に拒否します。

全laneのlive OPEN予約は opportunity・side・lot・symbol・magic・comment・fill期限・signal bar・basket ATR をstateへ先に保存します。注文成功後のprocess停止では、このreceiptとbroker positionの完全一致が1件だけ証明できる場合に限り、新規basketまたは既存basketへの1 ticket追加を復元します。欠損・複数候補・期限外・ownership不一致は自動採用しません。
OPEN応答後はpositionとpending orderを再取得し、ticket/position identifierの重複を照合前に拒否します。atomic guard・10018・10026/10027の確定no-fill時に同時出現した同一namespaceの建玉を今回の約定として採用せず、正常OPENでも返却ticket以外のposition/orderが増えていれば、返却ticketだけをstateへ記録したうえで新規entryを非recoverable blockします。
liveの新規OPEN直前には、設定lotがbrokerのvolume min/max/stepに一致し、設定digits/pointが
INFOと一致することも確認します。不一致時は新規entryだけをfail closedとし、既存positionの
同期・決済監視とshadow/DEV判定は止めません。session-VWAPのlive判定ではbroker quote時刻が
欠けたINFOをhost poll時刻で補いません。起動時だけでなく各pollでもINFO全体を取得失敗として扱い、
全自動判定を保留して正しい時刻付きquoteを待ちます。

## Raw tick shadow collector（2026-08-29追加、売買非介入）

`raw_tick_shadow_collector.py`はCycle27のraw tick特徴量を将来再現するための独立collectorです。`live_s23_bot.py`からはimportされず、初期設定も`raw_tick_shadow_collector.enabled=false`です。既存entry/closeには介入しません。

- bridge命令: `TICKS|symbol|from_msc|to_msc|max_rows|skip_at_from_msc`
- 1ページ最大2,000 tick
- CSV: `logs/s23_raw_ticks_append_only.csv`
- state: `state/s23_raw_tick_collector_state.json`
- recipe: `s23_raw_tick_shadow_v1`
- 同一millisecond内のskip cursorと1 tickごとの次cursorをCSVへ保存
- 同一run内のbatch番号はcollector寿命を通じて単調増加し、復帰時はrunの再登場、batch飛び・逆行、同一batch内のingested/cutoff不一致を拒否する。available時刻はbatch境界を含む全履歴で非減少を要求し、collectorも前回保存時刻をfloorとしてhost時計後退を吸収する
- configは重複key・非有限値・型強制・path escapeを拒否し、CSV復帰は末尾だけでなく全履歴をstream検査して、完全列数、recipe/batch、全時刻・quote、broker時刻、sequence、先頭skip=1と同一millisecond内cursorの連続性を要求
- collector生成時もsymbol・recipe・明示run IDの型、前後空白、CSV/command区切り文字を送信・追記前に拒否し、自身で復帰不能な証跡を生成しない
- Lastは0以上、flagsはMQL5 `uint`範囲内だけを許可し、bridge応答時とCSV復帰時の両方で検証
- page metadata、最大件数、要求from/cutoff窓を応答ごとに再検証し、空pageは件数・tail時刻・tail件数がすべて0の完全な空metadataだけを受理
- CSVは追記専用。起動・復帰時に既存全履歴を検証し、その後は保持したfile ID・size・mtimeと直前cursorを追記ごとに照合する。外部改変時は追記せず停止し、履歴増加に対して毎page全走査を繰り返さない
- CSV identityから一意に決まるlockを全実行中保持し、別state名を使った場合も同一CSVへの競合追記をfail closedで拒否
- stateはCSVの代替cursorではなく損失検知用の永続証跡として照合する。stateがあるのにCSVが欠損・空、stateがCSVより先行、またはstateのsequence位置にあるCSV行とcursor・run ID・available時刻が不一致なら復帰を拒否する。CSV追記後・state置換前の停止でstateだけが遅れている場合も、そのstateがCSV途中の完全一致checkpointであることを証明してから全履歴検証済みCSVを正として継続する
- `event_time`、`release_time`、`ingested_time`、`available_time`、`cutoff_time`を独立列で保存
- 時刻はUTCかつ `event/release <= cutoff <= ingested <= available=recorded` を要求し、host時計後退時もcutoff以前のreceipt時刻を生成しない

手動実行は、更新済み`BotBridge_s23.mq5`をcompile/attachした環境で`py raw_tick_shadow_collector.py --force --once`を使用します。本作業ではcompile/attach・実環境取得・deployは行っていません。

OHLCVだけでCycle27構造を再生成した別candidateは、`backtest/output/backtest226/candidates/xau-eu-nypre-ohlcv-rebuild-v001`に分離しています。これは現行bot23の売買signalではありません。

`bot23` is the live/shadow port of the fixed
`bot23_late_short_30m_action_matrix_v001:reverse_d60` candidate for XAUUSD.
It preserves the ZA confirmed-M1 producer and four-lane
`first_consuming_lane_preserve_primary_v1` inventory, then transforms only a
late SHORT opportunity: when the executable Bid is at least 0.60% below the
completed-M1 close exactly 30 minutes earlier, the effective side is LONG.

After any LONG basket is closed at its native target, the runner blocks only
new LONG baskets across all four lanes for eight minutes. The live clock starts
from broker close-deal confirmation; existing baskets, LONG adds, and every
SHORT path remain unchanged. Unsubmitted pending LONG entries are cancelled
when the target close is armed and again when it is confirmed.

The adopted overlay freezes the minimum/maximum entry-price range whenever the
portfolio holds equal non-zero LONG and SHORT position counts and the completed
M1 close is inside that range. After one completed-M1 close breaks the range,
two consecutive completed-M1 closes back inside within 15 minutes create one
new-basket opportunity opposite the breakout. ZA has priority on the same bar;
the synthetic opportunity waits for the next completed M1 without a ZA signal.

An independent morning overlay runs only from 00:00 through 01:59 UTC
(JST 09:00-10:59). It implements the frozen provisional candidate
`stable_001-param-15-55-45`: false-break confirmation with direction control
(15-minute hold), price/effort divergence with direction control (55-minute
hold), and M15 compression/M5 edge release in the primary direction (45-minute
hold). Each signal owns one 0.01-lot lane and at most one position, for a
combined morning maximum of three. Holds start from the actual broker fill
time. This overlay does not use ZA routing, pullback, adds, adaptive exits, or
the LONG-target portfolio-rearm gate.

An independent midday overlay runs only for executable releases from 02:00
through 03:59 UTC (JST 11:00-12:59). Its frozen signal is
`round_s2p5_d0p05_r0p03`: using confirmed M1, the prior close selects the
nearest 2.5-USD grid boundary, a bar must sweep that boundary by at least
0.05 ATR60 and reclaim by at least 0.03 ATR60, and only a new raw-side onset
is admitted. One private 0.01-lot lane holds the confirmed broker fill for 60
minutes and has capacity one. It does not enter ZA routing or use ZA pullback,
adds, adaptive exits, cooldown, or LONG-target rearm.

Operational status as of 2026-09-02: `midday_session_enabled=false`. An
unchanged-candidate extended-forward diagnostic produced 25 Stress trades /
USD -52.561 / PF 0.511, while the live sample was also negative. The master
switch therefore blocks new Midday orders while preserving passive shadow
opportunity/state-tag evidence and management of any already-owned Midday
position. Morning, pre-EU30, and ZA routing are unchanged.

The adopted pre-Europe overlay keeps the original JST 13:00-to-15:30/16:30
market-time meaning, but runtime admission is resolved and compared only in
UTC: 04:00-06:30 during London summer time and 04:00-07:30 during London
standard time. London and New York IANA zones are used only to choose their
DST-specific UTC boundaries. Three
private 0.01-lot lanes run the frozen best candidate: Bollinger-squeeze release
with direction control (45-minute hold), double-sweep resolution in its primary
direction (60-minute hold), and RSI-extreme reversal with direction control
(45-minute hold). The three M5 signals, their directions, and holds are fixed;
they do not use ZA pullback, adds, adaptive exits, or LONG-target rearm.

The independent trend-recovery lane is armed only after broker-confirmed full
closure of a `reverse_d60` LONG basket at its native basket stop. For 30 minutes,
each newly completed bullish M1 may open one 0.01-lot SHORT, up to two entries
for the episode. ATR30 is frozen from the stopped LONG basket. Every ticket has
its own native target and a 0.5x native stop; one ticket reaching its stop closes
the whole recovery basket. Targets and the 70-minute maximum hold close only the
affected ticket. The episode cannot rearm from its own exits.

## Structure

- One inventory-free ZA opportunity is created from each confirmed M1 signal.
- The frozen `reverse_d60` policy is applied once before lane routing. Raw LONG
  and non-qualifying SHORT opportunities are unchanged.
- The opportunity is offered to Lane 1, then Lane 2, Lane 3, and Lane 4.
- Routing stops at the first lane that consumes it through pending arm,
  pending refresh, confirmed entry, or confirmed/attempted add.
- The same opportunity is never submitted to more than one lane.
- A LONG target close is a portfolio event: all lane exits are processed before
  any lane may fill or admit a new entry on the same polling cycle.
- Balanced-book range state is evaluated once per completed M1 before that
  poll's exits, matching the frozen ordered-tick replay. A change to unequal
  LONG/SHORT counts invalidates an unconfirmed break.
- The range-fade opportunity bypasses only the low-volatility ZA extreme and
  pullback requirement. Session, spread/ATR, daily-loss, cooldown, capacity,
  portfolio rearm, ownership, reservation, and final execution guards remain.
- Each lane independently owns its basket, pending state, cooldown, frozen
  ATR30, daily realized PnL, tickets, and close lifecycle.
- Each ZA lane permits at most two positions; ZA portfolio capacity is eight.

| Lane | Magic | Comment prefix |
|---:|---:|---|
| 1 | 230023 | `s23_za_l1` |
| 2 | 230024 | `s23_za_l2` |
| 3 | 230025 | `s23_za_l3` |
| 4 | 230026 | `s23_za_l4` |
| AM 1 | 230027 | `s23_am_l1` |
| AM 2 | 230028 | `s23_am_l2` |
| AM 3 | 230029 | `s23_am_l3` |
| MD 1 | 230030 | `s23_md_l1` |
| PE 1 | 230031 | `s23_pe_l1` |
| PE 2 | 230032 | `s23_pe_l2` |
| PE 3 | 230033 | `s23_pe_l3` |
| TR 1 | 230034 | `s23_tr_l1` |
| SV 1 | 230035 | `s23_sv_l1` |
| SV 2 | 230036 | `s23_sv_l2` |
| SV 3 | 230037 | `s23_sv_l3` |
| SV 4 | 230038 | `s23_sv_l4` |
| SV 5 | 230039 | `s23_sv_l5` |
| ED 1 | 230040 | `s23_ed_l1` |
| ED 2 | 230041 | `s23_ed_l2` |
| ED 3 | 230042 | `s23_ed_l3` |
| ED 4 | 230043 | `s23_ed_l4` |

All ZA lanes use 0.01 lot, session 13:00-18:00 UTC, the 14 UTC new-basket
block, USD -27 confirmed daily realized loss limit per lane, 0.65 ATR add,
30% add-profit guard, 10-minute failure-to-progress, 70-minute maximum hold,
and eight-minute cooldown. The ATR30 `<2.0` ZA pullback and adaptive exits are
unchanged.

Morning signals are evaluated only from completed bars. M15/M5 values become
available after their source bar completes; the final M5 edge is released to
M1 at M5 completion. Live tick `Volume` is the activity proxy used by the
price/effort signal. Current executable spread and stale-signal checks remain
live safety gates, so exact trade counts can differ from the research replay.

The midday lane is independent of the three morning lanes. Because a morning
position can remain open after 02:00 UTC, the transition can temporarily hold
up to four overlay positions; together with ZA's eight-position limit, the
configured portfolio ceiling is twelve. The midday close clock starts at the
confirmed broker fill, not at the signal-bar timestamp.

## DST-aware entry-admission clock

Post-13:00 JST overlays use independent `Europe/London` and
`America/New_York` new-entry admission clocks instead of hard-coded month
ranges. On dates when both markets are in the same DST regime, the schedule is:

- JST 13:00-15:30 (16:30)
- JST 15:30 (16:30)-20:30 (21:30)
- JST 20:30 (21:30)-05:30 (06:30)

Boundaries are half-open, so an instant belongs to at most one block. The clock
is configured with `routing_enabled: true` for the adopted pre-EU30 overlay.
The later two blocks remain without an adopted signal and do not alter the
existing JST09-13 or ZA routes.

Each configured boundary names its own reference clock. The European start is
governed by `Europe/London`; the US start and overnight end are governed by
`America/New_York`. This remains correct during the March and autumn weeks in
which the two markets change DST on different dates.

London governs the 15:30 (16:30) European boundary. New York independently
governs the 20:30 (21:30) and 05:30 (06:30) US boundaries. During the March and
October/November weeks when their DST regimes differ, the clock therefore does
not incorrectly shift all three boundaries from one market's calendar.

This calendar is entry-only. Once a broker fill is confirmed, the owning lane
calculates its close deadline as confirmed fill UTC plus elapsed hold minutes.
Position synchronization, target/stop, and scheduled-close monitoring continue
across admission-block boundaries and DST changes without reclassification.
For fixed-hold lanes, a missing broker `open_time` is never replaced by the
poll time. The owned position is retained in state with entry blocked, and a
later exact owned-position sync restores the broker time automatically when it
becomes available.

## Safety and recovery

- Broker inventory is accepted only after ticket/identifier, side, volume,
  symbol, magic, and comment match persisted lane state.
- A durable opportunity reservation is saved before routing and an OPEN
  reservation is saved before broker submission. A crash may discard one
  opportunity, but must not duplicate it across lanes.
- Transient symbol/position/order failures block new entry. Complete owned
  synchronization clears a recoverable block and resumes basket monitoring.
- If pending-order visibility alone is unavailable, new entries remain blocked,
  while fully reconciled owned market positions continue target/stop/time
  monitoring and may still be closed.
- A symbol-info outage while inventory is open emits a manual-action alert;
  broker-side SL/TP remains unset, so no automated basket exit can run until an
  executable Bid/Ask quote and the bridge recover.
- Non-recoverable ownership or reconciliation blocks require manual action and
  cannot be overwritten by a later transient failure.
- Broker fill confirmation is required before inventory is added to state;
  close-deal confirmation is required before realized PnL or flat state is
  recorded.
- A durable close intent saved before the broker call is distinguished from a
  confirmed submitted close. After a restart or malformed/failed response, only
  an exact owned-position plus empty-order synchronization can re-arm the
  unsent/failed remainder. A position already marked `close_requested` remains
  pending until position disappearance and `CLOSEDEAL`; it is never resent merely
  because position visibility lags. Unrelated ownership/policy blocks remain set.
- A mixed multi-position result retains `close_requested` on every successfully
  submitted ticket and retries only the failed or unsent remainder. CLOSEDEAL
  ownership uses lifecycle position identifier, symbol, and persisted opening
  ownership; exit-deal magic identifies the closing actor and may legitimately
  differ after a manual or external close.
- Exact market-closed retcode 10018 is a definitive no-fill for every bot-managed
  close path, including ZA and trend target/stop/max-hold. It creates no permanent
  reconciliation block and waits for a broker quote at least 60 seconds newer
  before retry. ZA FTP/max-hold and trend max-hold elapsed time also use the
  broker quote clock, never a host clock that has run ahead.
- Exact owned sync restores broker `open_time` for ZA positions as well as
  fixed-hold overlays before any FTP/max-hold elapsed-time decision.
- Persisted entry retry and post-close cooldown timestamps are fail-closed when
  malformed; an unreadable durable clock must never be interpreted as an
  expired guard. A missing or non-finite persisted basket PnL peak is reset to
  current executable PnL so failure-to-progress monitoring remains active.
- Startup shape validation covers routing plus every ZA/overlay lane, while
  absent overlay lanes remain eligible for the intentional first-start
  migration. Every live OPEN also requires a nonnegative integer basket
  sequence. Exact owned sync restores broker entry price as well as open time;
  malformed frozen ATR falls back to fixed exit thresholds. Fixed-hold spread
  defer state is normalized before a due exit, malformed trend virtual state is
  invalidated without opening, and malformed session close-ledger identity
  blocks same-direction reuse.
- Fixed-hold exits use the broker quote timestamp returned by the bridge. A
  quote from before the deadline, a duplicate quote, or a missing timestamp
  never advances the close state. A normal spread closes immediately; after a
  wide spread, three fresh narrow quotes are required, with a 30-minute force
  limit. Exact MT5 retcode 10018 waits 60 seconds and retries without creating
  a permanent reconciliation block.
- While a LONG target close is awaiting confirmation, new LONG baskets fail
  closed. The fixed eight-minute rearm then starts from the latest confirmed
  close-deal timestamp, survives restart in routing state, and blocks no SHORT.
- Range, breakout, confirmation, pending synthetic side, and per-M1 dedup state
  are persisted under `routing`. First-start migration initializes only this
  overlay as inactive and preserves existing baskets and ZA pending entries.
- Bridge ACCOUNT metadata must match the configured MT5 login and server. An
  older compiled bridge without account identity, or a terminal logged into a
  different account/server, is rejected before live operation.

## Logging policy

- `s23_trades.csv` preserves economic actions, ownership/reconciliation state
  transitions, causal timestamps, tickets, position identifiers, deal IDs,
  entry/exit prices, lane/basket identity, and ticket-level confirmed net PnL.
- `s23_signal_evaluation.csv` is a separate passive ledger with
  `strategy_group`, lane, `spec_id`, configured `signal_id`, and a normalized
  `signal_variant_id`. ZA is split into `za_horizontal_primary`,
  `za_late_short_reverse_long`, and
  `za_inventory_range_false_break_fade`; raw/effective side and transform are
  retained. The shared opportunity/ticket/deal identities join this ledger to
  `s23_trades.csv`, including ticket-level confirmed net PnL.
- Shadow/DEV basket closes are emitted to the evaluation ledger as one
  `position_close_attributed` row per position. This prevents a basket that
  contains multiple ZA variants from collapsing to `mixed`, and the split PnL
  sums to the unchanged aggregate `basket_close` PnL in `s23_trades.csv`.
  Migrated/legacy ZA inventory without an `opportunity_id` is isolated as
  `za_unattributed_legacy` and is never credited to the primary ZA signal.
- Outcome aggregation must use `position_close_confirmed` for live fills and
  `position_close_attributed` for shadow/DEV closes. Requested/deferred rows
  are lifecycle evidence, not additional realized PnL.
- Evaluation-ledger construction/I/O failures are logged once and disable that
  passive sink for the process; they never interrupt an owned position close.
  Its header is still checked at preflight so a stale schema is visible before
  new operation begins. The operational `s23_trades.csv` remains a hard
  preflight gate.
- Broker-confirmed close rows are idempotent by immutable deal ID, lane, and
  position identifier. A retry after state persistence failure does not append
  duplicate operational or passive close rows. Basket state and daily realized
  PnL advance only after every confirmed deal has a flushed and filesystem-
  synchronized operational audit row. Duplicate/conflicting confirmed deal
  identities are rejected during startup validation. The subsequent realized-
  PnL, portfolio-rearm/recovery, basket-clear, sync-block, and state-save steps
  are rollback-guarded as one retryable state transition; a failure restores the
  same runner's pre-consumption state before poll containment. Helper-level state
  saves are deferred and the completed transition is durably committed once, so
  process termination cannot preserve a half-applied close transition. Derived operational
  and passive rows carry the basket's deterministic broker-close deal identity and
  deduplicate independently, so retry neither repeats durable rearm/recovery evidence
  nor prevents repair of a missing passive counterpart.
- Repeated operational `entry_skip` diagnostics write once on transition and
  then one `diagnostic_repeat_summary` every five minutes with
  `repeat_count` and `repeat_window_seconds`. A reason change flushes the prior
  summary immediately.
- `s23_bot.log` writes status every five minutes and rotates at 10 MiB, keeping
  five backups.
- The runner refuses an old or incompatible trades-CSV header. Archive/reset
  the legacy CSV before the version-3 first start; it never silently appends a
  new row shape under an old header. Header identity is rechecked on every
  append so same-path replacement cannot bypass the gate. Startup also rejects
  malformed row widths and an unterminated tail left by a partial write.

### Passive forward opportunity observer

- `shadow_opportunity_observer.py` has no bridge, executor, or order dependency.
  Its return values are never used by entry, add, close, or routing decisions.
- Every confirmed ZA opportunity is registered before policy/stale rejection or
  lane routing. The observer records raw/effective side, reverse_d60 disposition,
  executable spread, ATR30, ret10, volume ratio, lane occupancy/pending state,
  readiness, route result, and consumed lane.
- `logs/s23_shadow_markouts.csv` records executable-side PnL and MFE/MAE after
  1, 5, 15, 30, and 60 minutes. LONG uses registration Ask to later Bid; SHORT
  uses registration Bid to later Ask. A policy-blocked signal is labeled using
  its raw side with `raw_fallback_policy_blocked` rather than being presented as
  an executed strategy decision.
- `logs/s23_shadow_opportunities.csv` and
  `state/s23_shadow_observer_state.json` are separate from `s23_trades.csv` and
  `s23_bot_state.json`. Pending horizons survive restart and CSV identities are
  reconciled to suppress duplicate registrations, route rows, and markouts.
- Observer initialization/write failures are logged and contained; they do not
  block or alter live trading. The evidence is diagnostic only and is not a
  live gate or automatic parameter-selection input.
- MFE/MAE resolution is the live poll cadence (currently five seconds), not
  ordered every-tick resolution. Quote gaps and process downtime remain visible
  through `observation_delay_seconds` and `quote_samples`.
- The JST11-13 source has the same passive evidence in separate files:
  `s23_midday_shadow_opportunities.csv`, `s23_midday_shadow_markouts.csv`,
  `s23_midday_shadow_state_tags.csv`, and
  `s23_midday_shadow_observer_state.json`. Capacity, spread, stale, and sync
  rejections remain observable even when no order is sent.
- The pre-EU30 source uses `s23_pre_eu30_shadow_opportunities.csv`,
  `s23_pre_eu30_shadow_markouts.csv`, `s23_pre_eu30_shadow_state_tags.csv`, and
  `s23_pre_eu30_shadow_observer_state.json` with the same failure isolation.
- The local `exness-bot-23` Compose service mounts the observer module read-only.
  This definition change alone does not alter the running container; collection
  begins only after the updated files are deployed and that service is recreated.

### Passive forward state tags

- `shadow_state_tagger.py` adds causal market/inventory descriptors to each raw
  ZA opportunity and writes `logs/s23_shadow_state_tags.csv`.
- The file joins to `s23_shadow_opportunities.csv` and
  `s23_shadow_markouts.csv` by `opportunity_id`; future markouts are deliberately
  not written into the tag row.
- Tags use the completed signal M1 and bars available before it: prior-20 range
  position, high/low sweep and rejection, candle body/wicks, ATR-normalized
  returns and range, activity, path efficiency, and current inventory balance.
- The activity percentile is computed from earlier completed bars only. The
  current bar is never included in its own reference distribution.
- The tagger has no bridge, executor, or order dependency. Its return value is
  ignored, failures are contained, and no tag is read by entry, routing, add, or
  close logic. It therefore observes the existing forward strategy without
  changing orders.
- `opportunity_id` is loaded from the CSV at startup, preventing duplicate tag
  rows after a restart. A mismatched existing header disables tagging through
  the contained error path instead of altering trading.

## Canonical evidence

- Knowledge root:
  `C:/botter/backtest/検討中/chatgpt案/多重ポジ/利益確保案/bot23_多重ポジ化`
- Dev run:
  `C:/botter/backtest/output/backtest153/candidates/bot23-za-horizontal-inventory-v001/runs/20260825_tradeops_full_dev_v002`
- Observed leakcheck run:
  `C:/botter/backtest/output/backtest153/candidates/bot23-za-horizontal-inventory-v001/runs/20260825_observed_leakcheck_tick_v001`
- reverse_d60 dev run:
  `C:/botter/backtest/output/backtest157/candidates/bot23-late-short-30m-action-matrix-v001/runs/20260826_0230_reverse_refinement_v001`
- reverse_d60 observed leakcheck run:
  `C:/botter/backtest/output/backtest157/candidates/bot23-late-short-30m-action-matrix-v001/runs/20260826_0226_observed_leakcheck_replay_v001`
- LONG target portfolio-rearm candidate:
  `C:/botter/backtest/output/backtest208/candidates/bot23-long-target-portfolio-rearm-v001`
- Inventory range false-break fade candidate:
  `C:/botter/backtest/output/backtest213/candidates/bot23-x-archive-inventory-range-fade-opt-v001`
- JST09-11 stable_001 research:
  provisional fixed implementation `stable_001-param-15-55-45`; Dev Stress
  146 trades / USD 345.0425 / PF 1.78453 / MTM DD USD 75.623 / 11 of 11
  positive weeks. The original stable_001 leakcheck was USD +79.333. The
  parameterized forward check contains only one complete day (4 trades,
  USD +28.446 Stress), so it is not independent proof and must not be retuned
  from live outcomes.
- JST11-13 round-level sweep research:
  fixed `round_s2p5_d0p05_r0p03`, 60-minute hold, capacity one. Dev Stress
  produced 102 trades / USD +199.7715 / PF 1.676534 / MTM DD USD 88.591;
  observed leakcheck Stress produced 40 / USD +156.4325 / PF 2.892275 / DD
  USD 27.583. The already-consumed forward file produced only 4 trades / USD
  +17.583 / PF 3.110804 / DD USD 15.756 and is not independent proof.
- JST09-13 combined fixed-risk audit:
  `evidence/jst0913_combined_v004`. On the exact fixed overlays, Dev Stress was
  248 trades / USD +544.814 / PF 1.74115 / every-tick MTM DD USD 80.473 /
  maximum four positions. The observed leakcheck diagnostic was 95 trades /
  USD +258.534 / PF 2.09609 / MTM DD USD 44.363 / maximum three positions.
  Morning/midday inventory overlapped for 26 DEV episodes (12.568 hours) and
  11 observed-leakcheck episodes (5.117 hours); the result is a portfolio-risk
  audit only and was not used to retune either block.

reverse_d60 Dev Base produced 1,529 trades, USD 908.314, PF 1.188445,
and every-tick MTM MDD USD 228.926. Its observed leakcheck Base produced 391
trades, USD 218.173, PF 1.190972, and MDD USD 153.836. Stress produced 378
trades, USD 261.099, PF 1.243088, and MDD USD 193.440. This remains a
forward-only candidate: the leakcheck period is already observed, and live
profit and five-second-poll equivalence are not proven.

The fixed eight-minute portfolio rearm improved reverse_d60 Dev Base from USD
908.314 / PF 1.1884 / MDD 228.926 / 1,529 trades to USD 986.008 / PF 1.2118 /
MDD 199.475 / 1,484 trades. Dev Stress improved from USD 862.938 / PF 1.1808 /
MDD 246.086 / 1,520 trades to USD 993.680 / PF 1.2200 / MDD 246.086 / 1,470
trades. The two already-observed forward days also improved, so this remains a
forward-only adoption rather than independent forward proof.

The fixed range-fade overlay improved that parent on Dev Base from 1,484 trades
/ USD 986.008 / PF 1.2118 / MDD 199.475 to 1,490 trades / USD 1,079.596 / PF
1.2343 / MDD 199.475. Dev Stress improved from 1,470 / USD 993.680 / PF 1.2200
/ MDD 246.086 to 1,473 / USD 1,066.683 / PF 1.2379 / MDD 246.086. Observed
leakcheck Base added one trade and USD 10.076; Stress was unchanged. Because
leakcheck was already observed and the event count is sparse, this remains
`forward_only` and must not be retuned from future live outcomes.

## Start prerequisites

State schema remains version 3 and the four ZA ownership namespaces are unchanged.
The first start adds empty morning lane states and policy identity while
preserving every existing ZA basket and pending entry. Morning ownership uses
new magics 230027-230029 and does not adopt positions from any other magic.
The first start also adds the empty midday lane and its frozen policy identity
without changing ZA or morning inventory. Midday ownership uses magic 230030
and comment `s23_md_l1`; an incompatible non-empty or foreign identity blocks
that lane rather than being adopted.
The first start adds three empty pre-EU30 lanes and their frozen policy
identity, preserving every ZA, morning, and midday basket. Their private magics
are 230031-230033 and their close deadlines remain confirmed fill UTC plus
45/60/45 elapsed minutes even after the admission block ends.
The first start also adds an empty trend-recovery lane and an inactive episode
record. It does not infer a historical reverse-LONG stop or modify any existing
basket. Ownership uses magic 230034 and comment `s23_tr_l1`.
The first start adds the portfolio-rearm and range-fade fields under `routing`
while preserving all existing baskets, unresolved OPEN evidence, and pending
ZA entries; it invents neither a historical rearm interval nor a historical
range/breakout.
The first start also adds one empty Q01 lane and its frozen policy identity
while preserving all 21 pre-existing lane states. Q01 owns magic 230044 and
comment `s23_q01_l1`; a current-generation Q01 state with a missing required
receipt or quote-clock field fails closed instead of being silently repaired.
On the first start with `reverse_d60`, the runner preserves existing baskets and
unresolved OPEN reconciliation state but clears unsubmitted local pending
entries created under the previous entry policy.

An unresolved market `OPEN` remains a hard block on every new entry and add.
It does not suppress target, stop, failure-to-progress, or max-hold exits for an
older basket when every live position exactly matches the persisted ticket,
position identifier, symbol, magic, comment, side, and lot. Any unexpected live
ticket, ownership mismatch, or failed position query still blocks both
admission and automated close. After the lane is broker-flat, an unresolved
OPEN reservation is cleared only after three consecutive clean bot-scoped flat
position/order confirmations.

Before restart, MT5 must be reconciled for the retired bot23 magic 200023 and
active magics 230023-230044, with no unexplained pending orders. The runner independently
checks the retired namespace and refuses cutover if it is non-flat or cannot be
queried. Preserve a compatible `state/s23_bot_state.json`; never reset state
while any lane position or order exists. Because the close audit columns changed,
archive the existing `logs/s23_trades.csv` under an `old` folder before restart
so the new header is created. Startup now checks this before connecting.

`live_trading_enabled=true` and `shadow_forward_enabled=false` remain unchanged.
File replacement does not restart or deploy the running process.

Research/live equivalence still has two limits. Research built M1 from ordered
Bid ticks, while live uses broker HIST M1 OHLC and tick volume and submits on a
later five-second polling quote. The updated bridge now exposes broker quote
time and the fixed-hold path enforces the fresh-quote/spread/reopen contract,
but live exits can still occur several seconds after the first eligible tick.
This remains a forward-audit limitation rather than exact tick-for-tick parity.

## Checks

```powershell
py -m py_compile live_s23_bot.py test_s23_regressions.py
py live_s23_bot.py --self-test
py test_s23_regressions.py
py test_shadow_opportunity_observer.py
py test_shadow_state_tagger.py
py test_eu_session_clock.py
```

`test_ny0530_mtm_mdd_applies_explicit_lot_contract_multiplier` additionally
checks the external backtest227 research source when it is available. In a
standalone GitHub clone, set `BOTTER_RESEARCH_ROOT` to the separate research
checkout root; otherwise that one external-artifact check is reported as
skipped while the self-contained bot23 suite continues.

Local release-candidate runner SHA-256:
`c79284157a27a13bc16ea302278d4482395d53b6561d21487897a383bdea0911`.

Local release-candidate params SHA-256:
`67faa225d255d4b4bc88b7f3c11bcd483a3510c9c1f65e3337a70bb21b722af5`.
Local release-candidate regression SHA-256:
`21472106c24cf5ad38bc678ab9a6b2154d3c5fe6b11b7a87be7957324db4cbfe`.
Entry-admission clock SHA-256:
`fbdecc7be7d64e457a151c05cbb8f0496986c7d6de18dcd37b15104f6b28318b`.
Position-lifecycle clock SHA-256:
`91e2feea92d986154dfce88171dd154daf6b80f842b42206a1a6d9c79631ee57`.
Clock regression SHA-256:
`8b6542452a6e3d0e9d0762997033654aff0df01597a995fa165a22d31ffbf8cb`.
Installed shadow-observer SHA-256:
`f25a003df49e801dce8f9e3a73fa1f5722f001c75a68d714295510628b49eeaa`.
Installed shadow-observer regression SHA-256:
`5dd72132c92de2aea1f43fe992cf738354529238d2cf503b4605c0c8a4c76f4d`.
Installed executor SHA-256:
`ea0c5f6d48f6fa36bccfd602e2ac4aaf4ea0f2245920af6e137a21c38cc41489`.
Installed bridge-source SHA-256:
`d40491d6b25eafd402ea3de7b94161da2bffa26347381dbc25387a45d6a165f5`.
Installed bridge-binary SHA-256:
`e9135ec12d9c8f2cc84591f3b1842942644b5db060c62cd82bf75408fb03234d`.
All hashes above identify the local source candidate. Runtime deployment and
process identity must be verified separately; these hashes do not replace the
canonical research identities in `SOURCE_BACKTEST.md`.

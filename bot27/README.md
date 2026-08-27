# bot27 / S27

取引対象USTECの`PV2C859_L3_ACTIVITY_VSA_FORWARD_R1`を稼働するlive forward bot。XAUUSDは取引しない。

- strategy: 既存Longを3lane化し、独立Shortの`Activity`と`VSA`を追加
- Long signal: `ret25 > 0`かつ`absret_std_ratio30_120 <= 0.6693523825105777`
- Activity Short: 直前M1のtick activityが60本medianの1.3倍以上、直前`abs(ret1)<=0.5bps`、現在M1が陰線かつ直前安値を下抜け
- VSA Short: 現在M1のtick activityが1.2倍以上、上ヒゲ比率0.35以上、陰線、CloseがEMA20より上
- feature source: Longは完了M1のBid close。ShortのCloseはbridge `MidClose`、Open/High/LowはMT5 Bid bar、activityはTickVolume
- execution: signal bar終了後30秒以内の最初の5秒pollでmarket LONG
- inventory: 最大5つ。Long lane 1-3はfirst-free、lane 3のみentry spread `<=0.595764962212364bps`。Short lane 4/5はActivity/VSAを独立保有し、同一M1で両方成立した場合は両laneへ入れる
- lot: Long各`0.20`、Short各`0.15`。バックテストのShort 0.75 exposureをUSTEC最小lot 0.05で正確に再現するため、全体を4倍scaleして比率を維持
- exit: Long lane 1/3は+4bps連続20分、lane 2は+2bps連続20分、hard 90分。Short lane 4/5はAsk基準+4bps連続10分、hard 60分。全lane actual entry時刻基準。close時のfresh quote/spread guardは従来どおり
- session: 固定UTC時刻やDST表は使わず、fresh broker quoteとmarket-closed retcodeで平日・週末・休日の再開を共通判定
- symbol / broker minimum lot / configured lots: `USTEC` / `0.05` / Long `0.20`, Short `0.15`
- magic / comment: `200027` / `s27_pv2c859_l1`～`s27_pv2c859_l5`。既存の共通prefix positionもownership対象として維持する
- bridge / state / logs: `BotBridge_s27` / `state/s27_bot_state.json` / `logs/s27_bot.log`, `logs/s27_trades.csv`
- passive evidence: raw Long/Activity/VSA signalを`logs/s27_shadow_opportunities.csv`へ保存し、1/5/15/30/60分の実行可能Bid/Ask markout・MFE・MAEを`logs/s27_shadow_markouts.csv`へ保存する。髭、sweep、activity、return、path efficiency、lane inventoryは`logs/s27_shadow_state_tags.csv`へ因果的に保存する。これらの失敗は売買経路へ伝播させない
- execution safety: live OPEN前に`pending_open_action`をstateへ原子的に予約し、再起動時に未解決なら`unresolved_open_action`で新規注文を停止する
- operations: `s27_bot.log`は10MiB×5世代、手動照合が必要なhard blockは設定済みDiscord webhookへrate-limit付き通知する
- current mode: `live_trading_enabled=true`、`shadow_forward_enabled=false`

Forward tickの固定比較では、旧best `Activity 0.75 + VSA 0.75` が新path案よりPnL、MTM DD、日次標準偏差で優位だったため採用した。既存Longポジションはstrategy ID migrationでstateを保持し、保存済みexit policyを継続する。

```powershell
Get-ChildItem bot27\*.py | ForEach-Object { py -m py_compile $_.FullName }
py bot27\live_s27_bot.py --self-test
py bot27\test_shadow_opportunity_observer.py
py bot27\test_shadow_state_tagger.py
py bot27\porting_parity_test.py
```

`s27_trades.csv`はlane、signal type、opportunity ID、position/deal ID、entry/exit時刻、USD損益を持つ監査schemaへ更新した。旧schemaのCSVが存在する環境では、service更新前に既存CSVを日時付きでarchiveし、新headerで開始する。header不一致のままではpreflightが意図的に起動を拒否する。

`c4566_exit_policy.py`とC4566証跡は非稼働の履歴として保存する。`live_config.py`と`startup.ini`はローカル/CentOS個別管理でGit対象外。コード更新だけでは稼働中serviceへ反映されない。

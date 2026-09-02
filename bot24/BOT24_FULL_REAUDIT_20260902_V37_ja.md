# bot24 最終収束再監査 V37（2026-09-02）

## 結論

V36候補を基準に、注文・決済・所有権・状態・起動・Bridge・Compose・受動証跡を再監査した。到達可能な新規欠陥は受動shadow証跡境界に限定して修正した。修正後は異なる観点の全検査を2周し、追加修正0件で収束した。

- ローカル候補: PASS / converged
- core/v206 signal、routing、lot、add rule、TP/SL、hold、cooldown: 変更なし
- `s24_params.json`、実state、execution CSV: 変更なし
- deploy / restart / EA attach / state repair / live switch / order / Git: 未実施
- 実CentOS/MT5 runtime: 未検証のため operation release は NO-GO

## 今回見つけて修正した項目

1. Observer stateの完全性
   - root identityだけでなくpending/completedの全shape、UTC timestamp、side、ownership、価格、sample、horizon、route、JSON metadataを保存前・CSV照合前に検証するようにした。
   - booleanを金融数値として受け入れず、非有限JSONも保存しない。
   - 破損stateからCSVを先に復元してからstate保存に失敗し、証跡だけが部分的に進む経路を閉じた。
2. Passive lotのboolean coercion
   - wrapperが`true`を`1.0 lot`へ変換してobserverを有効化できた。
   - raw値をstrict validatorへ渡し、異常時は該当observerだけを無効化する。fallbackは固定の非実行inert値で構築する。
3. Passive CSVの既存行検証
   - opportunity、markout、state-tag CSVで余剰列・欠損列・異なるidentity/version/symbol等を拒否する。
   - 起動後にファイルが破損した場合も、次回append直前に全行幅を再検査し、壊れた履歴へ追記しない。
4. Canonical mappingのBridge identity
   - 中央mappingに残っていたv8表記を、現行sourceと一致する`2026-09-02-s24-core-atomic-v13`へ更新した。

すべてfailure-first testで修正前の失敗を再現してから修正した。変更対象はno-order証跡と文書identityだけで、core/v206の注文・決済判断は変更していない。

## 独立監査結果

- core/v206 state mutation監査: validator例外0、active lifecycle fail-closedを維持
- MQL5 command監査: execution/query/zero-argument commandのexact arityとstrict numeric guardを確認
- 異常response parser fuzz: 100,000 cases、未処理例外0
- singleton: live runner namespaceを構築・接続前に取得、self-testは実namespace非接触
- Compose: config PASS、`exness-bot-24` serviceとruntime import mountを確認
- sensitive host config: 内容を表示せずregular-file境界だけ確認
- semantic deletion: permissiveな受動破損受付とboolean coercion以外の削除なし

## 修正後 clean cycle 1

- unittest discovery: 168/168 PASS
- Python compileall: PASS
- self-test: PASS（temporary stateのみ）
- Compose config/service: PASS
- MQL5 compile: 0 errors / 0 warnings
- state hash / execution CSV hash: V36から不変
- 新規欠陥: 0件

## 修正後 clean cycle 2

- unittest discovery: 168/168 PASS
- JSON artifacts: 28/28 parse PASS
- merge conflict marker: 0件
- bot24 Python process: 0
- MetaEditor process: 0
- state hash / execution CSV hash: 不変
- 新規欠陥: 0件

テスト中のERROR/CRITICALログは、破損state、所有権不一致、atomic guard失敗、passive write失敗等を意図的に注入したfail-closed確認であり、テスト失敗ではない。

## Candidate hashes

- `live_s24_bot.py`: `2823a1afd4ea8bfea94eacd844689016b6e40e12553bc6324282db101c0d055e`
- `ea_bridge.py`: `5df56cbacd18b23ae138a9af2959f061be8b4b905e3d65f870b93c0959e36bdb`
- `test_s24_bridge_contract.py`: `f6c40ada9b47744b5c7610b57205688bd8883b0d94191fd1d8e95192f77af5df`
- `test_s24_safety_regressions.py`: `eaa01feb8fc2393603a7f916fa9dcd14efffb3054b60637f4e4a54f7f4226b7a`
- `test_shadow_opportunity_observer.py`: `378ea5a3b35c3c5a5c6a9edb2be7eaf0a88bbe25e4458b6d7bc59bee3ea09687`
- `test_shadow_state_tagger.py`: `d5bfd8a02c17356cce9bc930ce5f84ecdfc593db93509e4f3e57dedb4cc2c3d5`
- `shadow_opportunity_observer.py`: `7f60cd11bff0b0c207d22d63fd69a7ac9843fdb369b6f23d14c19d7a61899974`
- `shadow_state_tagger.py`: `85bbc06b56126cd86b23c106807a6eb52c2295b78d4c594a905c2e2efb80da32`
- `README.md`: `b80820e62789eb22b94fab7836767f9e0840b9f1e4d76703ab6d1c81dc6105bb`
- `SOURCE_BACKTEST.md`: `260767d51471e516c51b689e286dfcfba6af1db3a5fae736b2dc618735128ba0`
- `s24_params.json`: `54f0e3c10e87d35f4e68edce54cbaaa8368ae9419a0a6246ab0f31c1ede7e5ff`
- `BotBridge_s24.mq5`: `989fbb3c40ac62d67be8d577049751d341cff0f9dbe4c1926c55493d6ef1249c`
- locally recompiled `BotBridge_s24.ex5`: `4f613978adec864bbe6d3742bcc88badb2483ab9b6ec999d66ceea98f6e8e60a`
- `BOT_BACKTEST_MAP_ja.md`: `c8091a7b6cff4c28fa1f60478944a5cfd4f471d07243f5a2ec7aa7a437cb462d`

## Preservation evidence

- `state/s24_bot_state.json`: `ddc20e94096e8ef7bc17dcf8fd91cf71306a480c75da9f8ea26f6c22f452ef84`
- `logs/s24_trades.csv`: `1b93bcd10dde4a42610d727fe4de3cba7dd940b55c9a770b9722866976bbf850`

V37はローカル候補である。既存のV33 evidence manifestはV33 candidate専用であり、V37 release証跡には再利用しない。実runtimeへ進む場合は、別途承認されたhost上でsource/binary/account/state/inventoryを読み取り照合してから判断する。

# bot18 / S18 basket replacement

`bot18` は、従来の単一GBPUSD bot18を今回のS18 basket実装へ置き換えた運用フォルダです。
サービス名・コンテナ名は既存と同じ `exness-bot-18` のまま使います。

## 現在の稼働設定

- service/container: `exness-bot-18`
- folder: `bot18`
- runner: `live_s18_bot.py`
- params: `s18_params.json`
- live trading: `true`
- shadow forward: `false`
- artifacts: `artifacts/`

## 対象銘柄

- GBPUSD: CatBoost / allow_rate=0.50 / spread_add_points=2.0 / lot=0.05
- EURUSD: LightGBM / allow_rate=0.50 / spread_add_points=2.0 / lot=0.04
- AUDUSD: CatBoost / allow_rate=0.50 / spread_add_points=2.0 / lot=0.04

USDJPY、USDCHF、NZDUSD、CHFJPY、USDCAD は今回の固定basketには入れていません。

## lot配分

entry threshold / allow_rate は変更せず、固定3戦略のportfolio診断に基づいてlotだけを銘柄別に変えています。

- GBPUSD: `0.05`
- EURUSD: `0.04`
- AUDUSD: `0.04`

top-level `lot` はfallback用の `0.01` として残し、実運用では各profileの `lot` が優先されます。

## 出力

- bot log: `logs/s18_bot.log`
- trades: `logs/s18_trades.csv`
- policy decisions: `logs/s18_policy_decisions.csv`
- state: `state/s18_<symbol>_bot_state.json`

## Bridge

- Bridge source: `BotBridge_s18.mq5`
- MT5 Expert name: `BotBridge_s18`
- Python command file: `cmd_s18.txt`
- Python response file: `res_s18.txt`
- Python heartbeat file: `heartbeat_s18.txt`
- Python lock file: `ea_bridge_s18.lock`
- MT5 EA input `InpCommandFile`: `cmd_s18.txt`
- MT5 EA input `InpResponseFile`: `res_s18.txt`

`live_s18_bot.py` は起動時に `CAPS` を確認し、attached bridge が `BotBridge_s18` でない場合は起動を止めます。

## 起動

Git pull後の反映は既存bot18と同じサービス名で行います。

```bash
sudo docker compose up -d --no-deps --force-recreate exness-bot-18
```

## bridge timeout対策

S18のentry threshold / allow_rate / lot配分は変えず、MT5の一時的な応答遅延だけ運用側で吸収します。
`INFO` / `HIST` / `POSITIONS` などの読み取りコマンドは1回だけ再試行し、`OPEN` / `MODIFY` / `CLOSE` は重複発注を避けるため再試行しません。
flat状態ではポジション同期を5秒間隔に抑えますが、cycle start直前は必ず強制同期します。

## 確認コマンド

```powershell
python .\live_s18_bot.py --self-test
python .\live_s18_bot.py --self-test --policy-self-test
```
## 旧stateについて

旧単一GBPUSD版の `state/s18_bot_state.json` は、このbasket実装では参照しません。
新しいstateは `state/s18_<symbol>_bot_state.json` として銘柄別に作成されます。
旧bot18の未決済ポジションがMT5上に残っている場合、新実装はmagicが異なるため管理しません。

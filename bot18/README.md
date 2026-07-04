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

- GBPUSD: CatBoost / allow_rate=0.50 / spread_add_points=2.0
- EURUSD: LightGBM / allow_rate=0.50 / spread_add_points=2.0
- AUDUSD: CatBoost / allow_rate=0.50 / spread_add_points=2.0

USDJPY、USDCHF、NZDUSD、CHFJPY、USDCAD は今回の固定basketには入れていません。

## 出力

- bot log: `logs/s18_bot.log`
- trades: `logs/s18_trades.csv`
- policy decisions: `logs/s18_policy_decisions.csv`
- state: `state/s18_<symbol>_bot_state.json`

## 起動

Git pull後の反映は既存bot18と同じサービス名で行います。

```bash
sudo docker compose up -d --no-deps --force-recreate exness-bot-18
```

## 確認コマンド

```powershell
python .\live_s18_bot.py --self-test
python .\live_s18_bot.py --self-test --policy-self-test
```
## 旧stateについて

旧単一GBPUSD版の `state/s18_bot_state.json` は、このbasket実装では参照しません。
新しいstateは `state/s18_<symbol>_bot_state.json` として銘柄別に作成されます。
旧bot18の未決済ポジションがMT5上に残っている場合、新実装はmagicが異なるため管理しません。
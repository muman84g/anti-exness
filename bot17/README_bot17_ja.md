# bot17

ForexFactory EA 調査 v2 の tick-only 通貨強弱 Top2 を、bot14 の EA bridge / executor 構成で動かすための live bot。

## 初期候補

- GBPUSD: `v2cs_a9272d85b8a8`
  - `currency_strength_dashboard_rank`
  - M15, TP 28 pips, SL 20 pips, MaxHold 160 bars
  - StrengthWindow 12, Smooth 6, EntryThreshold 0.03, ExitThreshold 0.0105
- USDJPY: `v2cs_b6a062ee7428`
  - 上記と同じ構成、Smooth 3

## 安全設定

`bot17_params.json` は初期状態で `trading_enabled: false`。

この状態では、EA bridge からデータとスプレッドを取得し、シグナルが出ても発注せず `logs/bot17_trades.csv` に `SIGNAL_DRY_RUN` として記録する。

実発注する場合は、MT5 側で bot14 と同じ EA bridge が動いていること、28ペアの M15 履歴が取れること、symbol 名が `GBPUSD` / `USDJPY` / universe と一致していることを確認してから `trading_enabled` を `true` に変更する。

## 起動

Git clone 直後は `live_config.py` が `.gitignore` 対象のため存在しない。必要に応じて以下で作成し、認証情報は環境変数で渡す。

```powershell
copy .\live_config.example.py .\live_config.py
```

```powershell
cd C:\botter\bot\bot17
py .\live_bot17_bot.py --once
```

常駐起動:

```powershell
cd C:\botter\bot\bot17
py .\live_bot17_bot.py
```

## 実装メモ

- シグナルは 28ペアの同期済み M15 確定足から計算する。
- `drop_last_bar_as_forming: true` のため、EA bridge が形成中バーを返しても使わない。
- 初回起動は `prime_on_first_run: true` により catch-up entry を行わない。
- TP/SL は発注時にサーバー側へ入れる。
- 週末ルールは JST 土曜 02:00 新規停止、土曜 02:30 強制クローズ。
- bot14 の live 認証情報はコピーしていない。`live_config.py` は環境変数または bridge 既定値を使う。

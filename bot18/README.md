# bot18 / S18 GBPUSD Snowball Anti-Grid

## 目的

`bot18` は GBPUSD 専用の Snowball / Anti-Grid virtual-entry bot。
EA bridge / executor などの共通部品は `bot14` 系の構成に合わせ、このディレクトリ内へコピーして自己完結させる。

## 固定方針

- symbol: `GBPUSD`
- lot: `0.01`
- grid distance: `5.0 pips`
- autoTP: `1 level`
- base additional entry distance from same-side average: `20.0 pips`
- inactive additional entry distance: `23.5 pips`
- inactive mode: `gate_false`
- H1 trend gate: fresh signal and `TrendAllowed=true` only starts a new cycle
- active cycle中は H1 gate false でもSL / autoTP / virtual level管理を継続する
- fresh H1 signal かつ `TrendAllowed=false` の間だけ追加entry距離を `23.5 pips` に広げる
- stale signal では追加距離を広げない
- weekend position hold: on
- Monday re-anchor: on
- SHORT autoTP reference: Ask
- max entry spread: `9 points`
- exact-net autoTP: off
- loss carry: off
- inactive basket close: off
- rollover block: off
- new-extreme filter: off

## 運用メモ

現在の `s18_params.json` は `live_trading_enabled=true`。
無効化する場合は `live_trading_enabled=false` に変更してから起動する。

state は `state/s18_bot_state.json`、ログは `logs/s18_bot.log`。
Dockerでは `state/` ディレクトリをmountするため、`state/.gitkeep` でディレクトリ作成を保証し、実state JSONはCentOS側へ手動配置する。

`live_config.py` は認証情報を含むためgitに含めない。

## 安全確認

ライブ発注テストは行わない。
構文確認と `--self-test` の純ロジック確認だけを使う。

`live_trading_enabled=true` でbridge/MT5接続へ進む前に、runnerはstate保存可否を検証する。
保存できない場合はfail-closedで起動停止する。

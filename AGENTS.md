# AGENTS.md

## Scope

- Live-bot-specific Codex guidance goes here.

## Notes

- Treat order execution, credentials, account settings, and broker connectivity as high risk.
- Do not change strategy parameters, live execution behavior, or deployment files unless explicitly requested.

## Bot Template / Naming

- 新しい live bot を作る場合は、原則として bot14 をベースにする。
- 新規botの番号はユーザーが指定した番号を使う。番号、用途名、接頭辞、略称をCodex側で独自に決めない。
- 正式名称は既存規則の `botNN` / `sNN` を正本とする。ユーザーの明示指示なしに、別形式のディレクトリ名、ファイル名、サービス名を作らない。
- ファイル名は bot14 の命名に合わせ、対象bot番号へ置き換える。例: `live_s14_bot.py` -> `live_s17_bot.py`。
- ファイル内部の参照名も `s14` 形式に合わせ、対象bot番号へ置き換える。例: `s14_bot.log` -> `s17_bot.log`、`s14_params.json` -> `s17_params.json`、`s14_bot_state.json` -> `s17_bot_state.json`。
- `docker-compose.yml`、README、Teraterm手順、ログpath、params/state/logのpath記載も bot14 の構成に合わせて対象bot番号へ統一する。
- ディレクトリ名やサービス名は既存構成に合わせる。例: ディレクトリは `bot17`、Dockerサービスは `exness-bot-17`、内部ファイル名とログ名は `s17_*`。
- 既存botのファイル名やディレクトリ名は、ユーザーが改名を明示しない限り変更しない。新規bot作成を理由に既存botを整理・改名しない。

## Live Bot Base Policy

- live bot の土台は、bot14 を安全設計の参照元、bot18 を単一戦略の簡潔な構成参照元として扱う。
- bot14 から必ず取り込む考え方は、fail-closed、`pending_open`、`reconciliation_required`、operator確認付きreconciliation、state backup、atomic state save、取引CSV、決済失敗CSVまたは失敗イベント記録、position sync不整合時の新規entry停止である。
- bot18 から取り込む考え方は、対象botディレクトリ内で自己完結する構成、`sNN_*` 命名の統一、params/state/log/trades CSV の単純な対応、戦略固有ロジックと共通安全部品の分離、軽量な `--self-test` である。
- 新規botで bot18 型の単純構成を使う場合でも、bot14 型の安全チェックリストを満たす。満たせない安全機構がある場合は、起動前に理由と代替策をREADMEまたはAGENTSに明記する。
- bot14 の DMC、A/B、multi-symbol、bet-units、特定戦略パラメータは戦略固有なので、必要がある場合だけ取り込む。安全機構と戦略ロジックを混同して丸コピーしない。
- bot18 は簡潔さの参考にはできるが、取引CSV漏れのような移植抜けが起きたため、単独の正本テンプレートとして扱わない。bot18を参照する場合も `log_trade_csv`、全ENTRY/EXIT経路、state保存、position sync、docker volume、README記載を明示確認する。
- 土台としての優先順位は、まず bot14 の安全性、次に bot18 の可読性と自己完結性とする。

## Path Stability

- 既存コード、`docker-compose.yml`、README、運用手順に書かれたpathは既存運用の契約として扱う。
- ユーザーの明示指示なしに、絶対pathと相対pathの相互変更、配置先ディレクトリの変更、Dockerのvolume/workdir、params/state/logの保存先、import参照先を書き換えない。
- 開発元や一時作業場所のpath（例: scratch、Downloads、別プロジェクト）を、本番botのコード・設定・手順へ残さない。
- path変更が必要な場合は、変更理由、影響するファイル、旧path、新path、既存運用への影響、元に戻す方法を先に示し、ユーザー確認後に変更する。
- 新規botでは対象ディレクトリ内を自己完結の基準とし、bot14を参照元にしてもbot14配下を実行時pathとして参照させない。

## New Bot Workflow

- 作成前にbot14と対象botディレクトリの構成を読み、作成・変更するファイル一覧と名称対応（`14 -> NN`）を短く提示する。
- 既存の対象botディレクトリがある場合は、新規作成や上書きの前に内容を確認し、ユーザーの既存ファイルを保持する。
- 変更は対象botと、明示的に必要な共通運用ファイルだけに限定する。無関係なbotは変更しない。
- 作成後は旧番号、旧bot名、scratch path、Downloads path、意図しない絶対pathが残っていないか検索する。
- `docker-compose.yml` を変更した場合は、サービス名、build context、volume、command、ログpathが対象bot番号と一致するか確認する。
- params/state/logの実ファイルをテスト目的で上書きしない。検証ではimport/構文確認など、ライブ発注を行わない方法を優先する。
- 新規bot作成時は `docker-compose.yml` のstate mount方式を確認し、file mountならホスト側のstate JSON実ファイル、directory mountなら対象ディレクトリ内の期待state JSON（例: `state/sNN_bot_state.json`）を起動前に作成する。
- directory mount方式でstateディレクトリを使う場合、gitは空ディレクトリを追跡しないため、`state/.gitkeep` などのplaceholderを置いてディレクトリ作成を保証する。
- state JSONの事前作成後は、対象pathがディレクトリではなく通常ファイルであることを確認する。CentOS/Dockerのbind mountで未作成ファイルpathがディレクトリとして自動生成される状態を残さない。
- stateファイル名がbotごとに異なる場合は、コード上の `STATE_FILE`、`docker-compose.yml`、README/運用手順の記載が同じ実ファイルを指すことを確認する。
- 新規botのlive runnerでは、`live_trading_enabled=true` でbridge/MT5接続へ進む前にstate保存可否を検証し、保存失敗時はfail-closedで起動停止する安全措置を入れる。

## MT5 Bridge Selection

- Live bot bridge sources must be committed under `MetaTrader 5/MQL5/Experts/` using the bot-specific name, for example `BotBridge_s19.mq5` for bot19 and `BotBridge_s20.mq5` for bot20.
- Do not rely on files manually copied into a running container. `docker compose up -d --force-recreate` can discard manual MT5 `MQL5/Experts` changes.
- Each bot service that uses a bot-specific bridge must select it in `docker-compose.yml` with `EA_BRIDGE_EXPERT_NAME=BotBridge_sNN`.
- The selected bridge source should be bind-mounted from `./MetaTrader 5/MQL5/Experts/BotBridge_sNN.mq5` to a simple container path such as `/app/selected_bridge/BotBridge_sNN.mq5`, then referenced with `EA_BRIDGE_SOURCE_FILE=/app/selected_bridge/BotBridge_sNN.mq5`.
- If the startup chart symbol matters, set `EA_BRIDGE_STARTUP_SYMBOL` for that service. The entrypoint must generate a minimal temporary startup config for the selected expert and must not copy account login, password, or server settings into that generated config.
- The entrypoint copies the selected bridge into the Wine MT5 `MQL5/Experts` directory and compiles it on startup. The compiled `.ex5` must exist so the bridge appears in noVNC under MT5 `Navigator -> Expert Advisors`. If compilation fails or the `.ex5` is missing, fail startup instead of running with an invisible or stale bridge.
- If multiple bots run against the same MT5 Files directory, use bot-specific IPC names in both Python and MQL inputs, for example `cmd_s19.txt`, `res_s19.txt`, and `ea_bridge_s19.lock`.
- Treat `.mq5` as the source of truth. Commit `.ex5` only when the user explicitly wants a compiled binary tracked for that bridge.
- When adding a new bot with a new bridge, update `AGENTS.md`, `docker-compose.yml`, and the bot README in the same change so future Codex runs preserve the bridge-selection convention.

## Position Sync Safety

- live botでは、MT5/EA Bridgeのposition一覧取得失敗（例: `ERR|TIMEOUT`、`position sync failed`、`MT5 position list unavailable`）は一時的な同期失敗として扱い、そのcycleの新規entryはfail-closedで止める。
- 一時的なposition同期失敗で `sync_block_new_entries` を立てた場合、次回以降にposition一覧取得が正常成功し、state上の管理ticketとMT5上の建玉ticketが整合し、unmanaged position / missing state ticket / unresolved pending_open / `reconciliation_required` が無いことを確認できたら、その一時ブロックだけ自動解除してよい。
- 自動解除してよいのは、一時的なposition一覧取得失敗に由来する理由だけに限定する。`untracked live positions exist`、`Unmanaged live positions`、`state ticket missing on MT5`、`failed to repair SL/TP`、`open failed`、`pending_open`、`reconciliation_required` などは、後続の正常syncだけで勝手に解除しない。
- ブロック解除時は、どの理由を解除したかをログへ残す。例: `New-entry block cleared after recovery: position sync failed`。
- stateを手動編集してブロック解除する場合は、対象botを停止し、MT5画面またはEA Bridge応答で全管理ticket、symbol、direction、lot、SL/TPがstateと一致することを確認してから行う。
- 動作確認では、stateに `sync_block_new_entries` / `sync_block_reason` / `reconciliation_required` が残っていないか、MT5画面のticketとstate上のticketが一致しているか、ログ末尾に継続的な `ERR|TIMEOUT` が集中していないかを確認する。

## Live Trade CSV Logging

- live bot は `logs/sNN_bot.log` に加えて、取引イベントを `logs/sNN_trades.csv` に記録する。
- CSV名はbot番号と一致させる。例: bot18 は `logs/s18_trades.csv`、bot17 は `logs/s17_trades.csv` とする。
- 最低限、`ENTRY`、正常決済、決済失敗、server-side SL推定、手動決済や同期由来の決済検知をCSV記録対象にする。
- 決済失敗を通常取引CSVから分離する既存bot仕様がある場合は、`logs/sNN_trade_errors.csv` を使ってよい。
- 新規bot作成・既存bot移植時は、`log_trade_csv` 相当の実装と、全ENTRY/EXIT経路からの呼び出しを確認する。
- `log_trade_csv` 相当の実装、`sNN_trades.csv` 名、ENTRY/EXIT呼び出しのいずれかが欠けている場合、そのlive botは運用準備未完了として扱う。
- `sNN_trades.csv`、`sNN_trade_errors.csv`、実行ログ、`logs/` はgitに含めない。

## GitHub Push Workflow

- commit / push はユーザーが明示した場合だけ行う。
- `live_config.py`、`.env`、秘密鍵、トークン、認証情報、本番設定ファイルはgitに含めない。
- `*_bot_state.json` / `s*_bot_state.json` はgitに含めない。必要なstate JSONはCentOS側へ手動配置する。
- `state/` のような空ディレクトリをgitで作る場合は、`state/.gitkeep` などのplaceholderだけをcommitする。
- `logs/`、`__pycache__/`、`.pyc`、実行ログ、取引CSVはgitに含めない。
- push前に `git status --short` と `git diff --cached --name-only` を確認し、意図しないbot、削除、機密ファイル、state JSONがstageされていないことを確認する。
- 既存repoの作業ツリーに大量の未commit変更や削除がある場合は、直接stageせず、`C:\tmp` などへクリーンcloneを作って必要ファイルだけ反映してpushする。
- pushや修正前にbotディレクトリのバックアップを作る場合は、`C:\botter\bot` 直下へ `bot14_backup_...` のように並べず、対象bot配下の `_backups/`（例: `C:\botter\bot\bot14\_backups\bot14_backup_YYYYMMDD_label`）へ作成する。`_backups/` はローカル退避用でgitに含めない。
- 新規botをpushする場合、原則として対象botディレクトリ、`docker-compose.yml`、`AGENTS.md`、必要な運用メモだけをstageし、`live_config.py` と実state JSONは手動配置に残す。
- 既存botの更新をpushする場合、対象botディレクトリ丸ごとではなく、変更した実ファイルだけをstageする。`docker-compose.yml` や `AGENTS.md` は変更した場合だけstageする。


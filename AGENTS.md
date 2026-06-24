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


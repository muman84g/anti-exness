# bot15 を GitHub に上げて CentOS で pull する手順書 cmd版

対象フォルダ:

```text
C:\Users\muuma\.gemini\antigravity\scratch\anti-exness\bot15
```

GitHub リポジトリ:

```text
https://github.com/muman84g/anti-exness.git
```

ブランチ:

```text
main
```

この手順では、Windows 側は `cmd` で実行します。
`bot15/` と `docker-compose.yml` 以外をコミット・push・pull 対象にしないための確認も入れています。
ただし、`bot15/live_config.py` は GitHub に上げません。CentOS 側でいつも通り手動配置・手動編集します。

## 1. cmd でリポジトリへ移動する

cmd を開いて、次を実行します。

```cmd
cd /d C:\Users\muuma\.gemini\antigravity\scratch\anti-exness
```

すでに下のように表示されている場合は、移動済みなのでこのコマンドは不要です。

```text
C:\Users\muuma\.gemini\antigravity\scratch\anti-exness>
```

## 2. 現在の状態を確認する

```cmd
git status --short --branch
```

今回 GitHub に上げてよい対象は次の2つだけです。

```text
bot15/
docker-compose.yml
```

ただし、次のファイルは `bot15/` 配下でも GitHub に上げません。

```text
bot15/live_config.py
```

現在確認できている対象外の変更例:

```text
 M Bot稼働の仕方(Teraterm).txt
 M docker-compose.yml
?? bot15/
```

`Bot稼働の仕方(Teraterm).txt` は今回コミットしません。

## 3. bot15 と docker-compose.yml を確認する

cmd では PowerShell の `Get-ChildItem` は使えません。
代わりに `dir` を使います。

```cmd
git status --short -- bot15
git status --short -- docker-compose.yml
dir /a bot15
```

`bot15` の中身をファイル名だけで確認したい場合:

```cmd
dir /b bot15
```

## 4. live_config.py は GitHub に上げず無視する

`live_config.py` は `.gitignore` で無視される設定になっています。
今回はいつも通り手動で扱うため、GitHub には上げません。

無視対象になっているか確認します。

```cmd
git check-ignore -v bot15/live_config.py
```

次のような表示が出ればOKです。

```text
.gitignore:13:live_config.py        bot15/live_config.py
```

ここで何も表示されない場合は、`live_config.py` が無視対象になっていない可能性があります。
その場合は `git add` する前に確認してください。

## 5. bot15 と docker-compose.yml だけをステージングする

次のコマンドだけを使います。

```cmd
git add -- bot15 docker-compose.yml
```
"Warning出ても無視でいいです"

重要:

```text
git add .
git add -A
git add -f -- bot15/live_config.py
git commit -a
```

上の4つは今回使いません。対象外のファイルや `live_config.py` が混ざる可能性があります。

## 6. ステージング内容を自動検査する

次のコマンドを、cmd に1行ずつ貼り付けて実行します。

```cmd
git diff --cached --name-status
```


## 7. コミットする

念のため、コミットにも `-- bot15 docker-compose.yml` を付けて対象を限定します。

```cmd
git commit -m "Add bot15 live trading bot" -- bot15 docker-compose.yml
```

## 8. コミット内容を自動検査する

次のコマンドを、cmd に1行ずつ貼り付けて実行します。

```cmd
git show --name-status --oneline HEAD
```
botフォルダ、docker-composeファイルの2つが綺麗に出ない場合

```cmd
git diff --name-status origin/main..HEAD
```
これを実行して、下記のように出ればok
C:\Users\muuma\.gemini\antigravity\scratch\anti-exness>git diff --name-status origin/main..HEAD
A       bot15/base_interfaces.py
A       bot15/ea_bridge.py
A       bot15/live_data_fetcher.py
A       bot15/live_executor.py
A       bot15/live_s15_bot.py
A       bot15/s15_bot_state.json
A       bot15/s15_params.json
M       docker-compose.yml

## 9. GitHub に push する

```cmd
git push origin main
```

push 後の確認:

```cmd
git status --short --branch
```

対象外のローカル変更が残っていても問題ありません。
今回の commit / push に含めていなければ、CentOS 側には反映されません。

## 10. CentOS 側で pull 前に差分を検査する

CentOS に SSH で入ります。

```bash
ssh ユーザー名@サーバーIP
```

anti-exness のリポジトリへ移動します。

```bash
cd /path/to/anti-exness
```

実際の場所が分からない場合:

```bash
find ~ -type d -name anti-exness 2>/dev/null
```

GitHub の最新情報だけ取得します。まだ作業ファイルは変更されません。

```bash
git fetch origin
```

これから pull した場合に入ってくるファイル一覧を確認します。

```bash
git diff --name-status HEAD..origin/main
```

問題なければ下記を実行してpullします

```bash
git pull --ff-only origin main
```

`--ff-only` を付けることで、CentOS 側で余計なマージコミットを作らないようにします。

## 11. CentOS 側で bot15 を確認する

```bash
ls -la bot15
git status --short -- bot15
```

`live_config.py` は GitHub では送らないため、pull 直後には無い場合があります。
無い場合は正常です。

```bash
ls -la bot15/live_config.py
```

## 12. CentOS 側で live_config.py を手動配置する

いつも通り手動で `bot15/live_config.py` を配置します。
CentOS 上で直接作る場合:

```bash
nano bot15/live_config.py
```

例:

```python
MT5_LOGIN = 12345678
MT5_PASSWORD = "本番パスワード"
MT5_SERVER = "Exness-MT5RealXX"
```

保存後、この本番値は GitHub に push しないでください。

## 13. noVNC の URL を確認する

前回 bot14 では次の URL を使っていました。

```text
http://118.27.2.117:6089/vnc.html
```

bot15 で同じコンテナを使う場合は同じ URL の可能性があります。
別コンテナの場合は CentOS 側でポートを確認します。

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}' | grep -i exness
```

`6089->6080` のような表示があれば、ブラウザでは次の形で開きます。

```text
http://サーバーIP:6089/vnc.html
```

## 14. bot15 を起動する

リポジトリ直下で実行します。

```bash
py bot15/live_s15_bot.py
```

`py` が無い環境では次を試します。

```bash
python3 bot15/live_s15_bot.py
```

## 15. 今回の安全ルールまとめ

今回使ってよい cmd コマンド:

```cmd
git add -- bot15 docker-compose.yml
git commit -m "Add bot15 live trading bot" -- bot15 docker-compose.yml
git push origin main
```

今回使わないコマンド:

```cmd
git add .
git add -A
git add -f -- bot15/live_config.py
git commit -a
git reset --hard
git checkout -- .
git push --force
```

必ず確認すること:

```text
1. Windows 側のステージングが bot15/ と docker-compose.yml だけ
2. ただし bot15/live_config.py はステージングに含めない
3. Windows 側の最新コミットが bot15/ と docker-compose.yml だけ
4. ただし bot15/live_config.py は最新コミットに含めない
5. CentOS 側で pull 予定の差分が bot15/ と docker-compose.yml だけ
6. ただし bot15/live_config.py は pull 予定の差分に含めない
7. live_config.py は CentOS 側で手動配置・手動編集する
```

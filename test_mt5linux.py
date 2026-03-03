"""
test_mt5linux.py
CentOS 上で mt5linux 経由の MT5 接続を確認するテストスクリプト。
"""

import sys
import platform

print(f"Python: {sys.version}")
print(f"OS: {platform.system()}")

# mt5_compat 経由で mt5 を取得
from mt5_compat import mt5
from live_config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER

# mt5linux 使用時はまず MetaTrader5 サーバー (Wine側) を起動する必要がある
# run_server.sh を先に実行してから、このスクリプトを実行してください。

print("\nMT5への接続テスト開始...")

if platform.system() == "Windows":
    from live_config import MT5_PATH
    ok = mt5.initialize(path=MT5_PATH, login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
else:
    # Linux (mt5linux) では host/port 指定で Wine 内の MT5 と通信
    # mt5linux の MetaTrader5() は自動でローカルソケットに接続する
    ok = mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)

if not ok:
    print(f"[FAIL] MT5 初期化失敗: {mt5.last_error()}")
    print("\nヒント:")
    print("  Linux の場合: deploy/run_server.sh を先に別ターミナルで実行してください。")
    sys.exit(1)

account = mt5.account_info()
if account:
    print(f"[OK] ログイン成功!")
    print(f"     ブローカー: {account.company}")
    print(f"     サーバー:   {account.server}")
    print(f"     口座番号:   {account.login}")
    print(f"     残高:       {account.balance} USD")
else:
    print("[FAIL] アカウント情報の取得に失敗しました。")

mt5.shutdown()
print("\nテスト完了。")

#!/bin/bash
export DISPLAY=:99

echo "Focusing MetaTrader 5..."
xdotool search --name "MetaTrader 5" windowactivate
sleep 1

echo "Opening Navigator (Ctrl+N)..."
xdotool key ctrl+n
sleep 1
xdotool key ctrl+n
sleep 1

# Navigator に移動 (Shift+Tab の可能性があるが、単純にマウスでチャート内をクリックしてから Navigator ショートカット等)

# マウスでドラッグアンドドロップ！
# Navigator 内の Expert Advisors -> BotBridge
# ツリーが折りたたまれている可能性がある。
# (1) x=55, y=390 (Expert Advisors の文字の左の [+] アイコン付近) を展開
xdotool mousemove 50 410 click 1
sleep 0.5
xdotool key Right
sleep 0.5

# (2) BotBridge が展開されたとして、その下(y=425あたり)をクリック
xdotool mousemove 60 425 click 1
sleep 0.5

# (3) Enterでチャートへアタッチを試みる
xdotool key Return
sleep 1
# 確認ダイアログ(Algo Trading許可など)が出たら Enter
xdotool key Return
sleep 1

# (4) 念のためもう一つ下の座標も
xdotool mousemove 60 440 click 1
sleep 0.5
xdotool key Return
sleep 1
xdotool key Return

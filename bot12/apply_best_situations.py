import os
import json

# スクリプト自身のディレクトリを基準にした相対パス設定
script_dir = os.path.dirname(os.path.abspath(__file__))
SITUATIONS_FILE = os.path.join(script_dir, "s12_best_situations.json")
PARAMS_FILE = os.path.join(script_dir, "s12_params.json")

def main():
    print("=== APPLYING BEST LOGGED SITUATIONS TO BOT ===")
    
    # 1. ログファイルの読み込み
    if not os.path.exists(SITUATIONS_FILE):
        print(f"Situations file not found: {SITUATIONS_FILE}")
        return
        
    try:
        with open(SITUATIONS_FILE, "r") as f:
            situations = json.load(f)
    except Exception as e:
        print(f"Error opening situations file: {e}")
        return
        
    # 最新のシチュエーション（都度保存された最後のシチュエーション）を銘柄ごとにグルーピング
    best_by_symbol = {}
    for sit in situations:
        sym = sit['symbol']
        # 最新のログで上書き
        best_by_symbol[sym] = sit
        
    # 2. パラメータファイルの読み込み
    if not os.path.exists(PARAMS_FILE):
        print(f"Params file not found: {PARAMS_FILE}")
        return
        
    with open(PARAMS_FILE, "r") as f:
        params = json.load(f)
        
    # 3. 最良設定の適用
    for sym in ['GBPNZDm', 'NZDCHFm', 'USDJPYm']:
        if sym in best_by_symbol:
            sit = best_by_symbol[sym]
            strategy = params['strategies'][sym]
            
            # パラメータの更新
            strategy['hold_bars'] = int(sit.get('hold_bars', 48))
            strategy['tp_mult'] = float(sit.get('tp_mult', 0.0))
            strategy['sl_mult'] = float(sit.get('sl_mult', 0.0))
            strategy['filter_type'] = sit.get('filter_type', 'None')
            strategy['filter_param'] = float(sit.get('filter_param', 0.0))
            
            # ロットサイズの管理
            # 既存のロットサイズが設定されており、かつ0.0より大きい場合はそれを維持
            current_lot = strategy.get('lot_size', 0.0)
            if current_lot > 0.0:
                strategy['lot_size'] = current_lot
            else:
                strategy['lot_size'] = 0.05 # 新規で有効な設定が見つかったため0.05で有効化
                
            print(f"Applied {sym} best config: hold={strategy['hold_bars']}, tp={strategy['tp_mult']}, filter={strategy['filter_type']}({strategy['filter_param']}), lot={strategy['lot_size']}")
        else:
            # 利益の出る設定が見つからなかった場合、既存のパラメータのフィルタをNoneにして、必要に応じて初期化
            strategy = params['strategies'][sym]
            strategy.setdefault('filter_type', 'None')
            strategy.setdefault('filter_param', 0.0)
            
            # 利益が出る設定がないものはロットサイズを0.0にして無効化（USDJPYmなど）
            strategy['lot_size'] = 0.0
            print(f"{sym} has no profitable situations in logs. Disabled (lot_size=0.0)")
            
    # 4. パラメータファイルの保存
    with open(PARAMS_FILE, "w") as f:
        json.dump(params, f, indent=4)
    print("Successfully updated s12_params.json with best situations!")

if __name__ == "__main__":
    main()

# 実運用ボット `bot8` 技術仕様・実行フロー説明書 (Technical Specification)

本ドキュメントは、Exness MT5上で稼働している取引ボット **`bot8`（S8ロバスト戦略）** の全体設計、判定ロジック、計算式、および実行フローをまとめた技術仕様書です。今後のシステム保守やパラメータ変更の参考資料として活用してください。

---

## 1. 全体システム構成 (System Architecture)

本システムは、Dockerコンテナ環境（Linux/Wine）を活用し、PythonスクリプトとMT5ターミナル間をファイルを用いた高速IPC（都市間通信）で繋ぐ構成になっています。

```mermaid
graph TD
    subgraph Host OS (Linux VPS)
        subgraph Docker Container (exness-bot-8)
            Bot[live_s8_bot.py] <-->|Pythonクラス呼び出し| Fetcher[live_data_fetcher.py]
            Bot <-->|Pythonクラス呼び出し| Executor[live_executor.py]
            Fetcher <-->|コマンド送信/レスポンス読込| Bridge[ea_bridge.py]
            Executor <-->|コマンド送信/レスポンス読込| Bridge
            
            subgraph Wine Environment
                Bridge <-->|cmd.txt / res.txt IPC| EA[EA Bridge.ex5]
                EA <-->|MQL5 API| MT5[MetaTrader 5 Terminal]
            end
        end
    end
    MT5 <-->|注文/レート同期| Broker((Exness Trade Server))
```

---

## 2. 実行スケジュール・フロー (Execution Flow)

ボットは **5分毎** に起動し、現在の日本時間（JST）の時刻情報に基づいて以下の判定サイクルを巡回します。

```mermaid
flowchart TD
    Start([5分毎のポーリング起動]) --> TimeCheck{現在時刻の JST 分を判定}
    
    %% 強制決済判定
    TimeCheck -->|JST H:55 〜 H:59| ExitPhase[強制決済フェーズ]
    ExitPhase --> ActivePos{アクティブポジションはあるか?}
    ActivePos -->|Yes| CloseOrder[MT5へ成行決済注文を送信]
    CloseOrder --> LogExit[s8_trades.csv / s8_bot.log に記録]
    LogExit --> SaveState[状態ファイルを保存 s8_bot_state.json]
    SaveState --> End([サイクル終了])
    ActivePos -->|No| End
    
    %% エントリー判定
    TimeCheck -->|JST H:00 〜 H:04| EntryPhase[エントリー判定フェーズ]
    EntryPhase --> HourCheck{現在のローカル市場時間は<br/>アノマリー時間帯窓に入っているか?}
    
    %% アノマリー窓判定
    HourCheck -->|Yes (銅 NY 20時)| FetchData[MT5から全21銘柄の過去1600足データをロード]
    HourCheck -->|No| End
    
    %% データ前処理 & 特徴量
    FetchData --> FeatGen[V23互換特徴量生成 <br/> 71/68種類の特徴量を計算]
    FeatGen --> GetConfirmed[前足の確定値ベクトルを取得 iloc -2]
    GetConfirmed --> Winsor[Winsorization クリップ適用 <br/> s8_pipeline_meta.json より上下限読込]
    Winsor --> SelectFeats[相関除去特徴量のみ抽出]
    SelectFeats --> Predict[LightGBMモデルで予測確率を計算]
    
    %% 確率閾値判定
    Predict --> ProbCheck{予測確率は閾値以上か?<br/>銅: 0.54}
    ProbCheck -->|Yes| CalcLot[動的ロットサイズ計算<br/>$10 リスクベース]
    CalcLot --> OpenOrder[MT5へ成行新規注文を送信]
    OpenOrder --> SaveStateEntry[状態ファイルを保存 s8_bot_state.json]
    SaveStateEntry --> LogEntry[s8_trades.csv / s8_bot.log に記録]
    LogEntry --> End
    ProbCheck -->|No| End
    
    TimeCheck -->|その他 H:05 〜 H:54| End
```

---

## 3. シグナル判定アルゴリズムと前処理計算式

アノマリー時間帯に進入した際、エントリー可否を判断する機械学習（LightGBM）モデルへの入力データを構築するための前処理フローです。

検証期間（Validation）を無事通過した「本質的に頑健な1アセット（銅）」のみを取引します。
- **Traded Assets**: XCUUSDm (工業用銅) - 確率閾値 0.54

### ① 特徴量生成式 (Feature Engineering)
入力される5分足データをもとに、以下の特徴量（スケールフリー特徴量）をリアルタイムに計算します。
- **リターン (Return)**:
  $$\text{ret}_k = \frac{\text{Close}_t - \text{Close}_{t-k}}{\text{Close}_{t-k}}$$
- **移動ボラティリティ (Volatility)**:
  $$\text{vol}_{k} = \text{std}(\text{ret}_1)_{[t-k, t]}$$
- **RSI (Relative Strength Index)**:
  $$\text{RSI}_{14} = 100 - \frac{100}{1 + \text{RS}}$$ (※RS = 14期間の上昇幅指数移動平均 / 下落幅指数移動平均)
- **ボリンジャーバンド %b**:
  $$\%b = \frac{\text{Close} - \text{LowerBand}}{\text{UpperBand} - \text{LowerBand}}$$
- **Garman-Klass ボラティリティ** (価格のレンジと窓開きを考慮した高度なボラティリティ):
  $$\sigma_{GK}^2 = 0.5 \left( \ln \frac{\text{High}}{\text{Low}} \right)^2 - (2\ln 2 - 1) \left( \ln \frac{\text{Close}}{\text{Open}} \right)^2$$
- **先行指標スプレッド**:
  $$\text{Spread}_k = \text{ret}_{k,\text{target}} - \text{ret}_{k,\text{lead}}$$
- **バスケットスプレッド**:
  $$\text{BasketSpread}_k = \text{ret}_{k,\text{target}} - \text{BasketReturn}_k$$ (※各種加重平均バスケット)

### ② パイプライン前処理 (Winsorization & 相関除去)
予測を行う前に、ヒストリカル学習時に決定されたパラメータを用いて特徴量データをクレンジングします。
- **Winsorization (外れ値クリップ)**:
  実運用中の急激な値動き（スパイクなど）によるモデルの誤作動を防ぐため、事前に Train 期間から算出された「1%点（`lower`）」および「99%点（`upper`）」の限界値にデータをクリップします。
  $$\hat{x} = \min(\max(x, \text{lower}), \text{upper})$$
- **相関除去（列ドロップ）**:
  多重共線性によるノイズを防ぐため、Train 期間において相関係数が `0.90` を超えてドロップされた不要な特徴量列を除外し、モデルが期待するクリーンな特徴量（銅: 71列）のベクトルのみを抽出します。

---

## 4. 動的ロットサイズ計算式 (Dynamic Lot Sizing)

ボットは、市場のボラティリティに応じて取引数量（ロット）を動的に調節し、すべての取引で**期待損失リスクが約 $10 になるように均一化（リスクパリティ）**します。

### ロットサイズ算出の流れ

1. **取引保有期間（4時間＝48足分）の価格変動差分を算出**:
   $$\Delta P_t = P_t - P_{t-48}$$
2. **過去1440期間（約5営業日分）における価格変動の標準偏差（ボラティリティ）を計算**:
   $$\sigma_{\Delta P} = \text{std}(\Delta P)_{[t-1440, t]}$$
3. **1ロットあたりの取引通貨変動価値（USD換算）を算出**:
   $$\text{USD Value Per Lot} = \sigma_{\Delta P} \times \text{LotMultiplier}_{\text{USD}}$$
   
   *(※ $\text{LotMultiplier}_{\text{USD}}$ は、1ロット取引した際の契約価値をUSDに換算する係数。銅 (`XCUUSDm`): $100$)*
4. **目標ロットサイズの計算**:
   $$\text{Target Lot} = \frac{\text{RISK\_USD} \ (\approx \$10.0)}{\text{USD Value Per Lot}}$$
5. **ブローカー制限による丸め処理**:
   ブローカー（Exness）が規定するシンボルの契約最小ロット、最大ロット、およびロット刻み幅（`volume_step`、通常 0.01）に基づいて最終調整します。
   $$\text{Final Lot} = \text{Round}\left(\max(\text{MinVolume}, \min(\text{Target Lot}, \text{MaxVolume})), \text{Step}\right)$$
## Final Live Profile - 2026-06-02

- Strategy: baseline anomaly window entry for XCUUSDm.
- ML filter: disabled for live execution.
- Entry window: XCUUSDm NY local hour 20, LONG.
- Lot plan: normal 0.20 lot, high-zone 0.10 lot.
- High-zone rule: XCUUSDm current close > cached M5 rolling 60-day/17280-bar 80% percentile.
- Market cache: bot8/cache/s8_market_cache.sqlite, seeded with 30000 XCU M5 bars and refreshed hourly with 5000 bars.
- Failsafe: if cache is missing, stale, or insufficient, use safe high-zone lot 0.10.

"""
Spread & Z-Score Module — compute the normalized spread and Z-score
for a given pair of assets.
"""

import numpy as np
import pandas as pd
from config import ZSCORE_LOOKBACK_BARS


def compute_spread(
    price_a: pd.Series,
    price_b: pd.Series,
    beta: float | pd.Series,
) -> pd.Series:
    """
    Compute the spread: S_t = A_t - β_t * B_t

    Both series must share the same DatetimeIndex.
    If beta is a Series (dynamic), it calculates element-wise.
    """
    if isinstance(beta, pd.Series):
        # Align indices
        common = price_a.index.intersection(price_b.index).intersection(beta.index)
        return price_a.loc[common] - beta.loc[common] * price_b.loc[common]
    
    return price_a - beta * price_b


def compute_zscore(
    spread: pd.Series,
    lookback: int = ZSCORE_LOOKBACK_BARS,
) -> pd.Series:
    """
    Compute the rolling Z-score of the spread.

    Z_t = (S_t - μ_rolling) / σ_rolling

    lookback is in number of bars (e.g. 48 × 15min = 12 hours).
    """
    rolling_mean = spread.rolling(window=lookback, min_periods=max(10, lookback // 2)).mean()
    rolling_std  = spread.rolling(window=lookback, min_periods=max(10, lookback // 2)).std()

    # Avoid division by zero
    rolling_std = rolling_std.replace(0, np.nan)

    zscore = (spread - rolling_mean) / rolling_std
    return zscore


def compute_half_life_15m(spread: pd.Series) -> float:
    """
    Half-life of mean reversion on 15-min spread data.
    Returns half-life in minutes (bars × 15).
    """
    spread = spread.dropna()
    if len(spread) < 20:
        return np.inf

    y = spread.values[1:]
    x = spread.values[:-1]

    x_with_const = np.column_stack([x, np.ones(len(x))])
    try:
        phi, _ = np.linalg.lstsq(x_with_const, y, rcond=None)[0]
    except Exception:
        return np.inf

    if phi <= 0 or phi >= 1:
        return np.inf

    hl_bars = -np.log(2) / np.log(phi)
    return hl_bars * 15  # convert to minutes

def calculate_spread_history(df_prices, leg1, leg2):
    """
    Wrapper for live_main.py.
    Calculates spread using OLS beta and returns a DataFrame with 'Spread' and 'Z-Score'.
    """
    price_a = df_prices[leg1]
    price_b = df_prices[leg2]
    
    # Compute beta dynamically for the history provided
    from pairs import _compute_hedge_ratio
    beta = _compute_hedge_ratio(price_a, price_b)
    
    spread_values = compute_spread(price_a, price_b, beta)
    zscore_values = compute_zscore(spread_values)
    
    res = pd.DataFrame(index=spread_values.index)
    res['Spread'] = spread_values
    res['Z-Score'] = zscore_values
    return res

def compute_rolling_zscore(spread_df, window=48):
    """
    Wrapper for live_main.py.
    Expects DataFrame with 'Spread' column or a Series.
    """
    if isinstance(spread_df, pd.DataFrame):
        s = spread_df['Spread']
    else:
        s = spread_df
        
    return compute_zscore(s, lookback=window)

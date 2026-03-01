"""
Pair Selection — identify tradeable pairs using correlation + cointegration.

For each walk-forward training window:
1. Compute rolling Pearson correlation on 1H returns
2. Filter cross-category pairs with |corr| >= threshold
3. Run ADF cointegration test on the price spread
4. Calculate half-life via Ornstein-Uhlenbeck AR(1)
5. Keep only pairs that pass all filters
"""

import logging
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller
from statsmodels.tsa.stattools import adfuller
from config import (
    CATEGORIES,
    LT_CORR_THRESHOLD,
    HIGH_CORR_THRESHOLD,
    HIGH_VOL_CATEGORIES,
    MAX_HALF_LIFE_MINUTES,
    CROSS_CATEGORY_ONLY
)

logger = logging.getLogger(__name__)


def _compute_hedge_ratio(price_a: pd.Series, price_b: pd.Series) -> float:
    """OLS hedge ratio: price_a = β * price_b + ε → returns β."""
    # Simple OLS: β = cov(a,b) / var(b)
    b_clean = price_b.dropna()
    a_clean = price_a.reindex(b_clean.index).dropna()
    b_clean = b_clean.reindex(a_clean.index)

    if len(a_clean) < 10:
        return np.nan

    cov = np.cov(a_clean.values, b_clean.values)
    var_b = cov[1, 1]
    if var_b == 0:
        return np.nan
    return cov[0, 1] / var_b


def _compute_half_life(spread: pd.Series) -> float:
    """
    Half-life of mean reversion via AR(1) model.

    spread_t = φ * spread_{t-1} + ε
    half_life = -ln(2) / ln(φ)

    Returns half-life in number of bars (multiply by bar duration for minutes).
    """
    spread = spread.dropna()
    if len(spread) < 20:
        return np.inf

    y = spread.values[1:]
    x = spread.values[:-1]

    # OLS: y = φ*x + c
    x_with_const = np.column_stack([x, np.ones(len(x))])
    try:
        phi, _ = np.linalg.lstsq(x_with_const, y, rcond=None)[0]
    except Exception:
        return np.inf

    if phi <= 0 or phi >= 1:
        return np.inf

    half_life = -np.log(2) / np.log(phi)
    return half_life


def _adf_test(spread: pd.Series, significance: float = 0.10) -> tuple[bool, float]:
    """
    Augmented Dickey-Fuller test for stationarity.
    Returns (is_stationary, p_value).
    """
    spread = spread.dropna()
    if len(spread) < 30:
        return False, 1.0
    try:
        result = adfuller(spread.values, maxlag=10, autolag="AIC")
        p_value = result[1]
        return p_value < significance, p_value
    except Exception:
        return False, 1.0


def select_pairs(
    df_1h: pd.DataFrame,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    bar_minutes_st: int = 15,
    min_corr: float = LT_CORR_THRESHOLD,
    max_half_life: float = MAX_HALF_LIFE_MINUTES,
    cross_category_only: bool = CROSS_CATEGORY_ONLY,
) -> list[dict]:
    """
    Select tradeable pairs from a training window of 1H data.

    Returns list of dicts:
        {
            "a": str,         # asset name A
            "b": str,         # asset name B
            "corr": float,    # Pearson correlation
            "beta": float,    # hedge ratio (A = β*B + spread)
            "half_life_min": float,  # half-life in minutes
        }
    """
    # Slice training window
    window = df_1h.loc[train_start:train_end]
    if len(window) < 50:
        logger.warning(f"Training window too short: {len(window)} rows")
        return []

    # Compute returns-based correlation matrix
    returns = window.pct_change()
    if returns.empty:
        return []

    corr_matrix = returns.corr()
    cols = corr_matrix.columns.tolist()

    # Enumerate pairs with high correlation
    candidates = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = cols[i], cols[j]

            # Category filter
            cat_a = CATEGORIES.get(a, "Unknown")
            cat_b = CATEGORIES.get(b, "Unknown")
            if cross_category_only and cat_a == cat_b:
                continue

            # Dynamic Correlation Threshold
            # If either asset is in a high-volatility category, require higher correlation
            threshold = min_corr
            if cat_a in HIGH_VOL_CATEGORIES or cat_b in HIGH_VOL_CATEGORIES:
                threshold = HIGH_CORR_THRESHOLD

            corr_val = corr_matrix.iloc[i, j]
            if pd.isna(corr_val) or abs(corr_val) < threshold:
                continue

            candidates.append((a, b, corr_val))

    filter_label = "cross-category" if cross_category_only else "all"
    logger.info(f"  Correlation filter: {len(candidates)} {filter_label} pairs (dynamic threshold >= {min_corr}/{HIGH_CORR_THRESHOLD})")

    # For each candidate, compute hedge ratio, spread, cointegration, half-life
    selected = []
    fail_reasons = {"align": 0, "beta": 0, "adf": 0, "half_life": 0}

    for a, b, corr_val in candidates:
        price_a = window[a].dropna()
        price_b = window[b].dropna()

        # Align
        common_idx = price_a.index.intersection(price_b.index)
        if len(common_idx) < 30:
            fail_reasons["align"] += 1
            continue
        price_a = price_a.loc[common_idx]
        price_b = price_b.loc[common_idx]

        # Hedge ratio
        beta = _compute_hedge_ratio(price_a, price_b)
        if np.isnan(beta):
            fail_reasons["beta"] += 1
            continue

        # Spread
        spread = price_a - beta * price_b

        # Cointegration test (ADF) — soft filter
        # Prefer p < 0.10, but allow p < 0.20 (weaker evidence)
        adf_pass, adf_p = _adf_test(spread, significance=0.20)
        if not adf_pass:
            fail_reasons["adf"] += 1
            continue

        # Half-life (in 1H bars → convert to minutes)
        hl_bars = _compute_half_life(spread)
        hl_minutes = hl_bars * 60  # 1H bars → minutes

        if hl_minutes <= 0 or hl_minutes > max_half_life:
            fail_reasons["half_life"] += 1
            continue

        selected.append({
            "a": a,
            "b": b,
            "corr": round(corr_val, 4),
            "beta": round(beta, 6),
            "half_life_min": round(hl_minutes, 1),
            "adf_p": round(adf_p, 4),
        })

    if candidates:
        logger.info(f"  Filter breakdown: align={fail_reasons['align']}, "
                    f"beta={fail_reasons['beta']}, adf={fail_reasons['adf']}, "
                    f"half_life={fail_reasons['half_life']} → {len(selected)} passed")
    return selected

def find_cointegrated_pairs(df_h1, min_corr=LT_CORR_THRESHOLD):
    """
    Wrapper for live_main.py.
    Uses the entire dataframe index as the training window.
    """
    if df_h1.empty:
        return []
        
    train_start = df_h1.index[0]
    train_end = df_h1.index[-1]
    
    return select_pairs(
        df_h1, 
        train_start, 
        train_end, 
        min_corr=min_corr
    )

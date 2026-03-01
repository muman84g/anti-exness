"""
Machine Learning Strategy Module.
Implements a Rolling Random Forest Classifier to filter trade entries.

Features:
1. Z-Score (Entry strength)
2. Half-Life (Mean reversion speed)
3. Volatility (Spread standard deviation)
4. Correlation (Asset relationship strength)
5. Time of Day (Hour UTC) - Captures session biases (EU/US open)
"""

import logging
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
# from sklearn.ensemble import RandomForestClassifier
from sklearn.base import clone

logger = logging.getLogger(__name__)

def create_ml_features(spread_df):
    """
    Generates features for ML model from spread data.
    spread_df: DataFrame with 'Spread' and 'Z-Score' columns (from spread.py)
    """
    df = spread_df.copy()
    
    # 1. Z-Score (already exists or compute it if missing)
    if 'Z-Score' not in df.columns:
        # Fallback if compute_rolling_zscore wasn't called yet
        # (Using local import to avoid circular dependency if any)
        from spread import compute_rolling_zscore
        df['Z-Score'] = compute_rolling_zscore(df, window=48)
        
    # 2. Volatility (Standard Deviation of spread)
    df['volatility'] = df['Spread'].rolling(window=20).std()
    
    # 3. Z-Score Momentum (change in z-score)
    df['z_mom'] = df['Z-Score'].diff(3)
    
    # 4. Hour of day (UTC)
    df['hour_utc'] = df.index.hour
    
    # Feature columns
    features = pd.DataFrame(index=df.index)
    features['z_entry'] = df['Z-Score']
    features['volatility'] = df['volatility']
    features['z_mom'] = df['z_mom']
    features['hour_utc'] = df['hour_utc']
    
    return features

def create_labels(spread_df, entry_z_threshold, lookahead=24):
    """
    Creates target labels: 1 if spread mean-reverts profitably, 0 otherwise.
    lookahead: How many bars to look forward for reversion.
    """
    df = spread_df.copy()
    if 'Z-Score' not in df.columns:
        return pd.Series(dtype=float)

    z = df['Z-Score']
    targets = pd.Series(index=df.index, data=np.nan, name='Target')
    
    for i in range(len(df) - lookahead):
        curr_z = z.iloc[i]
        future_zs = z.iloc[i+1 : i+1+lookahead]
        
        if curr_z >= entry_z_threshold:
            # Short spread: Win if it goes down to 0
            targets.iloc[i] = 1 if (future_zs <= 0).any() else 0
        elif curr_z <= -entry_z_threshold:
            # Long spread: Win if it goes up to 0
            targets.iloc[i] = 1 if (future_zs >= 0).any() else 0
            
    return targets

def train_ml_model(X, y):
    """Trains a LightGBM model."""
    model = LGBMClassifier(
        n_estimators=50,
        max_depth=3,
        num_leaves=7,
        learning_rate=0.1,
        random_state=42,
        verbosity=-1
    )
    model.fit(X, y)
    return model

class RollingMLFilter:
    def __init__(self, retrain_days: int = 7, min_trades: int = 50):
        """
        Args:
            retrain_days (int): How often to retrain the model (in simulation days).
            min_trades (int): Minimum closed trades required to start training.
        """
        self.model = LGBMClassifier(
            n_estimators=100,
            max_depth=3,           # Reduced from 5 for speed (less complex trees)
            num_leaves=7,          # 2^3 - 1, prevents overfitting and faster
            learning_rate=0.1,
            min_child_samples=20,  # Minimum data needed in a leaf (faster split search)
            subsample=0.8,         # 80% of data used per tree (faster)
            colsample_bytree=0.8,  # 80% of features used per tree (faster)
            random_state=42,
            n_jobs=1,              # Set to 1 because we will parallelize the outer loop
            verbosity=-1
        )
        self.retrain_days = retrain_days
        self.min_trades = min_trades
        self.last_train_time = None
        self.is_ready = False
        
        # Storage for trade history (X: features, y: target)
        self.feature_cols = [
            "z_entry", "half_life", "volatility", "corr", "hour_utc"
        ]
        self.history = []

    def record_trade(self, features: dict, pnl: float):
        """
        Record the result of a closed trade.
        Target: 1 if PnL > 0, else 0.
        """
        row = {col: features.get(col, 0) for col in self.feature_cols}
        row["target"] = 1 if pnl > 0 else 0
        row["exit_time"] = features.get("exit_time") # Used for periodic retraining check
        self.history.append(row)

    def train_if_needed(self, current_time: pd.Timestamp):
        """
        Check if we need to retrain the model based on time elapsed.
        """
        if len(self.history) < self.min_trades:
            return

        # If never trained, or enough time passed since last train
        if (self.last_train_time is None) or \
           (current_time - self.last_train_time).days >= self.retrain_days:
            
            df = pd.DataFrame(self.history)
            X = df[self.feature_cols]
            y = df["target"]
            
            # Simple walk-forward: train on all past data
            try:
                self.model.fit(X, y)
                self.is_ready = True
                self.last_train_time = current_time
                
                # Log feature importance (optional debug)
                # importances = dict(zip(self.feature_cols, self.model.feature_importances_))
                # logger.info(f"ML Retrained. Valid Trades: {len(df)}. Importances: {importances}")
                
            except Exception as e:
                logger.error(f"ML Training failed: {e}")
                self.is_ready = False

    def predict_proba(self, features: dict) -> float:
        """
        Predict probability of profit for a candidate trade.
        Returns 0.5 (neutral) if model is not ready.
        """
        if not self.is_ready:
            return 0.5 # Default allow/neutral
            
        try:
            # Prepare single-row dataframe
            row = pd.DataFrame([features], columns=self.feature_cols)
            # Probability of class 1 (Win)
            prob = self.model.predict_proba(row)[0][1]
            return prob
        except Exception as e:
            logger.error(f"ML Prediction failed: {e}")
            return 0.5

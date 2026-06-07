#py -u "C:\Users\muuma\.gemini\antigravity\scratch\anti-backtest\output\backtest14\run_s14_live_aligned_grid_search.py" --preset full --workers 4 --top-n 30 --save-top-trades 10
import argparse
import itertools
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent))
from run_backtest_move_catcher import MonteCarloManager, close_trade, find_first_ge, find_first_le


WORKSPACE_DIR = Path(r"C:\Users\muuma\.gemini\antigravity\scratch\anti-backtest")
DATA_DIR = WORKSPACE_DIR / "data"
OUTPUT_DIR = WORKSPACE_DIR / "output" / "backtest14" / "s14_live_aligned_grid_results"


def is_weekend_jst(t_jst, stop_hour=20, monday_start_hour=7):
    if t_jst.weekday() == 4 and t_jst.hour >= stop_hour:
        return True
    if t_jst.weekday() in (5, 6):
        return True
    if t_jst.weekday() == 0 and t_jst.hour < monday_start_hour:
        return True
    return False


def is_in_news_window(t_jst, macro_times, hours):
    if hours <= 0 or not macro_times:
        return False
    dt = pd.Timedelta(hours=hours)
    for mt in macro_times:
        if mt - dt <= t_jst <= mt + dt:
            return True
    return False


def get_next_tick_outside_news(times, start_idx, macro_times, hours):
    if hours <= 0 or not macro_times:
        return start_idx
    t_curr = pd.Timestamp(times[start_idx])
    dt = pd.Timedelta(hours=hours)
    for mt in macro_times:
        if mt - dt <= t_curr <= mt + dt:
            return int(times.searchsorted(mt + dt))
    return start_idx


def find_first_true(mask, start):
    if start >= len(mask):
        return len(mask)
    rel = np.flatnonzero(mask[start:])
    if len(rel) == 0:
        return len(mask)
    return start + int(rel[0])


def find_first_false(mask, start):
    if start >= len(mask):
        return len(mask)
    rel = np.flatnonzero(~mask[start:])
    if len(rel) == 0:
        return len(mask)
    return start + int(rel[0])


def load_macro_times(events_file):
    if not events_file.exists():
        return []
    with events_file.open("r", encoding="utf-8") as f:
        events = json.load(f)
    return [pd.Timestamp(ev["release_time_jst"], tz="Asia/Tokyo") for ev in events]


def load_tick_data(tick_file, start=None, end=None, max_rows=None):
    print(f"Loading tick data: {tick_file}")
    usecols = ["<DATE>", "<TIME>", "<BID>", "<ASK>"]
    read_kwargs = {"sep": "\t", "usecols": usecols}
    if max_rows:
        read_kwargs["nrows"] = int(max_rows)
    df = pd.read_csv(tick_file, **read_kwargs)
    df["Datetime"] = pd.to_datetime(df["<DATE>"] + " " + df["<TIME>"])
    df.set_index("Datetime", inplace=True)
    df.rename(columns={"<BID>": "Bid", "<ASK>": "Ask"}, inplace=True)
    df["Bid"] = pd.to_numeric(df["Bid"], errors="coerce").ffill().bfill()
    df["Ask"] = pd.to_numeric(df["Ask"], errors="coerce").ffill().bfill()
    df = df.sort_index()
    df.index = df.index.tz_localize("Europe/Athens").tz_convert("Asia/Tokyo")
    if start:
        df = df[df.index >= pd.Timestamp(start, tz="Asia/Tokyo")]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz="Asia/Tokyo")]
    print(f"Loaded {len(df):,} ticks from {df.index.min()} to {df.index.max()}")
    return df


def analyze_trades(trades):
    if trades.empty:
        return {
            "Trades": 0,
            "PnL_USD": 0.0,
            "WinRate": 0.0,
            "PF": 0.0,
            "MDD": 0.0,
            "RF": 0.0,
            "AvgTrade": 0.0,
            "Equity": pd.Series(dtype=float),
        }

    pnl = trades["PnL_USD"].astype(float)
    total_pnl = float(pnl.sum())
    wins = int((pnl > 0).sum())
    gross_p = float(pnl[pnl > 0].sum())
    gross_l = float(abs(pnl[pnl <= 0].sum()))
    pf = gross_p / gross_l if gross_l > 0 else float("inf")
    wr = wins / len(trades) * 100

    exit_times = pd.to_datetime(trades["Exit_Time"])
    equity = pd.Series(pnl.cumsum().to_numpy(), index=exit_times)
    peaks = np.maximum.accumulate(equity.to_numpy())
    drawdowns = peaks - equity.to_numpy()
    mdd = float(drawdowns.max()) if len(drawdowns) else 0.0
    rf = total_pnl / mdd if mdd > 0 else 0.0

    return {
        "Trades": int(len(trades)),
        "PnL_USD": total_pnl,
        "WinRate": float(wr),
        "PF": float(pf),
        "MDD": mdd,
        "RF": float(rf),
        "AvgTrade": float(total_pnl / len(trades)),
        "Equity": equity,
    }


def simulate_s14_live_aligned(
    df,
    *,
    W_pips=40.0,
    b_trigger_ratio=0.5,
    commission_pips=0.6,
    slippage_pips=0.2,
    symbol="GBPUSDm",
    lot_multiplier=0.01,
    max_bet_units=8,
    initial_sequence=(2, 2, 2),
    weekend_filter=True,
    weekend_stop_hour=20,
    monday_start_hour=7,
    macro_times=None,
    avoidance_hours=2.0,
    max_spread_pips=0.8,
):
    macro_times = macro_times or []
    pip_val = 0.01 if "JPY" in symbol else 0.0001
    W = W_pips * pip_val
    commission = commission_pips * pip_val
    slippage = slippage_pips * pip_val
    max_spread = None if max_spread_pips is None else max_spread_pips * pip_val

    bids = df["Bid"].to_numpy()
    asks = df["Ask"].to_numpy()
    spreads = asks - bids
    times = df.index
    N = len(df)
    if N < 2:
        return pd.DataFrame()

    weekend_mask = np.array(
        [is_weekend_jst(pd.Timestamp(t), weekend_stop_hour, monday_start_hour) for t in times],
        dtype=bool,
    )
    spread_ok_mask = np.ones(N, dtype=bool) if max_spread is None else spreads <= max_spread

    trades = []
    mc_manager = MonteCarloManager(initial_sequence=list(initial_sequence))

    def calculate_lot(bet_units):
        capped = min(bet_units, max_bet_units) if max_bet_units else bet_units
        raw_lot = capped * lot_multiplier
        return max(0.01, round(raw_lot, 2)), capped

    def can_open(idx):
        t = pd.Timestamp(times[idx])
        if weekend_filter and weekend_mask[idx]:
            return False
        if is_in_news_window(t, macro_times, avoidance_hours):
            return False
        if not spread_ok_mask[idx]:
            return False
        return True

    def next_recheck_idx(idx):
        if idx >= N:
            return N
        t = pd.Timestamp(times[idx])
        jumps = []
        if weekend_filter and weekend_mask[idx]:
            jumps.append(find_first_false(weekend_mask, idx))
        if is_in_news_window(t, macro_times, avoidance_hours):
            jumps.append(get_next_tick_outside_news(times, idx, macro_times, avoidance_hours))
        if not spread_ok_mask[idx]:
            jumps.append(find_first_true(spread_ok_mask, idx))
        jumps = [j for j in jumps if j > idx]
        return max(jumps) if jumps else idx + 1

    i = 0
    pos_A = None
    pos_B = None
    next_direction_A = "LONG"
    waiting_B = True
    S = None

    while i < N - 1:
        if weekend_filter and weekend_mask[i]:
            if pos_A:
                exit_px = bids[i] if pos_A["direction"] == "LONG" else asks[i]
                trades.append(close_trade(pos_A, exit_px, times[i], "WEEKEND", symbol, commission, slippage))
                pos_A = None
            if pos_B:
                exit_px = bids[i] if pos_B["direction"] == "LONG" else asks[i]
                trades.append(close_trade(pos_B, exit_px, times[i], "WEEKEND", symbol, commission, slippage))
                pos_B = None
            next_direction_A = "LONG"
            waiting_B = True
            S = None
            i = max(i + 1, find_first_false(weekend_mask, i))
            continue

        if pos_A is None and next_direction_A is not None:
            if can_open(i):
                bet_raw = mc_manager.mc_A.get_bet_units()
                lot_A, bet_A = calculate_lot(bet_raw)
                if next_direction_A == "LONG":
                    entry = asks[i]
                    pos_A = {
                        "direction": "LONG",
                        "entry_price": entry,
                        "entry_time": times[i],
                        "tp": entry + W,
                        "sl": entry - W,
                        "lot": lot_A,
                        "bet_units": bet_A,
                    }
                else:
                    entry = bids[i]
                    pos_A = {
                        "direction": "SHORT",
                        "entry_price": entry,
                        "entry_time": times[i],
                        "tp": entry - W,
                        "sl": entry + W,
                        "lot": lot_A,
                        "bet_units": bet_A,
                    }
                next_direction_A = None
                if S is None:
                    S = entry
            else:
                i = next_recheck_idx(i)
                continue

        if waiting_B and pos_B is None and S is None:
            S = asks[i]

        t_weekend = N - i
        if weekend_filter:
            weekend_idx = find_first_true(weekend_mask, i)
            t_weekend = weekend_idx - i

        t_A = N - i
        reason_A = None
        if pos_A:
            if pos_A["direction"] == "LONG":
                tt_tp = find_first_ge(bids, pos_A["tp"], i) - i
                tt_sl = find_first_le(bids, pos_A["sl"], i) - i
            else:
                tt_tp = find_first_le(asks, pos_A["tp"], i) - i
                tt_sl = find_first_ge(asks, pos_A["sl"], i) - i
            if tt_tp < tt_sl:
                t_A = tt_tp
                reason_A = "TP"
            elif tt_sl < tt_tp:
                t_A = tt_sl
                reason_A = "SL"

        t_B = N - i
        reason_B = None
        if waiting_B and pos_B is None and S is not None:
            tt_up = find_first_ge(asks, S + W * b_trigger_ratio, i) - i
            tt_dn = find_first_le(bids, S - W * b_trigger_ratio, i) - i
            if tt_up < tt_dn:
                t_B = tt_up
                reason_B = "ACTIVATE_UP"
            elif tt_dn < tt_up:
                t_B = tt_dn
                reason_B = "ACTIVATE_DN"
        elif pos_B:
            if pos_B["direction"] == "LONG":
                tt_tp = find_first_ge(bids, pos_B["tp"], i) - i
                tt_sl = find_first_le(bids, pos_B["sl"], i) - i
            else:
                tt_tp = find_first_le(asks, pos_B["tp"], i) - i
                tt_sl = find_first_ge(asks, pos_B["sl"], i) - i
            if tt_tp < tt_sl:
                t_B = tt_tp
                reason_B = "TP"
            elif tt_sl < tt_tp:
                t_B = tt_sl
                reason_B = "SL"

        min_t = min(t_A, t_B, t_weekend)
        if min_t >= N - i:
            break
        if min_t <= 0:
            min_t = 1
        j = i + min_t

        if min_t == t_weekend:
            i = j
            continue

        close_A = None
        close_B = None
        outcome_A = None
        outcome_B = None

        if min_t == t_A and pos_A:
            close_A = pos_A
            outcome_A = reason_A

        if min_t == t_B:
            if waiting_B and pos_B is None:
                if not can_open(j):
                    i = next_recheck_idx(j)
                    continue
                bet_raw = mc_manager.mc_B.get_bet_units()
                lot_B, bet_B = calculate_lot(bet_raw)
                if reason_B == "ACTIVATE_UP":
                    entry = bids[j]
                    pos_B = {
                        "direction": "SHORT",
                        "entry_price": entry,
                        "entry_time": times[j],
                        "tp": entry - W,
                        "sl": entry + W,
                        "lot": lot_B,
                        "bet_units": bet_B,
                    }
                else:
                    entry = asks[j]
                    pos_B = {
                        "direction": "LONG",
                        "entry_price": entry,
                        "entry_time": times[j],
                        "tp": entry + W,
                        "sl": entry - W,
                        "lot": lot_B,
                        "bet_units": bet_B,
                    }
                waiting_B = False
            elif pos_B:
                close_B = pos_B
                outcome_B = reason_B

        mc_manager.update_mc(
            outcome_A,
            outcome_B,
            close_A["bet_units"] if close_A else 0,
            close_B["bet_units"] if close_B else 0,
        )

        if close_A:
            exit_px = bids[j] if close_A["direction"] == "LONG" else asks[j]
            if outcome_A == "SL":
                exit_px = exit_px - slippage if close_A["direction"] == "LONG" else exit_px + slippage
            trades.append(close_trade(close_A, exit_px, times[j], outcome_A, symbol, commission, slippage))
            pos_A = None
            next_direction_A = (
                "SHORT" if outcome_A == "TP" and close_A["direction"] == "LONG"
                else "LONG" if outcome_A == "TP" and close_A["direction"] == "SHORT"
                else close_A["direction"]
            )

        if close_B:
            exit_px = bids[j] if close_B["direction"] == "LONG" else asks[j]
            if outcome_B == "SL":
                exit_px = exit_px - slippage if close_B["direction"] == "LONG" else exit_px + slippage
            trades.append(close_trade(close_B, exit_px, times[j], outcome_B, symbol, commission, slippage))
            pos_B = None
            waiting_B = True
            S = exit_px

        i = j

    return pd.DataFrame(trades)


def make_grid(preset):
    if preset == "smoke":
        return {
            "W_pips": [40.0, 45.0],
            "b_trigger_ratio": [0.5, 0.6],
            "max_bet_units": [8],
            "weekend_stop_hour": [20],
            "avoidance_hours": [2.0],
            "max_spread_pips": [1.2],
            "lot_multiplier": [0.01],
        }
    if preset == "focused":
        return {
            "W_pips": [35.0, 40.0, 45.0, 50.0, 55.0],
            "b_trigger_ratio": [0.5, 0.6, 0.75],
            "max_bet_units": [6, 8, 10],
            "weekend_stop_hour": [18, 20, 22],
            "avoidance_hours": [0.0, 1.0, 2.0],
            "max_spread_pips": [0.8, 1.0, 1.2],
            "lot_multiplier": [0.01],
        }
    if preset == "full":
        return {
            "W_pips": [30.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0],
            "b_trigger_ratio": [0.4, 0.5, 0.6, 0.75, 1.0],
            "max_bet_units": [6, 8, 10, 12],
            "weekend_stop_hour": [18, 20, 22],
            "avoidance_hours": [0.0, 1.0, 2.0, 3.0],
            "max_spread_pips": [0.8, 1.0, 1.2, 2.0],
            "lot_multiplier": [0.01],
        }
    raise ValueError(f"Unknown preset: {preset}")


def plot_top_equity(equity_runs, output_path):
    plt.figure(figsize=(12, 7))
    plotted = False
    for label, equity in equity_runs:
        if len(equity) == 0:
            continue
        plt.plot(equity.index, equity.values, label=label)
        plotted = True
    plt.title("s14 GBPUSDm Live-Aligned Grid Search - Top Equity Curves")
    plt.xlabel("Exit time")
    plt.ylabel("PnL USD")
    plt.grid(True, linestyle="--", alpha=0.5)
    if plotted:
        plt.legend(fontsize=8)
    plt.gcf().autofmt_xdate()
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def main():
    parser = argparse.ArgumentParser(description="s14 live-aligned GBPUSDm grid search")
    parser.add_argument("--preset", choices=["smoke", "focused", "full"], default="focused")
    parser.add_argument("--start", default=None, help="JST start date, e.g. 2026-01-01")
    parser.add_argument("--end", default=None, help="JST end date, e.g. 2026-06-03")
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--save-top-trades", type=int, default=3)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tick_df = load_tick_data(DATA_DIR / "GBPUSDm_tick.csv", args.start, args.end, args.max_rows)
    macro_times = load_macro_times(DATA_DIR / "macro_events_2026.json")
    print(f"Loaded {len(macro_times)} macro events.")

    grid = make_grid(args.preset)
    keys = list(grid.keys())
    combos = [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]
    print(f"Preset={args.preset}, combinations={len(combos)}")

    results = []
    run_cache = []
    for idx, params in enumerate(combos, 1):
        print(f"[{idx:>4}/{len(combos)}] {params}")
        trades = simulate_s14_live_aligned(
            tick_df,
            symbol="GBPUSDm",
            macro_times=macro_times,
            weekend_filter=True,
            monday_start_hour=7,
            commission_pips=0.6,
            slippage_pips=0.2,
            **params,
        )
        metrics = analyze_trades(trades)
        row = {**params, **{k: v for k, v in metrics.items() if k != "Equity"}}
        results.append(row)
        run_cache.append((row, trades, metrics["Equity"]))
        print(
            f"    trades={row['Trades']} pnl={row['PnL_USD']:.2f} "
            f"pf={row['PF']:.2f} mdd={row['MDD']:.2f} rf={row['RF']:.2f}"
        )

    df_res = pd.DataFrame(results)
    df_res.sort_values(["RF", "PnL_USD", "PF"], ascending=[False, False, False], inplace=True)

    csv_path = OUTPUT_DIR / f"grid_results_{args.preset}.csv"
    json_path = OUTPUT_DIR / f"grid_results_{args.preset}.json"
    df_res.to_csv(csv_path, index=False)
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(df_res.to_dict(orient="records"), f, indent=2)

    top_rows = df_res.head(args.top_n)
    top_keys = [
        (
            row.W_pips,
            row.b_trigger_ratio,
            row.max_bet_units,
            row.weekend_stop_hour,
            row.avoidance_hours,
            row.max_spread_pips,
            row.lot_multiplier,
        )
        for row in top_rows.itertuples(index=False)
    ]

    equity_runs = []
    saved_trades = 0
    for row, trades, equity in run_cache:
        key = (
            row["W_pips"],
            row["b_trigger_ratio"],
            row["max_bet_units"],
            row["weekend_stop_hour"],
            row["avoidance_hours"],
            row["max_spread_pips"],
            row["lot_multiplier"],
        )
        if key not in top_keys:
            continue
        label = (
            f"W={row['W_pips']}, B={row['b_trigger_ratio']}, cap={row['max_bet_units']}, "
            f"stop={row['weekend_stop_hour']}, news={row['avoidance_hours']}h, RF={row['RF']:.2f}"
        )
        equity_runs.append((label, equity))
        if saved_trades < args.save_top_trades:
            trades_path = OUTPUT_DIR / f"top{saved_trades + 1}_trades_{args.preset}.csv"
            trades.to_csv(trades_path, index=False)
            saved_trades += 1

    plot_path = OUTPUT_DIR / f"top_equity_{args.preset}.png"
    plot_top_equity(equity_runs, plot_path)

    print("\nTop results:")
    print(top_rows.to_string(index=False))
    print(f"\nSaved results: {csv_path}")
    print(f"Saved JSON:    {json_path}")
    print(f"Saved plot:    {plot_path}")


if __name__ == "__main__":
    main()

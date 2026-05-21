"""
Change point detector using CUSUM (Cumulative Sum Control Chart).
Detects sustained shifts in step duration from eBPF trace CSV output.

Reads the first column of a CSV file (per-step duration in ms),
uses a baseline period to estimate the normal mean and standard deviation, then applies
CUSUM to detect persistent mean shifts.

Usage:
    python changepoint_detector.py <csv_file>
    python changepoint_detector.py <csv_file> --baseline 100
"""

import argparse
import csv
import sys


def read_durations(filepath):
    """Read first column from CSV as list of floats."""
    durations = []
    with open(filepath) as f:
        reader = csv.reader(f)
        for row in reader:
            if row:
                durations.append(float(row[0]))
    return durations


def _confirm(data, trigger_idx, threshold, confirm_count, n, direction):
    """
    Starting from trigger_idx, scan forward to confirm that the signal has
    moved past the threshold. Confirmation uses a short forward window instead
    of requiring consecutive threshold crossings, which is more robust for
    high-variance slowdowns where the mean has shifted but individual points
    can still oscillate around the threshold.

    Returns (True, first_index_of_confirmed_window) or (False, None).
    """
    confirm_count = max(confirm_count, 1)
    window_size = max(1, confirm_count * 2)
    min_ratio = 0.2

    scan_end = min(trigger_idx + confirm_count * 5, n)
    for j in range(trigger_idx, scan_end):
        window = data[j:min(j + window_size, n)]
        if len(window) < confirm_count:
            break

        if direction == "above":
            crossing_count = sum(1 for x in window if x > threshold)
            window_mean = sum(window) / len(window)
            if crossing_count / len(window) >= min_ratio or window_mean > threshold:
                return True, j
        elif direction == "below":
            crossing_count = sum(1 for x in window if x < threshold)
            window_mean = sum(window) / len(window)
            if crossing_count / len(window) >= min_ratio or window_mean < threshold:
                return True, j

    return False, None


def detect_changepoints(data, baseline_size=100, k_factor=1.5, h_factor=10.0,
                        min_pct=12.0, confirm_count=10):
    """
    CUSUM change point detection with confirmation.

    Args:
        data: list of float values (step durations)
        baseline_size: number of initial points for baseline estimation
        k_factor: sensitivity; detects shifts of k_factor * standard deviation (default 1.5)
        h_factor: threshold; alarm when cumsum reaches h_factor * standard deviation (default 20.0)
        min_pct: minimum percentage change to report (default 12.0%)
        confirm_count: number of consecutive points that must deviate to confirm (default 1)

    Returns:
        list of dicts: {step, old_mean, new_mean, direction, pct}
    """
    n = len(data)
    if n < baseline_size + 5:
        return []

    changepoints = []
    seg_start = 0

    while seg_start < n:
        seg_end = min(seg_start + baseline_size, n)
        if seg_end - seg_start < 5:
            break

        # Estimate baseline mean and standard deviation.
        baseline = data[seg_start:seg_end]
        mu = sum(baseline) / len(baseline)
        var = sum((x - mu) ** 2 for x in baseline) / len(baseline)
        sigma = var ** 0.5

        if sigma < 1e-9:
            # Constant data; use a small fraction of mean as sigma.
            sigma = abs(mu) * 0.01 if mu != 0 else 1.0

        # Use the larger of statistical sigma and a fraction of mean as effective sigma,
        # so that tiny absolute sigma on stable data does not cause false alarms.
        sigma_eff = max(sigma, abs(mu) * 0.02)

        k = k_factor * sigma_eff
        h = h_factor * sigma_eff

        # CUSUM scan
        s_pos = 0.0
        s_neg = 0.0
        cp_found = None

        for i in range(seg_end, n):
            x = data[i]
            s_pos = max(0.0, s_pos + (x - mu - k))
            s_neg = max(0.0, s_neg + (mu - k - x))

            if s_pos > h:
                # Confirm: check that the next confirm_count points
                # are consistently above baseline + min_pct threshold
                threshold = mu * (1 + min_pct / 100.0)
                confirmed, cp_step = _confirm(data, i, threshold, confirm_count, n, "above")
                if confirmed:
                    lookahead = data[cp_step:min(cp_step + baseline_size, n)]
                    new_mu = sum(lookahead) / len(lookahead)
                    pct = (new_mu - mu) / mu * 100 if mu != 0 else 0
                    changepoints.append({
                        "step": cp_step,
                        "old_mean": mu,
                        "new_mean": new_mu,
                        "direction": "up",
                        "pct": pct,
                    })
                    seg_start = cp_step
                    cp_found = True
                    break
                else:
                    s_pos = 0.0
                    continue

            if s_neg > h:
                threshold = mu * (1 - min_pct / 100.0)
                confirmed, cp_step = _confirm(data, i, threshold, confirm_count, n, "below")
                if confirmed:
                    lookahead = data[cp_step:min(cp_step + baseline_size, n)]
                    new_mu = sum(lookahead) / len(lookahead)
                    pct = (new_mu - mu) / mu * 100 if mu != 0 else 0
                    changepoints.append({
                        "step": cp_step,
                        "old_mean": mu,
                        "new_mean": new_mu,
                        "direction": "down",
                        "pct": pct,
                    })
                    seg_start = cp_step
                    cp_found = True
                    break
                else:
                    s_neg = 0.0
                    continue

        if not cp_found:
            break

    # Filter out tail artifacts; changepoints too close to the end
    # with insufficient lookahead are likely truncation, not real shifts
    changepoints = [cp for cp in changepoints
                    if cp["step"] + baseline_size <= n]

    return changepoints


def main():
    parser = argparse.ArgumentParser(description="CUSUM change point detector for step durations")
    parser.add_argument("csv_file", help="CSV file (first column = step duration in ms)")
    parser.add_argument("--baseline", type=int, default=100,
                        help="Baseline window size (default: 100)")
    args = parser.parse_args()

    data = read_durations(args.csv_file)
    if not data:
        print("No data.", file=sys.stderr)
        sys.exit(1)

    mu0 = sum(data[:args.baseline]) / min(len(data), args.baseline)
    sigma0 = (sum((x - mu0) ** 2 for x in data[:args.baseline]) / min(len(data), args.baseline)) ** 0.5
    print(f"Data: {len(data)} steps, baseline mean={mu0:.2f} ms, stddev={sigma0:.2f} ms")

    cps = detect_changepoints(data, baseline_size=args.baseline)

    if not cps:
        print("No change points detected.")
    else:
        for cp in cps:
            trend = "UP" if cp["direction"] == "up" else "DOWN"
            print(f"Change point at step {cp['step']}: "
                  f"mean {cp['old_mean']:.2f} -> {cp['new_mean']:.2f} ms "
                  f"({trend} {cp['pct']:+.1f}%)")


if __name__ == "__main__":
    main()

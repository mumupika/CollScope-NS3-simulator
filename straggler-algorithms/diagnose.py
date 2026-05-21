"""
Unified diagnostic: detect changepoints in step time, then attribute each
changepoint to either node computation slowdown or other causes (e.g. network).

Pipeline:
  1. Read output.csv from trace_dir -> detect changepoints (CUSUM)
  2. For each changepoint, map step range to raw line range via timestamps
  3. Run the inline node drift detector on that line range only
  4. If a node is slow in that range -> attribute to computation slowdown
  5. Otherwise -> attribute to other cause (network congestion, etc.)
  6. Generate annotated plot

Usage:
    python3 diagnose.py <trace_dir> -o <output.png>
    python3 diagnose.py <trace_dir> -o <output.png> --title "Experiment"
"""

import argparse
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from changepoint_detector import read_durations, detect_changepoints
from step_detector import parse_trace, detect_steps as detect_steps


# ===== File I/O =====
def find_trace_files(trace_dir):
    files = []
    for name in os.listdir(trace_dir):
        path = os.path.join(trace_dir, name)
        if os.path.isfile(path):
            try:
                pid = int(name)
                files.append((pid, path))
            except ValueError:
                continue
    files.sort(key=lambda x: x[0])
    return files


def read_all_timestamps(filepath):
    timestamps = []
    with open(filepath) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 4:
                timestamps.append(int(parts[0]))
    return timestamps


def read_entry_timestamps(filepath):
    """Read only entry events (enter_exit == 0), return list of timestamps."""
    timestamps = []
    with open(filepath) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 4 and int(parts[2]) == 0:
                timestamps.append(int(parts[0]))
    return timestamps


# ===== Step-to-line mapping =====
def build_step_to_line_map(entry_ts, all_ts):
    """
    Map entry event index to raw line index.
    Returns a list where result[i] = raw line index of the i-th entry event.
    """
    line_map = []
    j = 0
    for ts in entry_ts:
        while j < len(all_ts) and all_ts[j] != ts:
            j += 1
        line_map.append(j)
        j += 1
    return line_map


def step_to_raw_line(step_idx, step_timestamps, all_ts):
    """
    Given a step index and the list of step start timestamps (from step_detector),
    find the corresponding raw line index by matching the timestamp.
    """
    if step_idx >= len(step_timestamps):
        return len(all_ts) - 1
    target_ts = step_timestamps[step_idx]
    # Binary-ish search: timestamps are monotonic
    for i, ts in enumerate(all_ts):
        if ts >= target_ts:
            return i
    return len(all_ts) - 1


# ===== Node slowdown detection =====
def detect_slow_in_range(base_ts, other_ts, start_line, end_line,
                         n=100, p_thresh=45, m_us=2000):
    """
    Check if other node is slow relative to base within [start_line, end_line).
    Returns True if at any point within the range, P% of a window of N lines
    have drift > M us.
    """
    seg_len = end_line - start_line
    if seg_len < n:
        # Range too small for a full window; check the overall ratio.
        count = 0
        for i in range(start_line, min(end_line, len(base_ts), len(other_ts))):
            drift = (other_ts[i] - base_ts[i]) / 1e3
            if drift > m_us:
                count += 1
        return count / max(seg_len, 1) * 100 >= p_thresh

    over = []
    for i in range(start_line, min(end_line, len(base_ts), len(other_ts))):
        drift = (other_ts[i] - base_ts[i]) / 1e3
        over.append(1 if drift > m_us else 0)

    threshold = n * p_thresh / 100.0
    window_count = sum(over[:n])

    for i in range(n, len(over)):
        if window_count >= threshold:
            return True
        window_count += over[i] - over[i - n]

    return window_count >= threshold


# ===== Main =====
def main():
    parser = argparse.ArgumentParser(description="Unified changepoint diagnosis")
    parser.add_argument("trace_dir")
    parser.add_argument("-o", "--output", required=True, help="Output plot path")
    parser.add_argument("--title", default="", help="Plot title")
    parser.add_argument("--csv", default=None,
                        help="Path to output.csv (default: <trace_dir>/output.csv)")
    args = parser.parse_args()

    csv_path = args.csv or os.path.join(args.trace_dir, "output.csv")
    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    # 1. Detect changepoints from step durations
    data = read_durations(csv_path)
    cps = detect_changepoints(data)

    # 2. Load trace files
    trace_files = find_trace_files(args.trace_dir)
    if not trace_files:
        print("No trace files found.", file=sys.stderr)
        sys.exit(1)

    pids = [pid for pid, _ in trace_files]
    base_pid = pids[0]

    # Read timestamps
    all_ts_full = {}
    for pid, path in trace_files:
        all_ts_full[pid] = read_all_timestamps(path)

    # Get step start timestamps from step_detector on baseline PID
    base_path = [p for pid, p in trace_files if pid == base_pid][0]
    base_events = parse_trace(base_path)
    base_steps = detect_steps(base_events)
    step_timestamps = [s["start_ns"] for s in base_steps]

    base_all_ts = all_ts_full[base_pid]
    min_raw_len = min(len(ts) for ts in all_ts_full.values())

    # 3. For each changepoint, check node slowdown
    print(f"Loaded {len(pids)} PIDs, baseline PID {base_pid}")
    print(f"Detected {len(cps)} changepoint(s) in {len(data)} steps\n")

    cp_annotations = []  # for plotting

    for cp in cps:
        step = cp["step"]
        direction = cp["direction"]
        pct = cp["pct"]

        # Map step to raw line via step timestamps
        line_start = step_to_raw_line(step, step_timestamps, base_all_ts)

        # Find next changepoint or end of data
        cp_idx = cps.index(cp)
        if cp_idx + 1 < len(cps):
            next_step = cps[cp_idx + 1]["step"]
            line_end = step_to_raw_line(next_step, step_timestamps, base_all_ts)
        else:
            line_end = min_raw_len

        line_end = min(line_end, min_raw_len)

        trend = "UP" if direction == "up" else "DOWN"
        print(f"Changepoint at step {step} ({trend} {pct:+.1f}%), "
              f"raw lines {line_start}-{line_end}:")

        # Only check for node slowdown on "up" changepoints (things got slower)
        slow_nodes = []
        if direction == "up":
            base_ts = all_ts_full[base_pid]
            for pid in pids[1:]:
                if detect_slow_in_range(base_ts, all_ts_full[pid],
                                        line_start, line_end):
                    node_id = pid - base_pid
                    slow_nodes.append((pid, node_id))

            # Also check if baseline itself is slow (all others ahead)
            # Skip for simplicity; baseline slow means all others have negative drift.

        if slow_nodes:
            nodes_str = ", ".join(f"PID {p} (node {n})" for p, n in slow_nodes)
            print(f"  -> Computation slowdown: {nodes_str}")
            cp_annotations.append({
                **cp, "cause": "computation",
                "slow_nodes": slow_nodes
            })
        else:
            if direction == "up":
                print(f"  -> Network congestion or other cause (no node drift)")
            else:
                print(f"  -> Recovery")
            cp_annotations.append({**cp, "cause": "other", "slow_nodes": []})

    print()

    # 4. Plot
    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(range(len(data)), data, linewidth=0.6, color="#4A90D9")

    y_top = max(data) * 1.02

    for ann in cp_annotations:
        step = ann["step"]
        slow = ann["slow_nodes"]
        cause = ann["cause"]

        if cause == "computation":
            color = "#E74C3C"  # red
        elif ann["direction"] == "down":
            color = "#27AE60"  # green for recovery
        else:
            color = "#F39C12"  # orange for network/other

        ax.axvline(x=step, color=color, linestyle="--", linewidth=1, alpha=0.8)

        # Build label
        parts = [f'{ann["pct"]:+.1f}%']
        if slow:
            for pid, node_id in slow:
                parts.append(f"PID {pid} (node {node_id})")
            label = "\n".join(parts)
        else:
            if ann["direction"] == "up":
                parts.append("network/other")
            else:
                parts.append("recovery")
            label = "\n".join(parts)

        ax.annotate(label, xy=(step, y_top), fontsize=7, color=color,
                    ha="left", va="top",
                    xytext=(5, -5), textcoords="offset points")

    ax.set_xlabel("Step")
    ax.set_ylabel("Duration (ms)")
    if args.title:
        ax.set_title(args.title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(args.output, dpi=150)
    plt.close(fig)

    print(f"Plot saved to {args.output}")


if __name__ == "__main__":
    main()

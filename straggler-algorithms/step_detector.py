"""
Detect per-step collective communication patterns from eBPF trace data.
    batch mode - process all trace files in a directory (one per PID),
    output aligned per-step durations as CSV.

Input format per file (space-separated, one per line):
    timestamp_ns  type(AR/AG/RS)  enter_exit(0/1)  latency_us

File names should be PID numbers (e.g. 12345, 12346).

Usage:
    python step_detector.py <trace_dir> -o <output.csv>
    python step_detector.py <trace_dir> -o <output.csv> --debug
"""

import argparse
import csv
import os
import sys


# ===== Parse input =====
def parse_trace(filepath):
    events = []
    with open(filepath) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 4:
                continue
            ts_ns = int(parts[0])
            comm_type = parts[1]
            enter_exit = int(parts[2])
            if enter_exit == 0:
                events.append((ts_ns, comm_type))
    return events


# ===== Incremental KMP period detection =====
def _detect_pattern(type_seq, start, warmup, max_scan):
    n = len(type_seq)
    scan_end = min(start + max_scan, n)
    if start >= scan_end:
        return None, None

    pi = []
    for idx in range(start, scan_end):
        pos = idx - start
        if pos == 0:
            pi.append(0)
        else:
            j = pi[pos - 1]
            while j > 0 and type_seq[idx] != type_seq[start + j]:
                j = pi[j - 1]
            if type_seq[idx] == type_seq[start + j]:
                j += 1
            pi.append(j)

        cur_len = pos + 1
        if cur_len <= warmup:
            continue

        period = cur_len - pi[pos]
        if cur_len >= 2 * period and period >= 2:
            pat = type_seq[start:start + period]
            if len(set(pat)) < 2:
                continue
            return pat, start

    return None, None


# ===== State machine: DETECT <-> MATCH =====
def detect_steps(events, warmup_limit=None, window_size=None):
    if not events:
        return []

    type_seq = tuple(e[1] for e in events)
    n = len(type_seq)

    if warmup_limit is None:
        warmup_limit = max(3, min(n // 5, 50))
    if window_size is None:
        window_size = max(6, min(n // 3, 60))

    max_scan = warmup_limit + window_size * 3

    steps = []
    step_idx = 0
    i = 0

    while i < n:
        pat, align_start = _detect_pattern(type_seq, i, warmup_limit, max_scan)

        if pat is None:
            i += warmup_limit
            continue

        pat_len = len(pat)
        i = align_start

        pat_pos = 0
        step_start_i = i
        steps_before = len(steps)

        while i < n:
            if type_seq[i] == pat[pat_pos]:
                pat_pos += 1
                i += 1
                if pat_pos == pat_len:
                    start_ns = events[step_start_i][0]
                    if i < n:
                        end_ns = events[i][0]
                    else:
                        end_ns = events[i - 1][0]
                    dur_ms = (end_ns - start_ns) / 1e6
                    steps.append({
                        "step": step_idx,
                        "pattern": list(pat),
                        "start_ns": start_ns,
                        "end_ns": end_ns,
                        "duration_ms": dur_ms,
                    })
                    step_idx += 1
                    pat_pos = 0
                    step_start_i = i
            else:
                matched = len(steps) - steps_before
                if matched == 0:
                    i = align_start + pat_len
                else:
                    i = step_start_i
                break

    return steps


# ===== Debug output =====
def print_steps(steps, pid):
    if not steps:
        print(f"  PID {pid}: no steps detected.")
        return

    print(f"  PID {pid}: {len(steps)} steps")
    current_pat = None
    for s in steps:
        pat = " ".join(s["pattern"])
        if pat != current_pat:
            if current_pat is not None:
                print(f"    --- pattern changed ---")
            current_pat = pat
        print(f"    step {s['step']:>4}  {s['duration_ms']:>10.2f} ms  {pat}")

    durations = [s["duration_ms"] for s in steps]
    avg = sum(durations) / len(durations)
    print(f"    avg: {avg:.2f} ms, min: {min(durations):.2f} ms, max: {max(durations):.2f} ms")


# ===== Batch processing =====
def find_trace_files(trace_dir):
    """Find files whose names are numeric (PIDs), return sorted by PID."""
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


def main():
    parser = argparse.ArgumentParser(description="Batch step detection from eBPF traces")
    parser.add_argument("trace_dir", help="Directory containing trace files (named by PID)")
    parser.add_argument("-o", "--output", required=True, help="Output CSV file path")
    parser.add_argument("--warmup", type=int, default=None)
    parser.add_argument("--window", type=int, default=None)
    parser.add_argument("--debug", action="store_true", help="Print per-PID step details")
    args = parser.parse_args()

    trace_files = find_trace_files(args.trace_dir)
    if not trace_files:
        print(f"No trace files found in {args.trace_dir}", file=sys.stderr)
        sys.exit(1)

    pids = [pid for pid, _ in trace_files]
    all_durations = []  # list of lists, one per PID

    for pid, path in trace_files:
        events = parse_trace(path)
        if args.debug:
            print(f"  PID {pid}: parsed {len(events)} entry events", file=sys.stderr)

        steps = detect_steps(events, warmup_limit=args.warmup, window_size=args.window)

        if args.debug:
            print_steps(steps, pid)

        durations = [s["duration_ms"] for s in steps]
        all_durations.append(durations)

    # Align to shortest
    min_steps = min(len(d) for d in all_durations) if all_durations else 0

    if args.debug and all_durations:
        lengths = [len(d) for d in all_durations]
        if max(lengths) != min(lengths):
            print(f"\n  Step counts differ: {dict(zip(pids, lengths))}", file=sys.stderr)
            print(f"  Truncating to {min_steps} steps", file=sys.stderr)

    # Write CSV
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", newline="") as f:
        writer = csv.writer(f)
        for i in range(min_steps):
            row = [f"{d[i]:.2f}" for d in all_durations]
            writer.writerow(row)

    if args.debug:
        print(f"\nWrote {min_steps} rows x {len(pids)} PIDs to {args.output}")


if __name__ == "__main__":
    main()

import argparse
import ctypes
import os
import subprocess
import time

from bcc import BPF


FUNC_SPECS = [
    ("ncclAllReduce", "allreduce", 0),
    ("ncclReduceScatter", "reducescatter", 1),
    ("ncclAllGather", "allgather", 2),
    ("ncclBroadcast", "broadcast", 3),
    ("ncclReduce", "reduce", 4),
]

FUNC_NAME = {func_id: symbol for symbol, _, func_id in FUNC_SPECS}
FUNC_SHORT = {
    0: "AR",
    1: "RS",
    2: "AG",
    3: "B",
    4: "R",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Hook NCCL collectives with eBPF/BCC.")
    parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Process name (or keyword) to match, e.g. 'main_mpi' or 'python3'.",
    )
    parser.add_argument(
        "--nproc",
        type=int,
        required=True,
        help="Expected number of local processes (e.g. 4 for 4 GPUs on this node).",
    )
    parser.add_argument(
        "--lib-path",
        type=str,
        default="",
        help="Optional libnccl path. If omitted, resolve it from /proc/<pid>/maps.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print hook events to stdout.",
    )
    return parser.parse_args()


def find_target_pids(name):
    """Find matching Rl-like processes with ps aux and return sorted PIDs."""
    result = subprocess.run(
        ["ps", "aux"],
        capture_output=True,
        text=True,
    )

    pids = []
    for line in result.stdout.strip().split("\n")[1:]:  # Skip the header.
        fields = line.split(None, 10)  # Split into at most 11 fields.
        if len(fields) < 11:
            continue

        pid = fields[1]
        stat = fields[7]
        command = fields[10]

        # Match Rl-style running multithreaded states, such as Rl, Rl+, or Rsl.
        if name in command and stat.startswith("R") and "l" in stat:
            pids.append(int(pid))

    pids.sort()
    return pids


def wait_for_processes(name, nproc):
    """Poll until exactly nproc matching processes are found."""
    print(f"Waiting for {nproc} processes matching '{name}' with STAT=Rl ...")
    start = time.time()

    while True:
        pids = find_target_pids(name)
        elapsed = time.time() - start

        if len(pids) == nproc:
            print(f"Found {nproc} processes (elapsed {elapsed:.1f}s): {pids}")
            return pids

        if len(pids) > nproc:
            raise RuntimeError(
                f"Expected {nproc} processes, but found {len(pids)}: {pids}\n"
                f"Too many matching processes. Use a more specific --name to narrow down."
            )

        print(
            f"  found {len(pids)}/{nproc} so far (elapsed {elapsed:.1f}s), "
            f"retrying in 1s ...",
            flush=True,
        )
        time.sleep(1)


def resolve_libnccl_path(pid, lib_path_arg):
    if lib_path_arg:
        return lib_path_arg

    maps_path = f"/proc/{pid}/maps"
    with open(maps_path, "r", encoding="utf-8") as f:
        for line in f:
            if "libnccl.so" not in line:
                continue

            parts = line.strip().split()
            candidate = parts[-1]
            if os.path.isabs(candidate):
                return candidate

    raise RuntimeError(f"Could not find libnccl.so in {maps_path}")


def build_bpf_program():
    return r"""
#include <linux/ptrace.h>
#include <linux/sched.h>

struct data_t {
    u32 pid;
    u32 tid;
    u64 ts;
    u64 duration_ns;
    int type;
    int func_id;
};

BPF_PERF_OUTPUT(events);
BPF_HASH(start_ts, u64, u64);
BPF_HASH(func_map, u64, int);

static int trace_entry(struct pt_regs *ctx, int func_id) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 tid = pid_tgid & 0xFFFFFFFF;
    u64 ts = bpf_ktime_get_ns();

    start_ts.update(&tid, &ts);
    func_map.update(&tid, &func_id);

    struct data_t data = {};
    data.pid = pid_tgid >> 32;
    data.tid = tid;
    data.ts = ts;
    data.type = 0;
    data.func_id = func_id;

    events.perf_submit(ctx, &data, sizeof(data));
    return 0;
}

static int trace_return(struct pt_regs *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 tid = pid_tgid & 0xFFFFFFFF;
    u64 *tsp = start_ts.lookup(&tid);
    int *func_idp = func_map.lookup(&tid);

    if (tsp != 0 && func_idp != 0) {
        struct data_t data = {};
        data.pid = pid_tgid >> 32;
        data.tid = tid;
        data.ts = bpf_ktime_get_ns();
        data.duration_ns = data.ts - *tsp;
        data.type = 1;
        data.func_id = *func_idp;

        events.perf_submit(ctx, &data, sizeof(data));
        start_ts.delete(&tid);
        func_map.delete(&tid);
    }

    return 0;
}

int trace_allreduce_entry(struct pt_regs *ctx) { return trace_entry(ctx, 0); }
int trace_reducescatter_entry(struct pt_regs *ctx) { return trace_entry(ctx, 1); }
int trace_allgather_entry(struct pt_regs *ctx) { return trace_entry(ctx, 2); }
int trace_broadcast_entry(struct pt_regs *ctx) { return trace_entry(ctx, 3); }
int trace_reduce_entry(struct pt_regs *ctx) { return trace_entry(ctx, 4); }

int trace_allreduce_return(struct pt_regs *ctx) { return trace_return(ctx); }
int trace_reducescatter_return(struct pt_regs *ctx) { return trace_return(ctx); }
int trace_allgather_return(struct pt_regs *ctx) { return trace_return(ctx); }
int trace_broadcast_return(struct pt_regs *ctx) { return trace_return(ctx); }
int trace_reduce_return(struct pt_regs *ctx) { return trace_return(ctx); }
"""


class Data(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint32),
        ("tid", ctypes.c_uint32),
        ("ts", ctypes.c_uint64),
        ("duration_ns", ctypes.c_uint64),
        ("type", ctypes.c_int),
        ("func_id", ctypes.c_int),
    ]


def make_event_handler(output_files, debug):
    """Create an event handler that writes one output file per PID."""

    def handle_event(cpu, data, size):
        event = ctypes.cast(data, ctypes.POINTER(Data)).contents
        pid = event.pid
        func_short = FUNC_SHORT.get(event.func_id, "UNK")

        # Create the output file lazily when a new PID appears.
        if pid not in output_files:
            output_files[pid] = open(f"data/{pid}", "w", encoding="utf-8")
            print(f"New PID detected: {pid}, created data/{pid}")

        f = output_files[pid]

        if event.type == 0:
            f.write(f"{event.ts} {func_short} 0 0.0\n")
        else:
            latency_us = event.duration_ns / 1000.0
            f.write(f"{event.ts} {func_short} 1 {latency_us:.6f}\n")
        f.flush()

        if debug:
            ts_str = time.strftime("%H:%M:%S", time.localtime())
            func_name = FUNC_NAME.get(event.func_id, f"unknown({event.func_id})")
            if event.type == 0:
                print(
                    f"[{ts_str}] [ENTER] | PID: {pid:<7} | TID: {event.tid:<7} | FUNC: {func_name}"
                )
            else:
                latency_us = event.duration_ns / 1000.0
                print(
                    f"[{ts_str}] [EXIT ] | PID: {pid:<7} | TID: {event.tid:<7} | "
                    f"FUNC: {func_name:<18} | Latency: {latency_us:>10.2f} us"
                )

    return handle_event


def attach_hooks(bpf, lib_path):
    attached = []

    for symbol, prefix, _ in FUNC_SPECS:
        entry_fn = f"trace_{prefix}_entry"
        return_fn = f"trace_{prefix}_return"

        try:
            bpf.attach_uprobe(name=lib_path, sym=symbol, fn_name=entry_fn)
            bpf.attach_uretprobe(name=lib_path, sym=symbol, fn_name=return_fn)
            attached.append(symbol)
        except Exception as exc:
            print(f"skip {symbol}: {exc}")

    if not attached:
        raise RuntimeError("No NCCL symbols were attached successfully.")

    return attached


def main():
    args = parse_args()

    # Discover target processes.
    pids = wait_for_processes(args.name, args.nproc)

    # Resolve the libnccl path from the first target process.
    lib_path = resolve_libnccl_path(pids[0], args.lib_path)

    # Create the output directory; output_files is filled by the callback.
    os.makedirs("data", exist_ok=True)
    output_files = {}

    print(f"All matched PIDs: {pids}")
    print(f"libnccl path: {lib_path}")
    print(f"Output directory: data/")

    b = BPF(text=build_bpf_program())
    attached = attach_hooks(b, lib_path)

    print(f"Attached symbols: {', '.join(attached)}")
    if args.debug:
        print("-" * 100)
        print(f"{'TIME':<10} | {'TYPE':<7} | {'PID':<7} | {'TID':<7} | {'DETAILS'}")
        print("-" * 100)
    else:
        print("Tracing ... (Ctrl+C to stop, use --debug to see live output)")

    handle_event = make_event_handler(output_files, args.debug)
    b["events"].open_perf_buffer(handle_event)

    try:
        while True:
            b.perf_buffer_poll()
    except KeyboardInterrupt:
        pass
    finally:
        for f in output_files.values():
            f.close()
        print(f"\nDone. Output saved to data/ ({len(pids)} files)")


if __name__ == "__main__":
    main()

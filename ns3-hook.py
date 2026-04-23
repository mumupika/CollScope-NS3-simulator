#!/usr/bin/env python3
"""
ns3-hook.py: BPF hook for tracing RdmaCC::Send and RdmaCC::SendChunkFinish
Records simulation timestamps (Simulator::Now().GetTimeStep()) per node rank.

Workflow:
  1. Start this script (sudo python3 ns3-hook.py)
  2. In another terminal: cd Vedrfolnir && ./waf --run 'scratch/mix_allreduce mix_allreduce/config.txt'
  3. Script auto-detects process, attaches BPF uprobes, records events
  4. Per-rank files written to hook_output/ as rank_N.txt

Output format per file (one per m_rank):
    sim_time  AG  0|1  time_diff
  - sim_time = Simulator::Now().GetTimeStep() (simulation timestamp in nanoseconds)
  - AG = algorithm name (configurable via --alg, default "AG")
  - 0 = RdmaCC::Send completed, 1 = RdmaCC::SendChunkFinish completed
  - time_diff: for type 0 it's 0; for type 1 it's the sim_time difference since the paired Send

Usage:
    sudo python3 ns3-hook.py [--alg <name>] [--outdir <output_dir>] [--attach-pid <pid>]
"""

import os
import sys
import time
import shutil
import subprocess
import argparse
import ctypes
import struct
from collections import defaultdict

# ==============================================================================
# Configuration
# ==============================================================================

WORKSPACE = "/root/Vedrfolnir"
Vedrfolnir_BUILD = os.path.join(WORKSPACE, "Vedrfolnir", "build")
APPS_LIB_PATH = os.path.join(Vedrfolnir_BUILD, "libns3.18-applications-debug.so")
CORE_LIB_PATH = os.path.join(Vedrfolnir_BUILD, "libns3.18-core-debug.so")
DEFAULT_OUTDIR = os.path.join(WORKSPACE, "hook_output")

PROCESS_NAME = "mix_allreduce"

# Offset of m_currentTs in DefaultSimulatorImpl (from disassembly of Now())
M_CURRENT_TS_OFFSET = 0x78
# Offset of m_rank in RdmaCC (from disassembly of SetRank: mov %dx,0xa6(%rax))
M_RANK_OFFSET = 0xA6

# Symbol names
PEEK_IMPL_SYM = "_ZZN3ns3L8PeekImplEvE4impl"
SEND_SYM = "_ZN3ns36RdmaCC4SendEt"
SCF_SYM = "_ZN3ns36RdmaCC15SendChunkFinishEv"

FUNC_SEND = 0
FUNC_SEND_CHUNK_FINISH = 1

# ==============================================================================
# BPF Program
# ==============================================================================

BPF_PROGRAM = r"""
#include <uapi/linux/ptrace.h>
#include <linux/types.h>

struct event_t {
    u64 this_ptr;
    u64 sim_time;
    u32 func_id;      // 0=Send, 1=SendChunkFinish
    u32 pid;
    u64 arg1;         // distRank for Send
    u32 m_rank;       // node rank read from this+0xA6
    u32 _pad;
};

BPF_ARRAY(impl_addr_map, u64, 1);
BPF_PERF_OUTPUT(events);
BPF_HASH(send_ctx, u64, u64, 1024);
BPF_HASH(send_arg1_ctx, u64, u64, 1024);
BPF_HASH(scf_ctx, u64, u64, 1024);

#define CURRENT_TS_OFFSET 0x78
#define RANK_OFFSET 0xA6

static __always_inline u64 read_sim_time() {
    u64 key = 0;
    u64 *addr_ptr = impl_addr_map.lookup(&key);
    if (!addr_ptr || *addr_ptr == 0)
        return 0;

    u64 impl_ptr = 0;
    long ret = bpf_probe_read_user(&impl_ptr, sizeof(impl_ptr), (void*)*addr_ptr);
    if (ret < 0 || impl_ptr == 0)
        return 0;

    u64 current_ts = 0;
    ret = bpf_probe_read_user(&current_ts, sizeof(current_ts),
                              (void*)(impl_ptr + CURRENT_TS_OFFSET));
    if (ret < 0)
        return 0;

    return current_ts;
}

static __always_inline u32 read_rank(u64 this_ptr) {
    u16 rank = 0;
    long ret = bpf_probe_read_user(&rank, sizeof(rank), (void*)(this_ptr + RANK_OFFSET));
    if (ret < 0)
        return 0xFFFFFFFF;
    return (u32)rank;
}

int on_send_entry(struct pt_regs *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 this_ptr = (u64)PT_REGS_PARM1(ctx);
    u64 dist_rank = (u64)PT_REGS_PARM2(ctx);
    send_ctx.update(&pid_tgid, &this_ptr);
    send_arg1_ctx.update(&pid_tgid, &dist_rank);
    return 0;
}

int on_send_return(struct pt_regs *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 *this_ptr = send_ctx.lookup(&pid_tgid);
    if (!this_ptr)
        return 0;
    u64 *dist_rank = send_arg1_ctx.lookup(&pid_tgid);
    struct event_t event = {};
    event.this_ptr = *this_ptr;
    event.sim_time = read_sim_time();
    event.func_id = 0;
    event.pid = pid_tgid >> 32;
    event.arg1 = dist_rank ? *dist_rank : 0;
    event.m_rank = read_rank(*this_ptr);
    event._pad = 0;
    events.perf_submit(ctx, &event, sizeof(event));
    send_ctx.delete(&pid_tgid);
    send_arg1_ctx.delete(&pid_tgid);
    return 0;
}

int on_scf_entry(struct pt_regs *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 this_ptr = (u64)PT_REGS_PARM1(ctx);
    scf_ctx.update(&pid_tgid, &this_ptr);
    return 0;
}

int on_scf_return(struct pt_regs *ctx) {
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 *this_ptr = scf_ctx.lookup(&pid_tgid);
    if (!this_ptr)
        return 0;
    struct event_t event = {};
    event.this_ptr = *this_ptr;
    event.sim_time = read_sim_time();
    event.func_id = 1;
    event.pid = pid_tgid >> 32;
    event.arg1 = 0;
    event.m_rank = read_rank(*this_ptr);
    event._pad = 0;
    events.perf_submit(ctx, &event, sizeof(event));
    scf_ctx.delete(&pid_tgid);
    return 0;
}
"""

# ==============================================================================
# Helper Functions
# ==============================================================================

def find_pid_by_name(name, exclude_self=True):
    """Find PID of a process. Prefers the actual ELF binary over waf/python wrappers."""
    pids = []
    binary_pids = []
    my_pid = os.getpid()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if exclude_self and pid == my_pid:
                continue
            try:
                # Check /proc/pid/exe to find the actual binary
                exe_link = os.readlink(f"/proc/{pid}/exe")
                if name in exe_link and exe_link.endswith(name):
                    binary_pids.append(pid)
                    continue
            except (FileNotFoundError, PermissionError, OSError):
                pass
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="replace")
                    if name in cmdline:
                        pids.append(pid)
            except (FileNotFoundError, PermissionError):
                continue
    except Exception:
        pass
    # Prefer actual binary processes over waf/python wrappers
    return binary_pids if binary_pids else pids


def wait_for_process(name, poll_interval=0.5):
    print(f"[INFO] Waiting for process '{name}' to appear...")
    print(f"[INFO] Run in another terminal:")
    print(f"       cd {os.path.join(WORKSPACE, 'Vedrfolnir')} && ./waf --run 'scratch/mix_allreduce mix_allreduce/config.txt'")
    print()
    while True:
        pids = find_pid_by_name(name)
        if pids:
            if len(pids) > 1:
                print(f"[WARN] Multiple '{name}' processes found: {pids}, using first: {pids[0]}")
            return pids[0]
        time.sleep(poll_interval)


def read_proc_maps(pid):
    maps = []
    try:
        with open(f"/proc/{pid}/maps", "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 6:
                    continue
                addr_range = parts[0]
                perms = parts[1]
                offset = int(parts[2], 16)
                path = parts[5] if len(parts) > 5 else ""
                start_s, end_s = addr_range.split("-")
                start = int(start_s, 16)
                end = int(end_s, 16)
                maps.append((start, end, perms, offset, path))
    except FileNotFoundError:
        print(f"[ERROR] Process {pid} not found.")
        sys.exit(1)
    return maps


def find_lib_base(maps, lib_name):
    for start, end, perms, offset, path in maps:
        if lib_name in path and offset == 0:
            return start
    for start, end, perms, offset, path in maps:
        if lib_name in path:
            return start - offset
    return None


def find_symbol_offset(lib_path, symbol):
    try:
        result = subprocess.run(["nm", lib_path], capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[-1] == symbol:
                return int(parts[0], 16)
    except subprocess.CalledProcessError:
        pass
    return None


def read_process_memory(pid, address, size=8):
    try:
        with open(f"/proc/{pid}/mem", "rb") as f:
            f.seek(address)
            data = f.read(size)
            if len(data) == 8:
                return struct.unpack("<Q", data)[0]
    except (OSError, IOError):
        pass
    return None


def check_bcc_support():
    from bcc import BPF
    test_code = """
#include <uapi/linux/ptrace.h>
int test_fn(struct pt_regs *ctx) {
    char buf[8];
    bpf_probe_read_user(&buf, sizeof(buf), (void*)0x1);
    return 0;
}
"""
    try:
        b = BPF(text=test_code)
        b.load_func("test_fn", BPF.TRACEPOINT)
        return True
    except Exception:
        return False


# ==============================================================================
# Main
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="BPF hook for tracing RdmaCC functions")
    parser.add_argument("--alg", default="AG", help="Algorithm name to label output (default: AG)")
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR, help="Output directory")
    parser.add_argument("--attach-pid", type=int, default=None, help="Attach to specific PID directly")
    args = parser.parse_args()
    alg_name = args.alg

    print("=" * 70)
    print("ns3 BPF Hook - RdmaCC Tracer")
    print("=" * 70)

    # Validate library paths
    for desc, path in [("Applications lib", APPS_LIB_PATH), ("Core lib", CORE_LIB_PATH)]:
        if not os.path.exists(path):
            print(f"[ERROR] {desc} not found: {path}")
            sys.exit(1)

    # Clean old output files
    if os.path.exists(args.outdir):
        print(f"[INFO] Cleaning old output in {args.outdir}")
        shutil.rmtree(args.outdir)
    os.makedirs(args.outdir, exist_ok=True)

    # Check BCC
    print("[INFO] Checking BCC bpf_probe_read_user support...")
    if not check_bcc_support():
        print("[ERROR] bpf_probe_read_user not supported. Install newer BCC:")
        print("  sudo apt-get install -y python3-bpfcc libbpfcc")
        sys.exit(1)
    print("[OK] bpf_probe_read_user supported")

    # Resolve symbol offsets
    print("\n[INFO] Resolving symbol offsets...")
    peek_impl_offset = find_symbol_offset(CORE_LIB_PATH, PEEK_IMPL_SYM)
    send_offset = find_symbol_offset(APPS_LIB_PATH, SEND_SYM)
    scf_offset = find_symbol_offset(APPS_LIB_PATH, SCF_SYM)
    for name, off in [("PeekImpl()::impl", peek_impl_offset),
                      ("RdmaCC::Send", send_offset),
                      ("RdmaCC::SendChunkFinish", scf_offset)]:
        if off:
            print(f"  {name}: 0x{off:x}")
        else:
            print(f"  [ERROR] {name} NOT FOUND")
    if not all([peek_impl_offset, send_offset, scf_offset]):
        sys.exit(1)
    print(f"  m_rank offset in RdmaCC: 0x{M_RANK_OFFSET:x}")
    print(f"  m_currentTs offset in DefaultSimulatorImpl: 0x{M_CURRENT_TS_OFFSET:x}")

    # Load BPF program
    print("\n[INFO] Loading BPF program...")
    from bcc import BPF
    b = BPF(text=BPF_PROGRAM)
    print("[OK] BPF program loaded")

    # Find or wait for process
    if args.attach_pid:
        pid = args.attach_pid
        print(f"\n[INFO] Using specified PID: {pid}")
    else:
        pid = wait_for_process(PROCESS_NAME)
        print(f"[INFO] Detected '{PROCESS_NAME}' process: PID={pid}")

    # Read process memory map (with retry - libraries may not be loaded yet)
    # The waf wrapper may spawn the real binary as a child process, so we
    # may need to re-detect the correct PID.
    print("\n[INFO] Reading process memory map...")
    core_base = None
    apps_base = None
    max_attempts = 60  # up to 30 seconds
    for attempt in range(max_attempts):
        # Re-check if process still exists
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # Process died, try to find a new one
            print(f"\n  PID {pid} exited, re-detecting...", end="", flush=True)
            new_pids = find_pid_by_name(PROCESS_NAME)
            if new_pids:
                pid = new_pids[0]
                print(f" new PID={pid}", end="", flush=True)
            else:
                time.sleep(0.5)
                continue

        maps = read_proc_maps(pid)
        core_base = find_lib_base(maps, "libns3.18-core-debug.so")
        apps_base = find_lib_base(maps, "libns3.18-applications-debug.so")
        if core_base and apps_base:
            break
        if attempt == 0:
            print(f"  Libraries not loaded yet, waiting...", end="", flush=True)
        elif attempt % 10 == 0:
            print(f" {attempt}", end="", flush=True)
        else:
            print(".", end="", flush=True)
        time.sleep(0.5)
    print()

    if not core_base or not apps_base:
        print("[ERROR] Could not find required libraries in process memory.")
        for s, e, p, o, path in maps:
            if "ns3" in path and ".so" in path:
                print(f"  0x{s:x}-0x{e:x} {path}")
        sys.exit(1)

    print(f"  Core lib base:  0x{core_base:x}")
    print(f"  Apps lib base:  0x{apps_base:x}")

    impl_abs_addr = core_base + peek_impl_offset
    print(f"  PeekImpl()::impl absolute: 0x{impl_abs_addr:x}")

    impl_ptr = read_process_memory(pid, impl_abs_addr)
    if impl_ptr is not None:
        print(f"  SimulatorImpl* = 0x{impl_ptr:x}")
    else:
        print("[WARN] Could not read impl pointer yet")

    # Set BPF map
    impl_addr_map = b.get_table("impl_addr_map")
    impl_addr_map[0] = ctypes.c_uint64(impl_abs_addr)
    print(f"\n[INFO] BPF impl_addr_map[0] = 0x{impl_abs_addr:x}")

    # Attach uprobes
    print("\n[INFO] Attaching uprobes...")
    try:
        b.attach_uprobe(name=APPS_LIB_PATH, addr=send_offset, fn_name="on_send_entry", pid=pid)
        b.attach_uretprobe(name=APPS_LIB_PATH, addr=send_offset, fn_name="on_send_return", pid=pid)
        print(f"  [OK] RdmaCC::Send (0x{send_offset:x})")
        b.attach_uprobe(name=APPS_LIB_PATH, addr=scf_offset, fn_name="on_scf_entry", pid=pid)
        b.attach_uretprobe(name=APPS_LIB_PATH, addr=scf_offset, fn_name="on_scf_return", pid=pid)
        print(f"  [OK] RdmaCC::SendChunkFinish (0x{scf_offset:x})")
    except Exception as e:
        print(f"[ERROR] Failed to attach uprobes: {e}")
        sys.exit(1)

    # ==================================================================
    # Event handling - per-rank output with FIFO queue for pairing
    # ==================================================================
    # Since Sends can overlap (Send1, Send2, ..., SCF1, SCF2, ...),
    # we use a FIFO queue per rank to properly pair each SendChunkFinish
    # with its corresponding Send.
    rank_files = {}                # m_rank -> file handle
    rank_send_queue = {}           # m_rank -> deque of Send sim_times (FIFO)
    event_count = defaultdict(int)

    print(f"[INFO] Algorithm label: {alg_name}")

    def get_rank_file(rank):
        if rank not in rank_files:
            fname = os.path.join(args.outdir, f"rank_{rank}.txt")
            f = open(fname, "w")
            f.write(f"# RdmaCC trace for rank {rank}\n")
            f.write(f"# format: sim_time  {alg_name}  type(0=Send,1=SCF)  time_diff\n")
            f.write("#" + "=" * 60 + "\n")
            rank_files[rank] = f
            rank_send_queue[rank] = []
            print(f"[INFO] Created: {fname}")
        return rank_files[rank]

    def handle_event(cpu, data, size):
        event = b["events"].event(data)
        rank = event.m_rank
        sim_time = event.sim_time
        func_id = event.func_id

        # Skip events with invalid rank
        if rank == 0xFFFFFFFF:
            return

        event_count[f"rank_{rank}_{func_id}"] += 1
        f = get_rank_file(rank)

        if func_id == FUNC_SEND:
            # Send completed: push timestamp onto FIFO queue
            rank_send_queue[rank].append(sim_time)
            f.write(f"{sim_time}\t{alg_name}\t0\t0\n")
            f.flush()
        else:
            # SendChunkFinish completed: pop oldest Send to compute diff
            send_queue = rank_send_queue.get(rank, [])
            if send_queue:
                paired_send_time = send_queue.pop(0)
                rank_send_queue[rank] = send_queue
                time_diff = sim_time - paired_send_time
            else:
                time_diff = 0
            f.write(f"{sim_time}\t{alg_name}\t1\t{time_diff}\n")
            f.flush()

    b["events"].open_perf_buffer(handle_event)

    # ==================================================================
    # Main loop
    # ==================================================================
    print("\n" + "=" * 70)
    print(f"Tracing PID {pid}. Waiting for events...")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            b.perf_buffer_poll(timeout=200)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                print(f"\n[INFO] Process {pid} has exited")
                for _ in range(100):
                    b.perf_buffer_poll(timeout=50)
                break
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted")

    # Cleanup
    print("\n" + "=" * 70)
    print("Event summary:")
    for key, count in sorted(event_count.items()):
        print(f"  {key}: {count}")

    print(f"\nOutput files in: {args.outdir}")
    for rank, f in sorted(rank_files.items()):
        f.close()
        print(f"  rank_{rank}.txt")

    if not rank_files:
        print("  (no events captured)")

    print("\n[INFO] Done.")


if __name__ == "__main__":
    main()
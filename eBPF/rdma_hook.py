#!/usr/bin/env python3
"""
rdma_hook.py: RDMA tracing + ncclAllReduce correlation + CSV output

Usage:
  python3 rdma_hook.py --nccl /path/to/libnccl.so.2 --filter auto --csv data.csv
  python3 rdma_hook.py --gpus 1 --csv data.csv

Options:
  --filter write/recv/all/auto
  --with-imm
  --output FILE   Text log output
  --csv FILE      CSV output
  --nccl PATH
  --gpus N        GPUs per process, round = seq // N, default 2
  --gap-ms N      Burst gap threshold in ms when the NCCL hook is unavailable, default 10
"""

from bcc import BPF
import argparse
import csv
import glob
import os
import subprocess

# ========== Argument parsing ==========

parser = argparse.ArgumentParser()
parser.add_argument("--filter", type=str, default="all",
                    choices=["write", "recv", "all", "auto"])
parser.add_argument("--output", type=str, default=None)
parser.add_argument("--csv", type=str, default=None, dest="csv_file",
                    help="CSV output file path")
parser.add_argument("--with-imm", action="store_true")
parser.add_argument("--nccl", type=str, default=None)
parser.add_argument("--gpus", type=int, default=2,
                    help="GPUs per process, round = seq // gpus, default 2")
parser.add_argument("--gap-ms", type=float, default=10.0)
args = parser.parse_args()

outfile = None
if args.output:
    outfile = open(args.output, "w")

# -- CSV initialization --
CSV_FIELDS = [
    "timestamp_ns", "time_ms", "pid", "comm", "type", "round", "nccl_seq",
    "qp_num", "opcode", "opcode_name", "send_flags", "size_bytes", "num_sge",
    "wr_id", "remote_addr", "rkey", "imm_data",
    "wc_status", "wc_status_name", "wc_opcode", "wc_opcode_name",
    "wc_byte_len", "wc_imm_data", "wc_qp_num", "wc_src_qp", "wc_flags",
    "latency_ns", "cq_from_imm",
    "nccl_count", "nccl_datatype", "nccl_datatype_name", "nccl_op", "nccl_op_name",
]
csv_fp = None
csv_writer = None
if args.csv_file:
    csv_fp = open(args.csv_file, "w", newline="")
    csv_writer = csv.DictWriter(csv_fp, fieldnames=CSV_FIELDS)
    csv_writer.writeheader()
    csv_fp.flush()


def csv_row(row_dict):
    if csv_writer:
        csv_writer.writerow(row_dict)
        csv_fp.flush()


def log(msg):
    print(msg)
    if outfile:
        outfile.write(msg + "\n")
        outfile.flush()


# ========== Structure offsets ==========

LIBIBVERBS = "/lib/x86_64-linux-gnu/libibverbs.so"

OFF_QP_CONTEXT    = 0
OFF_QP_NUM        = 52
OFF_CTX_OPS       = 8
OFF_OPS_POST_SEND = 200
OFF_OPS_POST_RECV = 208
OFF_OPS_POLL_CQ   = 88

OFF_WR_ID         = 0
OFF_WR_SG_LIST    = 16
OFF_WR_NUM_SGE    = 24
OFF_WR_OPCODE     = 28
OFF_WR_SEND_FLAGS = 32
OFF_WR_IMM_DATA   = 36
OFF_WR_RDMA_ADDR  = 40
OFF_WR_RDMA_RKEY  = 48

OFF_RECV_WR_ID      = 0
OFF_RECV_WR_SG_LIST = 16
OFF_RECV_WR_NUM_SGE = 24

OFF_SGE_LENGTH = 8

OFF_WC_WR_ID    = 0
OFF_WC_STATUS   = 8
OFF_WC_OPCODE   = 12
OFF_WC_BYTE_LEN = 20
OFF_WC_IMM_DATA = 24
OFF_WC_QP_NUM   = 28
OFF_WC_SRC_QP   = 32
OFF_WC_FLAGS    = 36
SIZEOF_WC       = 48

# ========== Phase 1 BPF ==========

discover_bpf = """
#include <uapi/linux/ptrace.h>

BPF_PERF_OUTPUT(fn_addrs);

struct fn_addr_event_t {
    u32 pid;
    u64 post_send_addr;
    u64 post_recv_addr;
    u64 poll_cq_addr;
};

int trace_modify_qp(struct pt_regs *ctx) {
    struct fn_addr_event_t e = {};
    e.pid = bpf_get_current_pid_tgid() >> 32;

    u64 qp = PT_REGS_PARM1(ctx);
    if (qp == 0) return 0;

    u64 context = 0;
    bpf_probe_read_user(&context, 8, (void *)(qp + __OFF_QP_CTX__));
    if (context == 0) return 0;

    u64 ops_base = context + __OFF_CTX_OPS__;
    bpf_probe_read_user(&e.post_send_addr, 8, (void *)(ops_base + __OFF_POST_SEND__));
    bpf_probe_read_user(&e.post_recv_addr, 8, (void *)(ops_base + __OFF_POST_RECV__));
    bpf_probe_read_user(&e.poll_cq_addr,   8, (void *)(ops_base + __OFF_POLL_CQ__));

    fn_addrs.perf_submit(ctx, &e, sizeof(e));
    return 0;
}
""".replace("__OFF_QP_CTX__",    str(OFF_QP_CONTEXT)) \
   .replace("__OFF_CTX_OPS__",   str(OFF_CTX_OPS)) \
   .replace("__OFF_POST_SEND__", str(OFF_OPS_POST_SEND)) \
   .replace("__OFF_POST_RECV__", str(OFF_OPS_POST_RECV)) \
   .replace("__OFF_POLL_CQ__",   str(OFF_OPS_POLL_CQ))

# ========== Phase 2 BPF ==========
# type: 0=post_send  1=post_recv  2=cq_completion  3=nccl_allreduce_enter

trace_bpf = """
#include <uapi/linux/ptrace.h>

BPF_PERF_OUTPUT(events);

struct poll_args_t {
    u64 wc_ptr;
    u32 ne;
};
BPF_HASH(poll_args, u64, struct poll_args_t);

struct wr_key_t {
    u32 qp_num;
    u64 wr_id;
};
BPF_HASH(send_ts_map,    struct wr_key_t, u64, 65536);
BPF_HASH(recv_ts_map,    struct wr_key_t, u64, 65536);
BPF_HASH(send_seq_map,   struct wr_key_t, u32, 65536);
BPF_HASH(recv_seq_map,   struct wr_key_t, u32, 65536);
BPF_HASH(send_is_imm_map, struct wr_key_t, u8,  65536);

BPF_ARRAY(nccl_seq_counter, u32, 1);
BPF_HASH(nccl_last_seq, u32, u32);   // key=0 -> global latest nccl_seq

struct event_t {
    u64 ts;
    u32 pid;
    u8  type;
    u8  cq_from_imm;
    u8  _pad1[2];
    u32 qp_num;
    u64 wr_id;
    u32 opcode;
    u32 send_flags;
    u32 size;
    u32 num_sge;
    u64 remote_addr;
    u32 rkey;
    u32 imm_data;
    u32 wc_status;
    u32 wc_opcode;
    u32 wc_byte_len;
    u32 wc_imm_data;
    u32 wc_qp_num;
    u32 wc_src_qp;
    u32 wc_flags;
    u64 latency_ns;
    char comm[16];
    u64 nccl_count;
    u32 nccl_datatype;
    u32 nccl_op;
    u32 nccl_seq;
    u32 _pad2;
};

static __always_inline u32 get_last_seq() {
    u32 key = 0;
    u32 *p = nccl_last_seq.lookup(&key);
    return p ? *p : 0xFFFFFFFF;
}

// ========== post_send ==========
int trace_post_send(struct pt_regs *ctx) {
    struct event_t e = {};
    e.type = 0;
    e.ts   = bpf_ktime_get_ns();
    e.pid  = bpf_get_current_pid_tgid() >> 32;
    bpf_get_current_comm(&e.comm, sizeof(e.comm));
    e.nccl_seq = get_last_seq();

    u64 qp = PT_REGS_PARM1(ctx);
    u64 wr = PT_REGS_PARM2(ctx);
    if (qp == 0 || wr == 0) return 0;

    bpf_probe_read_user(&e.qp_num,      4, (void *)(qp + __OFF_QP_NUM__));
    bpf_probe_read_user(&e.wr_id,       8, (void *)(wr + __OFF_WR_ID__));
    bpf_probe_read_user(&e.opcode,      4, (void *)(wr + __OFF_WR_OPCODE__));
    bpf_probe_read_user(&e.send_flags,  4, (void *)(wr + __OFF_WR_SEND_FLAGS__));
    bpf_probe_read_user(&e.num_sge,     4, (void *)(wr + __OFF_WR_NUM_SGE__));
    bpf_probe_read_user(&e.imm_data,    4, (void *)(wr + __OFF_WR_IMM_DATA__));
    bpf_probe_read_user(&e.remote_addr, 8, (void *)(wr + __OFF_WR_RDMA_ADDR__));
    bpf_probe_read_user(&e.rkey,        4, (void *)(wr + __OFF_WR_RDMA_RKEY__));

    u64 sg_list = 0;
    bpf_probe_read_user(&sg_list, 8, (void *)(wr + __OFF_WR_SG_LIST__));
    if (sg_list != 0)
        bpf_probe_read_user(&e.size, 4, (void *)(sg_list + __OFF_SGE_LEN__));

    struct wr_key_t key = {};
    key.qp_num = e.qp_num;
    key.wr_id  = e.wr_id;
    send_ts_map.update(&key, &e.ts);
    send_seq_map.update(&key, &e.nccl_seq);

    u8 is_imm = (e.opcode == 1) ? 1 : 0;
    send_is_imm_map.update(&key, &is_imm);

    events.perf_submit(ctx, &e, sizeof(e));
    return 0;
}

// ========== post_recv ==========
int trace_post_recv(struct pt_regs *ctx) {
    struct event_t e = {};
    e.type = 1;
    e.ts   = bpf_ktime_get_ns();
    e.pid  = bpf_get_current_pid_tgid() >> 32;
    bpf_get_current_comm(&e.comm, sizeof(e.comm));
    e.nccl_seq = get_last_seq();

    u64 qp = PT_REGS_PARM1(ctx);
    u64 wr = PT_REGS_PARM2(ctx);
    if (qp == 0 || wr == 0) return 0;

    bpf_probe_read_user(&e.qp_num,  4, (void *)(qp + __OFF_QP_NUM__));
    bpf_probe_read_user(&e.wr_id,   8, (void *)(wr + __OFF_RECV_WR_ID__));
    bpf_probe_read_user(&e.num_sge, 4, (void *)(wr + __OFF_RECV_WR_NUM_SGE__));

    u64 sg_list = 0;
    bpf_probe_read_user(&sg_list, 8, (void *)(wr + __OFF_RECV_WR_SG_LIST__));
    if (sg_list != 0)
        bpf_probe_read_user(&e.size, 4, (void *)(sg_list + __OFF_SGE_LEN__));

    struct wr_key_t key = {};
    key.qp_num = e.qp_num;
    key.wr_id  = e.wr_id;
    recv_ts_map.update(&key, &e.ts);
    recv_seq_map.update(&key, &e.nccl_seq);

    events.perf_submit(ctx, &e, sizeof(e));
    return 0;
}

// ========== poll_cq ==========
int trace_poll_cq_entry(struct pt_regs *ctx) {
    u64 id = bpf_get_current_pid_tgid();
    struct poll_args_t a = {};
    a.wc_ptr = PT_REGS_PARM3(ctx);
    a.ne     = PT_REGS_PARM2(ctx);
    poll_args.update(&id, &a);
    return 0;
}

int trace_poll_cq_return(struct pt_regs *ctx) {
    int ret = PT_REGS_RC(ctx);
    if (ret <= 0) return 0;

    u64 id = bpf_get_current_pid_tgid();
    struct poll_args_t *a = poll_args.lookup(&id);
    if (!a) return 0;

    u64 wc_base = a->wc_ptr;
    poll_args.delete(&id);
    u64 now = bpf_ktime_get_ns();

    #pragma unroll
    for (int i = 0; i < 8; i++) {
        if (i >= ret) break;

        struct event_t e = {};
        e.type = 2;
        e.ts   = now;
        e.pid  = bpf_get_current_pid_tgid() >> 32;
        e.nccl_seq = 0xFFFFFFFF;
        bpf_get_current_comm(&e.comm, sizeof(e.comm));

        u64 wc = wc_base + i * __SIZEOF_WC__;

        bpf_probe_read_user(&e.wr_id,       8, (void *)(wc + __OFF_WC_WR_ID__));
        bpf_probe_read_user(&e.wc_status,   4, (void *)(wc + __OFF_WC_STATUS__));
        bpf_probe_read_user(&e.wc_opcode,   4, (void *)(wc + __OFF_WC_OPCODE__));
        bpf_probe_read_user(&e.wc_byte_len, 4, (void *)(wc + __OFF_WC_BYTE_LEN__));
        bpf_probe_read_user(&e.wc_imm_data, 4, (void *)(wc + __OFF_WC_IMM_DATA__));
        bpf_probe_read_user(&e.wc_qp_num,   4, (void *)(wc + __OFF_WC_QP_NUM__));
        bpf_probe_read_user(&e.wc_src_qp,   4, (void *)(wc + __OFF_WC_SRC_QP__));
        bpf_probe_read_user(&e.wc_flags,    4, (void *)(wc + __OFF_WC_FLAGS__));

        struct wr_key_t key = {};
        key.qp_num = e.wc_qp_num;
        key.wr_id  = e.wr_id;

        if (e.wc_opcode < 128) {
            u64 *ts_ptr = send_ts_map.lookup(&key);
            if (ts_ptr) {
                e.latency_ns = now - *ts_ptr;
                send_ts_map.delete(&key);
            }
            u32 *seq_ptr = send_seq_map.lookup(&key);
            if (seq_ptr) {
                e.nccl_seq = *seq_ptr;
                send_seq_map.delete(&key);
            }
            u8 *imm_flag = send_is_imm_map.lookup(&key);
            if (imm_flag) {
                e.cq_from_imm = *imm_flag;
                send_is_imm_map.delete(&key);
            }
        } else {
            u64 *ts_ptr = recv_ts_map.lookup(&key);
            if (ts_ptr) {
                e.latency_ns = now - *ts_ptr;
                recv_ts_map.delete(&key);
            }
            u32 *seq_ptr = recv_seq_map.lookup(&key);
            if (seq_ptr) {
                e.nccl_seq = *seq_ptr;
                recv_seq_map.delete(&key);
            }
        }

        events.perf_submit(ctx, &e, sizeof(e));
    }
    return 0;
}

// ========== ncclAllReduce entry ==========
int trace_nccl_allreduce(struct pt_regs *ctx) {
    struct event_t e = {};
    e.type = 3;
    e.ts   = bpf_ktime_get_ns();
    e.pid  = bpf_get_current_pid_tgid() >> 32;
    bpf_get_current_comm(&e.comm, sizeof(e.comm));

    e.nccl_count    = PT_REGS_PARM3(ctx);
    e.nccl_datatype = (u32)PT_REGS_PARM4(ctx);
    e.nccl_op       = (u32)PT_REGS_PARM5(ctx);

    u32 zero = 0;
    u32 *seqp = nccl_seq_counter.lookup(&zero);
    if (seqp) {
        e.nccl_seq = *seqp;
        lock_xadd(seqp, 1);
    }

    u32 gkey = 0;
    nccl_last_seq.update(&gkey, &e.nccl_seq);

    events.perf_submit(ctx, &e, sizeof(e));
    return 0;
}
""".replace("__OFF_QP_NUM__",          str(OFF_QP_NUM)) \
   .replace("__OFF_WR_ID__",           str(OFF_WR_ID)) \
   .replace("__OFF_WR_OPCODE__",       str(OFF_WR_OPCODE)) \
   .replace("__OFF_WR_SEND_FLAGS__",   str(OFF_WR_SEND_FLAGS)) \
   .replace("__OFF_WR_NUM_SGE__",      str(OFF_WR_NUM_SGE)) \
   .replace("__OFF_WR_IMM_DATA__",     str(OFF_WR_IMM_DATA)) \
   .replace("__OFF_WR_RDMA_ADDR__",    str(OFF_WR_RDMA_ADDR)) \
   .replace("__OFF_WR_RDMA_RKEY__",    str(OFF_WR_RDMA_RKEY)) \
   .replace("__OFF_WR_SG_LIST__",      str(OFF_WR_SG_LIST)) \
   .replace("__OFF_SGE_LEN__",         str(OFF_SGE_LENGTH)) \
   .replace("__OFF_RECV_WR_ID__",      str(OFF_RECV_WR_ID)) \
   .replace("__OFF_RECV_WR_NUM_SGE__", str(OFF_RECV_WR_NUM_SGE)) \
   .replace("__OFF_RECV_WR_SG_LIST__", str(OFF_RECV_WR_SG_LIST)) \
   .replace("__SIZEOF_WC__",           str(SIZEOF_WC)) \
   .replace("__OFF_WC_WR_ID__",        str(OFF_WC_WR_ID)) \
   .replace("__OFF_WC_STATUS__",       str(OFF_WC_STATUS)) \
   .replace("__OFF_WC_OPCODE__",       str(OFF_WC_OPCODE)) \
   .replace("__OFF_WC_BYTE_LEN__",     str(OFF_WC_BYTE_LEN)) \
   .replace("__OFF_WC_IMM_DATA__",     str(OFF_WC_IMM_DATA)) \
   .replace("__OFF_WC_QP_NUM__",       str(OFF_WC_QP_NUM)) \
   .replace("__OFF_WC_SRC_QP__",       str(OFF_WC_SRC_QP)) \
   .replace("__OFF_WC_FLAGS__",        str(OFF_WC_FLAGS))

# ========== Name mappings ==========

SEND_OPCODE = {
    0: "RDMA_WRITE", 1: "RDMA_WRITE_IMM", 2: "SEND", 3: "SEND_IMM",
    4: "RDMA_READ",  5: "ATOMIC_CMP_SWP", 6: "ATOMIC_FETCH_ADD",
}
WC_OPCODE = {
    0: "CQ_SEND", 1: "CQ_RDMA_WRITE", 2: "CQ_RDMA_READ",
    3: "CQ_COMP_SWAP", 4: "CQ_FETCH_ADD", 5: "CQ_BIND_MW",
    6: "CQ_LOCAL_INV", 7: "CQ_TSO",
    128: "CQ_RECV", 129: "CQ_RECV_IMM", 130: "CQ_RECV_INV",
}
WC_STATUS = {
    0: "SUCCESS", 1: "LOC_LEN_ERR", 2: "LOC_QP_OP_ERR",
    3: "LOC_EEC_OP_ERR", 4: "LOC_PROT_ERR", 5: "WR_FLUSH_ERR",
    6: "MW_BIND_ERR", 7: "BAD_RESP_ERR", 8: "LOC_ACCESS_ERR",
    9: "REM_INV_REQ_ERR", 10: "REM_ACCESS_ERR", 11: "REM_OP_ERR",
    12: "RETRY_EXC_ERR", 13: "RNR_RETRY_EXC_ERR",
    19: "FATAL_ERR", 20: "RESP_TIMEOUT_ERR", 21: "GENERAL_ERR",
}
SEND_FLAGS   = {1: "FENCE", 2: "SIGNALED", 4: "SOLICITED", 8: "INLINE", 16: "IP_CSUM"}
WC_FLAGS_MAP = {1: "GRH", 2: "WITH_IMM", 4: "IP_CSUM_OK", 16: "WITH_INV"}
NCCL_DTYPE = {
    0: "int8",  1: "uint8",   2: "int32",   3: "uint32",
    4: "int64", 5: "uint64",  6: "float16", 7: "float32",
    8: "float64", 9: "bfloat16",
}
NCCL_OP  = {0: "Sum", 1: "Prod", 2: "Max", 3: "Min", 4: "Avg"}
NO_COLL  = 0xFFFFFFFF


def flags_str(flags, mapping):
    parts = [name for bit, name in mapping.items() if flags & bit]
    return "|".join(parts) if parts else "0"


def size_str(nbytes):
    if nbytes >= 1048576: return f"{nbytes/1048576:.1f}MB"
    if nbytes >= 1024:    return f"{nbytes/1024:.1f}KB"
    return f"{nbytes}B"


def latency_str(ns):
    if ns == 0:   return "-"
    if ns >= 1e6: return f"{ns/1e6:.2f}ms"
    if ns >= 1e3: return f"{ns/1e3:.1f}us"
    return f"{ns}ns"


def round_label(seq):
    if seq == NO_COLL or not nccl_hooked:
        return "[?    ]"
    return f"[R{seq // args.gpus:<4}]"


def round_val(seq):
    if seq == NO_COLL or not nccl_hooked:
        return ""
    return seq // args.gpus


TYPE_NAME = {0: "SEND", 1: "RECV", 2: "CQ", 3: "NCCL"}


# ========== Helper functions ==========

def scan_all_maps():
    all_mappings = []
    nccl_path = None
    for maps_file in glob.glob("/proc/[0-9]*/maps"):
        try:
            with open(maps_file, "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 6:
                        continue
                    path = parts[5]
                    if not path.startswith("/"):
                        continue
                    if "x" in parts[1]:
                        addr_range = parts[0].split("-")
                        start  = int(addr_range[0], 16)
                        end    = int(addr_range[1], 16)
                        offset = int(parts[2], 16)
                        all_mappings.append((start, end, offset, path))
                    if nccl_path is None and "libnccl" in path and ".so" in path:
                        if os.path.exists(path):
                            nccl_path = path
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
    return all_mappings, nccl_path


def resolve_addr(addr, mappings):
    for start, end, offset, path in mappings:
        if start <= addr < end:
            return path, addr - start + offset
    return None, None


def check_symbol(lib_path, sym):
    try:
        out = subprocess.check_output(
            ["nm", "-D", "--defined-only", lib_path],
            stderr=subprocess.DEVNULL
        ).decode(errors="ignore")
        return sym in out
    except Exception:
        return False


# ========== Phase 1 ==========

print("[*] Phase 1: hook ibv_modify_qp and discover RDMA function addresses...")
b1 = BPF(text=discover_bpf)
b1.attach_uprobe(name=LIBIBVERBS, sym="ibv_modify_qp", fn_name="trace_modify_qp")

discovered = {}
found_nccl = {"path": args.nccl}


def handle_fn_addr(cpu, data, size):
    e = b1["fn_addrs"].event(data)
    print(f"\n[*] Captured ibv_modify_qp, host PID={e.pid}")
    print(f"    post_send = 0x{e.post_send_addr:x}")
    print(f"    post_recv = 0x{e.post_recv_addr:x}")
    print(f"    poll_cq   = 0x{e.poll_cq_addr:x}")

    print("[*] Scanning /proc/*/maps ...")
    mappings, auto_nccl = scan_all_maps()

    for name, addr in [("post_send", e.post_send_addr),
                       ("post_recv", e.post_recv_addr),
                       ("poll_cq",   e.poll_cq_addr)]:
        if addr == 0:
            print(f"    {name}: NULL, skipped"); continue
        path, offset = resolve_addr(addr, mappings)
        if path:
            print(f"    {name}: {path} + 0x{offset:x}")
            discovered[name] = (path, offset)
        else:
            print(f"    {name}: 0x{addr:x} could not be resolved")

    if found_nccl["path"] is None and auto_nccl:
        found_nccl["path"] = auto_nccl
        print(f"    libnccl: auto-discovered {auto_nccl}")
    elif found_nccl["path"]:
        print(f"    libnccl: specified by command line {found_nccl['path']}")
    else:
        print("    libnccl: not found; NCCL correlation will be skipped")


b1["fn_addrs"].open_perf_buffer(handle_fn_addr)
print("[*] Waiting for ibv_modify_qp ... (start the NCCL program)")

try:
    while not discovered:
        b1.perf_buffer_poll(timeout=1000)
except KeyboardInterrupt:
    if not discovered:
        print("\n[!] No addresses discovered, exiting"); exit(1)

b1.cleanup()
print(f"\n[*] Phase 1 complete, discovered {len(discovered)} RDMA functions")

# ========== Phase 2 ==========

print("[*] Phase 2: attach uprobes...")
b2 = BPF(text=trace_bpf)

fn_map = {
    "post_send": ("trace_post_send",     None),
    "post_recv": ("trace_post_recv",     None),
    "poll_cq":   ("trace_poll_cq_entry", "trace_poll_cq_return"),
}

for name, (path, offset) in discovered.items():
    entry_fn, ret_fn = fn_map.get(name, (None, None))
    if entry_fn:
        try:
            b2.attach_uprobe(name=path, addr=offset, fn_name=entry_fn)
            print(f"    [+] {name} uprobe  -> {path}+0x{offset:x}")
        except Exception as ex:
            print(f"    [-] {name} uprobe failed: {ex}")
    if ret_fn:
        try:
            b2.attach_uretprobe(name=path, addr=offset, fn_name=ret_fn)
            print(f"    [+] {name} uretprobe -> {path}+0x{offset:x}")
        except Exception as ex:
            print(f"    [-] {name} uretprobe failed: {ex}")

nccl_hooked = False
nccl_path   = found_nccl["path"]
if nccl_path and os.path.exists(nccl_path):
    if check_symbol(nccl_path, "ncclAllReduce"):
        try:
            b2.attach_uprobe(name=nccl_path, sym="ncclAllReduce",
                             fn_name="trace_nccl_allreduce")
            print(f"    [+] ncclAllReduce uprobe -> {nccl_path}")
            nccl_hooked = True
        except Exception as ex:
            print(f"    [-] ncclAllReduce uprobe failed: {ex}")
    else:
        print(f"    [!] ncclAllReduce symbol was not found in {nccl_path} (stripped?)")
else:
    print("    [!] libnccl.so not found; NCCL correlation skipped")
    print("    [hint] Use --nccl /path/to/libnccl.so.2 to specify the path manually")

if nccl_hooked:
    print(f"    [*] Logical round grouping: round = seq // {args.gpus}  (--gpus {args.gpus})")
if args.filter == "auto":
    print("    [*] auto filter: keep only RDMA_WRITE_WITH_IMM / post_recv / matching CQ")

# ========== Header ==========
hdr = "%-10s %-7s %-8s %-5s %-18s %-8s %-22s %-18s %-12s %s"
log("")
log(hdr % ("TIME(ms)", "ROUND", "TYPE", "QP", "OPCODE", "SIZE",
           "FLAGS/INFO", "WR_ID/SEQ", "LATENCY", "EXTRA"))
log("-" * 165)

start_ts      = 0
last_event_ts = 0
gap_ns        = args.gap_ms * 1e6

round_info = {}

count      = {"send": 0, "recv": 0, "cq_w": 0, "cq_r": 0}
auto_count = {"send_imm": 0, "recv": 0, "cq_w_imm": 0, "cq_recv_imm": 0}
lat_write  = []
lat_recv   = []


def _rdma_inc(r):
    if r is not None:
        if r not in round_info:
            round_info[r] = {"count": 0, "dtype": "?", "op": "?",
                             "rdma_n": 0, "seq_list": []}
        round_info[r]["rdma_n"] += 1


def _base_csv(e, ms, seq, r):
    """Build common fields for a CSV row."""
    return {
        "timestamp_ns": e.ts,
        "time_ms": f"{ms:.4f}",
        "pid": e.pid,
        "comm": e.comm.decode(errors="replace"),
        "type": TYPE_NAME.get(e.type, str(e.type)),
        "round": round_val(seq) if seq != NO_COLL else "",
        "nccl_seq": seq if seq != NO_COLL else "",
    }


def print_event(cpu, data, size):
    global start_ts, last_event_ts

    e = b2["events"].event(data)
    if start_ts == 0:
        start_ts = e.ts
    ms = (e.ts - start_ts) / 1e6

    if last_event_ts > 0 and not nccl_hooked:
        if (e.ts - last_event_ts) > gap_ns:
            log(f"{'-'*6} burst gap {(e.ts - last_event_ts)/1e6:.1f}ms {'-'*40}")
    last_event_ts = e.ts

    # ---- type=3: ncclAllReduce ENTER ----
    if e.type == 3:
        seq = e.nccl_seq
        r   = seq // args.gpus
        dtype_s = NCCL_DTYPE.get(e.nccl_datatype, f"dtype({e.nccl_datatype})")
        op_s    = NCCL_OP.get(e.nccl_op, f"op({e.nccl_op})")
        if r not in round_info:
            round_info[r] = {"count": e.nccl_count, "dtype": dtype_s,
                             "op": op_s, "rdma_n": 0, "seq_list": []}
        else:
            round_info[r]["count"] = e.nccl_count
            round_info[r]["dtype"] = dtype_s
            round_info[r]["op"]    = op_s
        round_info[r]["seq_list"].append(seq)
        rl = f"[R{r:<4}]"
        log(hdr % (f"{ms:.2f}", rl, "NCCL", "-", "AllReduce",
                   size_str(e.nccl_count), f"{dtype_s}/{op_s}",
                   f"seq={seq}", "-", f"pid={e.pid}"))
        # CSV
        row = _base_csv(e, ms, seq, r)
        row.update({
            "nccl_count": e.nccl_count,
            "nccl_datatype": e.nccl_datatype,
            "nccl_datatype_name": dtype_s,
            "nccl_op": e.nccl_op,
            "nccl_op_name": op_s,
        })
        csv_row(row)
        return

    # ---- RDMA events ----
    seq = e.nccl_seq
    r   = (seq // args.gpus) if seq != NO_COLL else None
    rl  = round_label(seq)

    if e.type == 0:  # post_send
        count["send"] += 1
        is_imm = (e.opcode == 1)
        if args.filter == "auto" and not is_imm:
            return
        if args.filter == "recv":
            return
        if args.filter == "write" and e.size <= 64:
            return
        if is_imm:
            auto_count["send_imm"] += 1
        op = SEND_OPCODE.get(e.opcode, f"OP({e.opcode})")
        fl = flags_str(e.send_flags, SEND_FLAGS)
        sz = size_str(e.size)
        extra = ""
        if e.opcode in (0, 1, 4):
            extra += f"raddr=0x{e.remote_addr:x} rkey=0x{e.rkey:x}"
        if e.opcode in (1, 3):
            extra += f" imm=0x{e.imm_data:x}"
        _rdma_inc(r)
        log(hdr % (f"{ms:.2f}", rl, "SEND", e.qp_num, op, sz, fl,
                   f"0x{e.wr_id:x}", "-", extra))
        # CSV
        row = _base_csv(e, ms, seq, r)
        row.update({
            "qp_num": e.qp_num, "opcode": e.opcode, "opcode_name": op,
            "send_flags": e.send_flags, "size_bytes": e.size,
            "num_sge": e.num_sge, "wr_id": f"0x{e.wr_id:x}",
            "remote_addr": f"0x{e.remote_addr:x}" if e.remote_addr else "",
            "rkey": f"0x{e.rkey:x}" if e.rkey else "",
            "imm_data": f"0x{e.imm_data:x}" if e.opcode in (1, 3) else "",
        })
        csv_row(row)

    elif e.type == 1:  # post_recv
        count["recv"] += 1
        auto_count["recv"] += 1
        if args.filter == "write":
            return
        sz = size_str(e.size)
        _rdma_inc(r)
        log(hdr % (f"{ms:.2f}", rl, "RECV", e.qp_num, "-", sz, "-",
                   f"0x{e.wr_id:x}", "-", ""))
        # CSV
        row = _base_csv(e, ms, seq, r)
        row.update({
            "qp_num": e.qp_num, "size_bytes": e.size,
            "num_sge": e.num_sge, "wr_id": f"0x{e.wr_id:x}",
        })
        csv_row(row)

    elif e.type == 2:  # CQ completion
        is_recv_cq = e.wc_opcode >= 128
        if is_recv_cq:
            count["cq_r"] += 1
            is_recv_imm = (e.wc_opcode == 129)
            if args.filter == "auto" and not is_recv_imm:
                return
            if args.filter == "write":
                return
            if args.with_imm and not (e.wc_flags & 2):
                return
            if is_recv_imm:
                auto_count["cq_recv_imm"] += 1
        else:
            count["cq_w"] += 1
            if args.filter == "auto" and not e.cq_from_imm:
                return
            if args.filter == "recv":
                return
            if args.with_imm and not (e.wc_flags & 2):
                return
            if e.cq_from_imm:
                auto_count["cq_w_imm"] += 1

        st  = WC_STATUS.get(e.wc_status, f"ST({e.wc_status})")
        op  = WC_OPCODE.get(e.wc_opcode, f"WCOP({e.wc_opcode})")
        fl  = flags_str(e.wc_flags, WC_FLAGS_MAP)
        sz  = size_str(e.wc_byte_len)
        lat = latency_str(e.latency_ns)

        if e.latency_ns > 0:
            (lat_recv if is_recv_cq else lat_write).append(e.latency_ns)

        extra = f"status={st}"
        if e.wc_flags & 2:
            extra += f" imm=0x{e.wc_imm_data:x}"
        if e.wc_src_qp:
            extra += f" src_qp={e.wc_src_qp}"
        _rdma_inc(r)
        log(hdr % (f"{ms:.2f}", rl, "CQ", e.wc_qp_num, op, sz, fl,
                   f"0x{e.wr_id:x}", lat, extra))
        # CSV
        row = _base_csv(e, ms, seq, r)
        row.update({
            "qp_num": e.wc_qp_num, "wr_id": f"0x{e.wr_id:x}",
            "wc_status": e.wc_status, "wc_status_name": st,
            "wc_opcode": e.wc_opcode, "wc_opcode_name": op,
            "wc_byte_len": e.wc_byte_len,
            "wc_imm_data": f"0x{e.wc_imm_data:x}" if (e.wc_flags & 2) else "",
            "wc_qp_num": e.wc_qp_num, "wc_src_qp": e.wc_src_qp or "",
            "wc_flags": e.wc_flags, "latency_ns": e.latency_ns or "",
            "cq_from_imm": e.cq_from_imm,
        })
        csv_row(row)


b2["events"].open_perf_buffer(print_event, page_cnt=64)
print("[*] Tracing started. Press Ctrl-C to stop.\n")

try:
    while True:
        b2.perf_buffer_poll()
except KeyboardInterrupt:
    pass

# ========== Statistics ==========

log(f"\n{'='*60}")
log(f"[*] Raw RDMA event counts:")
log(f"    SEND={count['send']}  RECV={count['recv']}  "
    f"CQ_write={count['cq_w']}  CQ_recv={count['cq_r']}")

if args.filter == "auto":
    log(f"\n[*] Valid auto-mode events (the four counts should match):")
    log(f"    SEND_IMM={auto_count['send_imm']}  RECV={auto_count['recv']}  "
        f"CQ_W_IMM={auto_count['cq_w_imm']}  CQ_RECV_IMM={auto_count['cq_recv_imm']}")

if round_info:
    n_rounds = len(round_info)
    n_seqs   = sum(len(ri["seq_list"]) for ri in round_info.values())
    log(f"\n[*] Collective summary ({n_rounds} rounds, {n_seqs} ncclAllReduce calls, "
        f"--gpus={args.gpus}):")
    log(f"    {'Round':<7} {'seqs':<14} {'count':<12} {'dtype':<10} {'op':<6} {'shown_events'}")
    log(f"    {'-'*64}")
    for r in sorted(round_info):
        ri = round_info[r]
        seqs_s = "+".join(f"#{s}" for s in ri["seq_list"])
        log(f"    R{r:<6} {seqs_s:<14} {ri['count']:<12} {ri['dtype']:<10} "
            f"{ri['op']:<6} {ri['rdma_n']}")
elif not nccl_hooked:
    log("\n[*] NCCL was not hooked; no collective summary")


def print_lat_stats(name, lats):
    if not lats:
        log(f"\n[*] {name} latency: no matched pairs"); return
    lats.sort()
    avg = sum(lats) / len(lats)
    p50 = lats[len(lats) // 2]
    p99 = lats[int(len(lats) * 0.99)]
    log(f"\n[*] {name} latency ({len(lats)} pairs):")
    log(f"    avg={latency_str(avg)}  p50={latency_str(p50)}  "
        f"p99={latency_str(p99)}  min={latency_str(lats[0])}  "
        f"max={latency_str(lats[-1])}")


print_lat_stats("send side (post_send -> CQ)", lat_write)
print_lat_stats("receive side (post_recv -> CQ)", lat_recv)

if csv_fp:
    csv_fp.close()
    print(f"[*] CSV data saved to {args.csv_file}")
if outfile:
    outfile.close()
    print(f"[*] Log saved to {args.output}")
print("Done.")

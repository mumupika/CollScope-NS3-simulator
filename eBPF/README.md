# eBPF Directory

This directory holds the eBPF tool scripts.

```txt
.
|-- README.md           
|-- check_offsets.sh        # Hooks RDMA/NCCL events.
`-- nccl-hook.py            # Hooks NCCL collective entry/exit points.
```

## Usage

```bash
sudo python3 rdma_hook.py --filter auto --gpus 1 --csv rdma.csv --output rdma.log
sudo python3 nccl_hook.py --name python3 --nproc 4 --debug
```

Requirements: Linux, BCC/eBPF, root privileges, libibverbs, and a running NCCL workload.

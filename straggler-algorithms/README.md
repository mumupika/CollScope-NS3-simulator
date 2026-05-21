# Straggler Algorithms

This directory is the detailed algorithms implemented in CollScope.

```txt
.
|-- README.md           
|-- step_detector.py                # Convert NCCL trace files into step durations.
|-- changepoint_detector.py         # Detect duration shifts.
|-- diagnose.py                     # Input NCCL trace files, detect anomaly and label them as computation slowdown or network/other cause.
`-- backprojection.py.py            # Ranks likely congested links from topology and flow completion time data.
```

## Usage

```bash
python3 backprojection.py --root <experiment_root>
python3 step_detector.py <trace_dir> -o <trace_dir>/output.csv
python3 changepoint_detector.py <trace_dir>/output.csv
python3 diagnose.py <trace_dir> -o diagnosis.png
```
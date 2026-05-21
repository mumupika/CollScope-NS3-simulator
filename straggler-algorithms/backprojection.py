#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DirectedEdge = tuple[int, int]
UndirectedEdge = tuple[int, int]


@dataclass(frozen=True)
class Topology:
    directed_edges: set[DirectedEdge]
    edge_delay_ns: dict[DirectedEdge, float]


@dataclass(frozen=True)
class Flow:
    line_no: int
    sid: int
    did: int
    size: int
    start_time: int
    fct: int
    path_nodes: tuple[int, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compute simple backprojection top-k congested-link scores for "
            "experiment folders. The reference topology defaults to "
            "normal/topology.txt, and each flow uses the routed switch path "
            "recorded in the experiment's out/fct.txt."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Experiment root directory. Default: current working directory.",
    )
    parser.add_argument(
        "--reference-topology",
        type=Path,
        default=None,
        help="Reference topology file. Default: ROOT/normal/topology.txt.",
    )
    parser.add_argument(
        "--experiments",
        nargs="+",
        default=["experiment/"+str(i+1) for i in range(30)],
        help="Experiment folders to analyze. Default: experiment/1 through experiment/30.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("backprojection_results"),
        help=(
            "Output root. A subfolder is created for each experiment. "
            "Default: ROOT/backprojection_results."
        ),
    )
    parser.add_argument(
        "--result-dir",
        type=Path,
        default=Path("result"),
        help=(
            "Extra output folder containing only normalized scores divided by "
            "observation_count. Files are named after each experiment folder. "
            "Default: ROOT/result."
        ),
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=100,
        help="Number of ranked links to write per output file. Default: 100.",
    )
    parser.add_argument(
        "--min-delta",
        type=float,
        default=1.0,
        help="Only use flows with residual delay >= this value in ns. Default: 1.",
    )
    return parser.parse_args()


def parse_delay_ns(text: str) -> float:
    units = {
        "ns": 1.0,
        "us": 1e3,
        "ms": 1e6,
        "s": 1e9,
    }
    lowered = text.strip().lower()
    for suffix, multiplier in sorted(units.items(), key=lambda item: -len(item[0])):
        if lowered.endswith(suffix):
            return float(lowered[: -len(suffix)]) * multiplier
    return float(lowered)


def parse_topology(path: str | Path) -> Topology:
    numeric_lines: list[str] = []
    with open(path, "r", encoding="utf-8") as input_file:
        for raw_line in input_file:
            stripped = raw_line.strip()
            if not stripped:
                continue
            first = stripped.split()[0]
            if first.lstrip("-").isdigit():
                numeric_lines.append(stripped)

    if len(numeric_lines) < 2:
        raise ValueError(f"{path}: topology header is missing")

    _node_count, _switch_count, _link_count = map(int, numeric_lines[0].split()[:3])

    directed_edges: set[DirectedEdge] = set()
    edge_delay_ns: dict[DirectedEdge, float] = {}
    for line in numeric_lines[2:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        src = int(parts[0])
        dst = int(parts[1])
        directed_edges.add((src, dst))
        directed_edges.add((dst, src))
        if len(parts) >= 4:
            delay_ns = parse_delay_ns(parts[3])
            edge_delay_ns[(src, dst)] = delay_ns
            edge_delay_ns[(dst, src)] = delay_ns

    return Topology(
        directed_edges=directed_edges,
        edge_delay_ns=edge_delay_ns,
    )


def parse_flow_header(path: str | Path, line_no: int, raw_line: str) -> tuple[int, int, int, int, int]:
    stripped = raw_line.strip()
    if stripped.startswith("###"):
        stripped = stripped[3:].strip()

    parts = stripped.replace(",", " ").split()
    try:
        values = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"{path}:{line_no}: non-integer flow header") from exc

    if len(values) < 6:
        raise ValueError(f"{path}:{line_no}: unsupported flow header column count {len(values)}")

    sid = values[0]
    did = values[1]
    size = values[-4]
    start_time = values[-3]
    fct = values[-2]
    return sid, did, size, start_time, fct


def parse_switch_path(path: str | Path, line_no: int, raw_line: str) -> tuple[int, ...]:
    parts = raw_line.strip().replace(",", " ").split()
    try:
        return tuple(int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"{path}:{line_no}: non-integer routed switch id") from exc


def combine_path_nodes(sid: int, switch_path: Sequence[int], did: int) -> tuple[int, ...]:
    nodes = [sid, *switch_path, did]
    deduped: list[int] = []
    for node in nodes:
        if not deduped or deduped[-1] != node:
            deduped.append(node)
    return tuple(deduped)


def parse_fct(path: str | Path) -> list[Flow]:
    flows: list[Flow] = []
    with open(path, "r", encoding="utf-8") as input_file:
        lines = input_file.readlines()

    index = 0
    while index < len(lines):
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines):
            break

        flow_line_no = index + 1
        sid, did, size, start_time, fct = parse_flow_header(path, flow_line_no, lines[index])
        index += 1

        if index >= len(lines):
            raise ValueError(f"{path}:{flow_line_no}: missing Routed Switch Ids line")
        route_label_line_no = index + 1
        route_label = lines[index].strip()
        if not route_label.startswith("Routed Switch Ids"):
            raise ValueError(f"{path}:{route_label_line_no}: expected 'Routed Switch Ids:'")
        index += 1

        if index >= len(lines):
            raise ValueError(f"{path}:{flow_line_no}: missing routed switch path")
        switch_line_no = index + 1
        switch_path = parse_switch_path(path, switch_line_no, lines[index])
        index += 1

        if index < len(lines) and not lines[index].strip():
            index += 1

        flows.append(
            Flow(
                line_no=flow_line_no,
                sid=sid,
                did=did,
                size=size,
                start_time=start_time,
                fct=fct,
                path_nodes=combine_path_nodes(sid, switch_path, did),
            )
        )

    return flows


def path_edges(path: Sequence[int]) -> tuple[DirectedEdge, ...]:
    return tuple(zip(path, path[1:]))


def link_delay_baseline_ns(topology: Topology, edges: Sequence[DirectedEdge]) -> float:
    return sum(topology.edge_delay_ns[edge] for edge in edges)


def compute_backprojection(
    topology: Topology,
    flows: Sequence[Flow],
    min_delta: float,
    normalize_size: bool,
) -> tuple[dict[DirectedEdge, dict[str, float]], dict[str, int]]:
    scores: dict[DirectedEdge, dict[str, float]] = {
        edge: {"score": 0.0, "count": 0.0, "max_delta": 0.0}
        for edge in topology.directed_edges
    }
    stats = {
        "parsed_flows": len(flows),
        "used_flows": 0,
        "below_min_delta": 0,
        "missing_routes": 0,
        "missing_path_edges": 0,
    }

    for flow in flows:
        edges = path_edges(flow.path_nodes)
        if not edges:
            stats["missing_routes"] += 1
            continue

        if any(edge not in topology.edge_delay_ns for edge in edges):
            stats["missing_path_edges"] += 1
            continue

        delay_delta = float(flow.fct) - link_delay_baseline_ns(topology, edges)
        if delay_delta < min_delta:
            stats["below_min_delta"] += 1
            continue

        projected_value = delay_delta / flow.size if normalize_size and flow.size > 0 else delay_delta
        share = projected_value / len(edges)
        for edge in edges:
            if edge not in scores:
                scores[edge] = {"score": 0.0, "count": 0.0, "max_delta": 0.0}
            scores[edge]["score"] += share
            scores[edge]["count"] += 1.0
            scores[edge]["max_delta"] = max(scores[edge]["max_delta"], float(delay_delta))
        stats["used_flows"] += 1

    return scores, stats


def write_scores(
    path: str | Path,
    scores: dict[DirectedEdge, dict[str, float]],
    top_k: int,
    divide_observation_count: bool,
) -> None:
    undirected_scores: dict[UndirectedEdge, dict[str, float]] = {}
    for edge, row in scores.items():
        undirected_edge = tuple(sorted(edge))
        if undirected_edge not in undirected_scores:
            undirected_scores[undirected_edge] = {"score": 0.0, "count": 0.0, "max_delta": 0.0}
        undirected_scores[undirected_edge]["score"] += row["score"]
        undirected_scores[undirected_edge]["count"] += row["count"]
        undirected_scores[undirected_edge]["max_delta"] = max(
            undirected_scores[undirected_edge]["max_delta"],
            row["max_delta"],
        )

    def output_score(edge: UndirectedEdge) -> float:
        row = undirected_scores[edge]
        if not divide_observation_count:
            return row["score"]
        if row["count"] <= 0:
            return 0.0
        return row["score"] / row["count"]

    with open(path, "w", newline="", encoding="utf-8") as output_handle:
        writer = csv.DictWriter(
            output_handle,
            fieldnames=[
                "rank_backprojection",
                "link",
                "backprojection_score",
                "observation_count",
                "max_observed_delta",
            ],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()

        sorted_edges = sorted(
            undirected_scores,
            key=lambda edge: (-output_score(edge), edge[0], edge[1]),
        )
        for rank, edge in enumerate(sorted_edges[:top_k], start=1):
            row = undirected_scores[edge]
            writer.writerow(
                {
                    "rank_backprojection": rank,
                    "link": f"{edge[0]}<->{edge[1]}",
                    "backprojection_score": f"{output_score(edge):.6f}",
                    "observation_count": int(row["count"]),
                    "max_observed_delta": int(row["max_delta"]),
                }
            )


def resolve_under_root(root: Path, path: Path) -> Path:
    if path.is_absolute():
        return path
    return root / path


def main() -> int:
    args = parse_args()
    root = args.root
    topology_path = args.reference_topology or Path("normal/topology.txt")
    topology = parse_topology(resolve_under_root(root, topology_path))
    output_root = resolve_under_root(root, args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    result_root = resolve_under_root(root, args.result_dir)
    result_root.mkdir(parents=True, exist_ok=True)

    for experiment in args.experiments:
        fct_path = root / experiment / "out" / "fct.txt"
        flows = parse_fct(fct_path)

        experiment_output_dir = output_root / experiment
        experiment_output_dir.mkdir(parents=True, exist_ok=True)

        raw_scores, raw_stats = compute_backprojection(
            topology=topology,
            flows=flows,
            min_delta=args.min_delta,
            normalize_size=False,
        )
        raw_output = experiment_output_dir / "top100_not_normalized.txt"
        write_scores(
            raw_output,
            raw_scores,
            args.top_k,
            divide_observation_count=False,
        )
        raw_div_count_output = experiment_output_dir / "top100_not_normalized_div_observation_count.txt"
        write_scores(
            raw_div_count_output,
            raw_scores,
            args.top_k,
            divide_observation_count=True,
        )

        normalized_scores, normalized_stats = compute_backprojection(
            topology=topology,
            flows=flows,
            min_delta=args.min_delta,
            normalize_size=True,
        )
        normalized_output = experiment_output_dir / "top100_normalized.txt"
        write_scores(
            normalized_output,
            normalized_scores,
            args.top_k,
            divide_observation_count=False,
        )
        normalized_div_count_output = experiment_output_dir / (
            "top100_normalized_div_observation_count.txt"
        )
        write_scores(
            normalized_div_count_output,
            normalized_scores,
            args.top_k,
            divide_observation_count=True,
        )
        result_output = result_root / experiment
        write_scores(
            result_output,
            normalized_scores,
            args.top_k,
            divide_observation_count=True,
        )

        print(
            f"{experiment} "
            f"not_normalized={raw_output} "
            f"not_normalized_div_observation_count={raw_div_count_output} "
            f"normalized={normalized_output} "
            f"normalized_div_observation_count={normalized_div_count_output} "
            f"result={result_output} "
            f"stats_not_normalized="
            f"{' '.join(f'{key}={value}' for key, value in raw_stats.items())} "
            f"stats_normalized="
            f"{' '.join(f'{key}={value}' for key, value in normalized_stats.items())}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

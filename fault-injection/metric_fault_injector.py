import argparse
import csv
import math
import random
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request


app = Flask(__name__)

NAMESPACE = "teastore"
DEFAULT_DEPLOYMENT = "teastore-webui"
DEFAULT_LABEL_SELECTOR = "app=teastore-webui"

DEFAULT_MIN_REPLICAS = 1
DEFAULT_MAX_REPLICAS = 1000
DEFAULT_TARGET_CPU_MILLICORES = 300.0
DEFAULT_TARGET_MEMORY_MI = 512.0

CSV_FIELDS = [
    "timestamp",
    "scenario",
    "deployment",
    "label_selector",
    "fault_type",
    "pod_count",
    "current_replicas",
    "real_cpu_m",
    "faulty_cpu_m",
    "real_memory_mi",
    "faulty_memory_mi",
    "desired_replicas_cpu_clean",
    "desired_replicas_cpu_faulty",
    "desired_replicas_memory_clean",
    "desired_replicas_memory_faulty",
]


def run_kubectl(args):
    cmd = ["kubectl", *args]
    return subprocess.check_output(cmd, text=True).strip()


def parse_cpu_millicores(value):
    if value.endswith("n"):
        return float(value[:-1]) / 1_000_000.0
    if value.endswith("u"):
        return float(value[:-1]) / 1_000.0
    if value.endswith("m"):
        return float(value[:-1])
    return float(value) * 1000.0


def parse_memory_mi(value):
    units = {
        "Ki": 1 / 1024,
        "Mi": 1,
        "Gi": 1024,
        "Ti": 1024 * 1024,
        "K": 1 / 1000,
        "M": 1,
        "G": 1000,
        "T": 1000 * 1000,
    }

    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier

    return float(value) / (1024 * 1024)


def get_current_replicas(args):
    output = run_kubectl([
        "get",
        "deployment",
        args.deployment,
        "-n",
        NAMESPACE,
        "-o",
        "jsonpath={.status.replicas}",
    ])
    return int(output or "0")


def get_pod_metrics(args):
    output = run_kubectl([
        "top",
        "pods",
        "-n",
        NAMESPACE,
        "-l",
        args.label_selector,
        "--no-headers",
    ])

    rows = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue

        rows.append({
            "pod": parts[0],
            "cpu_m": parse_cpu_millicores(parts[1]),
            "memory_mi": parse_memory_mi(parts[2]),
        })

    if not rows:
        raise RuntimeError(f"No pod metrics found for {args.label_selector}.")

    pod_count = len(rows)
    total_cpu_m = sum(row["cpu_m"] for row in rows)
    total_memory_mi = sum(row["memory_mi"] for row in rows)

    return {
        "pod_count": pod_count,
        "avg_cpu_m": total_cpu_m / pod_count,
        "avg_memory_mi": total_memory_mi / pod_count,
        "total_cpu_m": total_cpu_m,
        "total_memory_mi": total_memory_mi,
        "pods": rows,
    }


def apply_fault(cpu_m, memory_mi, scenario, fault_rate):
    if scenario == "baseline":
        return cpu_m, memory_mi, "none"

    if scenario == "random-multiplier":
        multiplier = random.uniform(0.0, 100.0)
        return (
            cpu_m * multiplier,
            memory_mi * multiplier,
            f"random_multiplier_{multiplier:.3f}",
        )

    if scenario == "random" and random.random() >= fault_rate:
        return cpu_m, memory_mi, "none"

    if scenario == "random":
        scenario = random.choice([
            "cpu-spike",
            "cpu-drop",
            "memory-spike",
            "memory-drop",
        ])

    if scenario == "cpu-spike":
        return cpu_m * 3.0, memory_mi, "cpu_spike"
    if scenario == "cpu-drop":
        return cpu_m * 0.3, memory_mi, "cpu_drop"
    if scenario == "memory-spike":
        return cpu_m, memory_mi * 2.0, "memory_spike"
    if scenario == "memory-drop":
        return cpu_m, memory_mi * 0.5, "memory_drop"

    raise ValueError(f"Unknown scenario: {scenario}")


def estimate_desired_replicas(current_replicas, observed_value, target_value, min_replicas, max_replicas):
    if current_replicas <= 0 or target_value <= 0:
        return min_replicas

    desired = math.ceil(current_replicas * observed_value / target_value)
    return max(min_replicas, min(max_replicas, desired))


def collect_sample(args):
    metrics = get_pod_metrics(args)
    current_replicas = get_current_replicas(args)
    real_cpu_m = metrics["avg_cpu_m"]
    real_memory_mi = metrics["avg_memory_mi"]
    faulty_cpu_m, faulty_memory_mi, fault_type = apply_fault(
        real_cpu_m,
        real_memory_mi,
        args.scenario,
        args.fault_rate,
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scenario": args.scenario,
        "deployment": args.deployment,
        "label_selector": args.label_selector,
        "fault_type": fault_type,
        "pod_count": metrics["pod_count"],
        "current_replicas": current_replicas,
        "real_cpu_m": round(real_cpu_m, 3),
        "faulty_cpu_m": round(faulty_cpu_m, 3),
        "real_memory_mi": round(real_memory_mi, 3),
        "faulty_memory_mi": round(faulty_memory_mi, 3),
        "desired_replicas_cpu_clean": estimate_desired_replicas(
            current_replicas,
            real_cpu_m,
            args.target_cpu_m,
            args.min_replicas,
            args.max_replicas,
        ),
        "desired_replicas_cpu_faulty": estimate_desired_replicas(
            current_replicas,
            faulty_cpu_m,
            args.target_cpu_m,
            args.min_replicas,
            args.max_replicas,
        ),
        "desired_replicas_memory_clean": estimate_desired_replicas(
            current_replicas,
            real_memory_mi,
            args.target_memory_mi,
            args.min_replicas,
            args.max_replicas,
        ),
        "desired_replicas_memory_faulty": estimate_desired_replicas(
            current_replicas,
            faulty_memory_mi,
            args.target_memory_mi,
            args.min_replicas,
            args.max_replicas,
        ),
    }


def append_csv(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    should_write_header = not path.exists()

    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        if should_write_header:
            writer.writeheader()
        writer.writerow(row)


def collect_loop(args):
    deadline = None
    if args.duration > 0:
        deadline = time.monotonic() + args.duration

    while deadline is None or time.monotonic() < deadline:
        row = collect_sample(args)
        append_csv(args.output, row)
        print(row, flush=True)
        time.sleep(args.interval)


@app.route("/metric")
def metric():
    args = build_args([
        "serve",
        "--scenario",
        request.args.get("scenario", "random"),
        "--fault-rate",
        request.args.get("fault_rate", "0.2"),
    ])
    row = collect_sample(args)

    return jsonify(row)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Collect Kubernetes pod metrics and simulate autoscaling faults.",
    )
    subparsers = parser.add_subparsers(dest="command")

    for command in ("serve", "collect"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument(
            "--scenario",
            choices=[
                "baseline",
                "cpu-spike",
                "cpu-drop",
                "memory-spike",
                "memory-drop",
                "random",
                "random-multiplier",
            ],
            default="random",
        )
        subparser.add_argument("--deployment", default=DEFAULT_DEPLOYMENT)
        subparser.add_argument("--label-selector", default=DEFAULT_LABEL_SELECTOR)
        subparser.add_argument("--fault-rate", type=float, default=0.2)
        subparser.add_argument("--target-cpu-m", type=float, default=DEFAULT_TARGET_CPU_MILLICORES)
        subparser.add_argument("--target-memory-mi", type=float, default=DEFAULT_TARGET_MEMORY_MI)
        subparser.add_argument("--min-replicas", type=int, default=DEFAULT_MIN_REPLICAS)
        subparser.add_argument("--max-replicas", type=int, default=DEFAULT_MAX_REPLICAS)

    collect_parser = subparsers.choices["collect"]
    collect_parser.add_argument("--interval", type=float, default=15.0)
    collect_parser.add_argument("--duration", type=float, default=300.0)
    collect_parser.add_argument("--output", default="results/fault-injection.csv")

    serve_parser = subparsers.choices["serve"]
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=5001)

    return parser


def build_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["serve"])
    return args


if __name__ == "__main__":
    cli_args = build_args()
    if cli_args.command == "collect":
        collect_loop(cli_args)
    else:
        app.run(host=cli_args.host, port=cli_args.port)

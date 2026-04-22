import argparse
import csv
import math
import random
import statistics
import subprocess
import time
from collections import deque
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

# Mitigation filter defaults
DEFAULT_WINDOW_SIZE = 5        # number of samples in the sliding window
DEFAULT_ZSCORE_THRESHOLD = 2.0 # samples beyond this many std-devs are rejected

# VPA simulation defaults
# Safety margin approximates VPA's 90th-percentile histogram target with headroom
DEFAULT_VPA_SAFETY_MARGIN = 1.15
DEFAULT_VPA_MIN_CPU_M = 100.0
DEFAULT_VPA_MAX_CPU_M = 2000.0
DEFAULT_VPA_MIN_MEMORY_MI = 128.0
DEFAULT_VPA_MAX_MEMORY_MI = 1024.0

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
    # VPA simulation columns
    "vpa_cpu_rec_clean_m",
    "vpa_cpu_rec_faulty_m",
    "vpa_memory_rec_clean_mi",
    "vpa_memory_rec_faulty_mi",
    "vpa_cpu_risk",
    "vpa_memory_risk",
    # Mitigation columns — windowed median after z-score outlier rejection
    "cpu_outlier_rejected",
    "memory_outlier_rejected",
    "effective_cpu_m",
    "effective_memory_mi",
    "desired_replicas_cpu_mitigated",
    "desired_replicas_memory_mitigated",
    "vpa_cpu_rec_mitigated_m",
    "vpa_memory_rec_mitigated_mi",
    "vpa_cpu_risk_mitigated",
    "vpa_memory_risk_mitigated",
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


def estimate_vpa_recommendation(observed_value, safety_margin, min_allowed, max_allowed):
    """Simulate VPA Recommender: target = observed * safety_margin, clamped to policy bounds."""
    recommended = observed_value * safety_margin
    return round(max(min_allowed, min(max_allowed, recommended)), 3)


def vpa_risk(real_value, faulty_recommendation):
    """
    Classify the reliability/availability risk introduced by a faulty VPA recommendation.

    under_provisioned: faulty rec < real usage  → pod may be throttled or OOM-killed (reliability risk)
    over_provisioned:  faulty rec > real usage * safety headroom → wastes node capacity,
                       may prevent scheduling if node lacks headroom (availability risk)
    accurate:          faulty rec is within normal headroom of real usage
    """
    if faulty_recommendation < real_value:
        return "under_provisioned"
    if faulty_recommendation > real_value * 1.5:
        return "over_provisioned"
    return "accurate"


class MetricFilter:
    """
    Two-layer transient fault mitigation applied to the faulty metric stream:

    Layer 1 — Z-score outlier rejection:
        If the incoming sample deviates more than `zscore_threshold` standard
        deviations from the current window mean, it is treated as a transient
        fault and replaced with the rolling mean before being added to the window.

    Layer 2 — Windowed median:
        The effective metric value reported to HPA/VPA simulation is the median
        of the last `window_size` accepted samples, which absorbs residual noise
        that slipped past the z-score filter.
    """

    def __init__(self, window_size, zscore_threshold):
        self.zscore_threshold = zscore_threshold
        self._cpu: deque = deque(maxlen=window_size)
        self._memory: deque = deque(maxlen=window_size)

    def update(self, faulty_cpu_m, faulty_memory_mi):
        """
        Feed one faulty sample into the filter.
        Returns (effective_cpu_m, effective_memory_mi, cpu_rejected, memory_rejected).
        """
        cpu_accepted, cpu_rejected = self._accept(faulty_cpu_m, self._cpu)
        self._cpu.append(cpu_accepted)

        mem_accepted, mem_rejected = self._accept(faulty_memory_mi, self._memory)
        self._memory.append(mem_accepted)

        return (
            round(statistics.median(self._cpu), 3),
            round(statistics.median(self._memory), 3),
            cpu_rejected,
            mem_rejected,
        )

    def _accept(self, value, window):
        """Return (accepted_value, was_rejected). Rejected samples are replaced with the rolling mean."""
        if len(window) < 2:
            return value, False
        mean = statistics.mean(window)
        stdev = statistics.stdev(window)
        if stdev > 0 and abs(value - mean) / stdev > self.zscore_threshold:
            return mean, True
        return value, False


# Module-level filter used by the Flask serve handler (one persistent window per process).
_serve_filter: MetricFilter | None = None


def _get_serve_filter(args):
    global _serve_filter
    if _serve_filter is None:
        _serve_filter = MetricFilter(args.window_size, args.zscore_threshold)
    return _serve_filter


def collect_sample(args, metric_filter):
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

    effective_cpu_m, effective_memory_mi, cpu_rejected, mem_rejected = metric_filter.update(
        faulty_cpu_m, faulty_memory_mi,
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
        # VPA simulation: what resource requests would VPA set under clean vs faulty metrics?
        "vpa_cpu_rec_clean_m": estimate_vpa_recommendation(
            real_cpu_m, args.vpa_safety_margin, args.vpa_min_cpu_m, args.vpa_max_cpu_m,
        ),
        "vpa_cpu_rec_faulty_m": estimate_vpa_recommendation(
            faulty_cpu_m, args.vpa_safety_margin, args.vpa_min_cpu_m, args.vpa_max_cpu_m,
        ),
        "vpa_memory_rec_clean_mi": estimate_vpa_recommendation(
            real_memory_mi, args.vpa_safety_margin, args.vpa_min_memory_mi, args.vpa_max_memory_mi,
        ),
        "vpa_memory_rec_faulty_mi": estimate_vpa_recommendation(
            faulty_memory_mi, args.vpa_safety_margin, args.vpa_min_memory_mi, args.vpa_max_memory_mi,
        ),
        "vpa_cpu_risk": vpa_risk(real_cpu_m, estimate_vpa_recommendation(
            faulty_cpu_m, args.vpa_safety_margin, args.vpa_min_cpu_m, args.vpa_max_cpu_m,
        )),
        "vpa_memory_risk": vpa_risk(real_memory_mi, estimate_vpa_recommendation(
            faulty_memory_mi, args.vpa_safety_margin, args.vpa_min_memory_mi, args.vpa_max_memory_mi,
        )),
        # Mitigation outcomes — compare these against the faulty columns to measure effectiveness
        "cpu_outlier_rejected": cpu_rejected,
        "memory_outlier_rejected": mem_rejected,
        "effective_cpu_m": effective_cpu_m,
        "effective_memory_mi": effective_memory_mi,
        "desired_replicas_cpu_mitigated": estimate_desired_replicas(
            current_replicas,
            effective_cpu_m,
            args.target_cpu_m,
            args.min_replicas,
            args.max_replicas,
        ),
        "desired_replicas_memory_mitigated": estimate_desired_replicas(
            current_replicas,
            effective_memory_mi,
            args.target_memory_mi,
            args.min_replicas,
            args.max_replicas,
        ),
        "vpa_cpu_rec_mitigated_m": estimate_vpa_recommendation(
            effective_cpu_m, args.vpa_safety_margin, args.vpa_min_cpu_m, args.vpa_max_cpu_m,
        ),
        "vpa_memory_rec_mitigated_mi": estimate_vpa_recommendation(
            effective_memory_mi, args.vpa_safety_margin, args.vpa_min_memory_mi, args.vpa_max_memory_mi,
        ),
        "vpa_cpu_risk_mitigated": vpa_risk(real_cpu_m, estimate_vpa_recommendation(
            effective_cpu_m, args.vpa_safety_margin, args.vpa_min_cpu_m, args.vpa_max_cpu_m,
        )),
        "vpa_memory_risk_mitigated": vpa_risk(real_memory_mi, estimate_vpa_recommendation(
            effective_memory_mi, args.vpa_safety_margin, args.vpa_min_memory_mi, args.vpa_max_memory_mi,
        )),
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
    metric_filter = MetricFilter(args.window_size, args.zscore_threshold)
    deadline = None
    if args.duration > 0:
        deadline = time.monotonic() + args.duration

    while deadline is None or time.monotonic() < deadline:
        row = collect_sample(args, metric_filter)
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
    row = collect_sample(args, _get_serve_filter(args))
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
        subparser.add_argument("--vpa-safety-margin", type=float, default=DEFAULT_VPA_SAFETY_MARGIN)
        subparser.add_argument("--vpa-min-cpu-m", type=float, default=DEFAULT_VPA_MIN_CPU_M)
        subparser.add_argument("--vpa-max-cpu-m", type=float, default=DEFAULT_VPA_MAX_CPU_M)
        subparser.add_argument("--vpa-min-memory-mi", type=float, default=DEFAULT_VPA_MIN_MEMORY_MI)
        subparser.add_argument("--vpa-max-memory-mi", type=float, default=DEFAULT_VPA_MAX_MEMORY_MI)
        subparser.add_argument("--window-size", type=int, default=DEFAULT_WINDOW_SIZE)
        subparser.add_argument("--zscore-threshold", type=float, default=DEFAULT_ZSCORE_THRESHOLD)

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

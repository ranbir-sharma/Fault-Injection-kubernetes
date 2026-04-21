# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Kubernetes Autoscaling Fault Injection Testbed** (CPEN 533 course project). It studies how corrupted CPU/memory metrics affect Kubernetes Horizontal Pod Autoscaler (HPA) scaling decisions. Fault injection is **simulated in Python post-processing** — it does not inject faults into the live Kubernetes control loop.

## Setup

```bash
pip install -r requirements.txt       # locust, flask, kubernetes
```

Requires: Docker Desktop with Kubernetes enabled, `kubectl` configured to `docker-desktop` context.

Install metrics-server (one-time):
```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deployment metrics-server -n kube-system \
  --patch-file k8s/metrics-server-patch.yaml
kubectl rollout restart deployment metrics-server -n kube-system


kubectl apply -f k8s/metrics-server.yaml
kubectl patch deployment metrics-server -n kube-system \
  --patch-file k8s/metrics-server-patch.yaml
```

## Common Commands

**Deploy the TeaStore service and HPA:**
```bash
./run.sh
# or manually:
kubectl apply -f k8s/teastore.yaml
kubectl apply -f k8s/recommender-hpa.yaml        # CPU-based HPA
# kubectl apply -f k8s/recommender-hpa-memory.yaml  # Memory-based HPA
```

**Run an experiment (collect metrics with fault injection simulation):**
```bash
python fault-injection/metric_fault_injector.py collect \
  --scenario cpu-spike \
  --duration 600 \
  --interval 15 \
  --output results/cpu-spike.csv
```
Valid `--scenario` values: `baseline`, `cpu-spike`, `cpu-drop`, `memory-spike`, `memory-drop`, `random`

**Generate load:**
```bash
cd load && locust
# Then open http://localhost:8089 — configure 50-200 users, 5/sec spawn rate
# Target host: http://localhost:$(kubectl get svc -n teastore teastore-recommender -o jsonpath='{.spec.ports[0].nodePort}')
```

**Monitor HPA and pods:**
```bash
kubectl get hpa -n teastore -w
kubectl get pods -n teastore -w
kubectl top pods -n teastore
```

**Run Flask serve mode** (exposes `/metric` endpoint on port 5001):
```bash
python fault-injection/metric_fault_injector.py serve
```

**Teardown:**
```bash
kubectl delete -f k8s/
```

## Architecture

```
Locust Load Generator
    ↓ HTTP requests
TeaStore Recommender Pod  (k8s/teastore.yaml: 500m-1000m CPU, 256-512Mi memory)
    ↓ real metrics
Kubernetes metrics-server
    ↓
Live HPA  (k8s/recommender-hpa.yaml: 1-10 replicas, 60% CPU target)

PARALLEL — Python collector (fault-injection/metric_fault_injector.py):
    ├── reads real metrics via `kubectl top pods`
    ├── applies fault multipliers to simulate faulty telemetry
    ├── calculates hypothetical HPA replica counts for both clean and faulty metrics
    └── writes 13-column CSV to results/
```

### Fault Models

| Scenario | CPU multiplier | Memory multiplier |
|---|---|---|
| `cpu-spike` | ×3.0 | — |
| `cpu-drop` | ×0.3 | — |
| `memory-spike` | — | ×2.0 |
| `memory-drop` | — | ×0.5 |
| `random` | random from above, configurable rate | same |

### Desired-replicas formula (Kubernetes HPA algorithm)

```python
desired = ceil(current_replicas × observed_value / target_value)
# clamped to [min_replicas, max_replicas]
```

### CSV Output Schema (13 columns)

`timestamp, scenario, fault_type, pod_count, current_replicas, real_cpu_m, real_memory_mi, faulty_cpu_m, faulty_memory_mi, desired_replicas_cpu_clean, desired_replicas_cpu_faulty, desired_replicas_memory_clean, desired_replicas_memory_faulty`

The key comparison is `desired_replicas_*_clean` vs `desired_replicas_*_faulty` to quantify how bad metrics diverge from correct scaling decisions.

## Key Files

- `fault-injection/metric_fault_injector.py` — core logic: metric collection, fault injection, replica estimation, CSV writing
- `load/locustfile.py` — minimal Locust task hitting `/tools.descartes.teastore.webui/`
- `k8s/teastore.yaml` — namespace, Deployment, Service for TeaStore recommender
- `k8s/recommender-hpa.yaml` — CPU HPA; `recommender-hpa-memory.yaml` — memory HPA
- `results/` — CSV outputs (git-ignored)

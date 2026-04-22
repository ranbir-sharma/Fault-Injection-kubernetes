# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Kubernetes Autoscaling Fault Injection Testbed** (CPEN 533 course project). It studies how corrupted CPU/memory metrics affect both Horizontal Pod Autoscaler (HPA) and Vertical Pod Autoscaler (VPA) scaling decisions. Fault injection is **simulated in Python post-processing** — faulty metrics are never fed into the live Kubernetes control loop. The script computes what HPA/VPA *would* have done under corrupted telemetry, and records both clean and faulty outcomes side-by-side in CSV.

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
```

`k8s/metrics-server.yaml` in this repo is not a standalone install manifest. It is only a partial deployment fragment for reference; use the upstream `components.yaml` URL above, then apply `k8s/metrics-server-patch.yaml`.

## Deploy

```bash
./run.sh
# or manually:
kubectl apply -f k8s/teastore.yaml
kubectl apply -f k8s/recommender-hpa.yaml          # CPU-based HPA (60% target, 1-10 replicas)
# kubectl apply -f k8s/recommender-hpa-memory.yaml # Memory-based HPA (70% target)
kubectl apply -f k8s/recommender-vpa.yaml          # VPA in Off mode (recommends only, no pod restarts)
```

**Teardown:**
```bash
kubectl delete -f k8s/
```

## Running an Experiment

Start load generation first, then run the collector in parallel:

```bash
# Terminal 1 — generate load
cd load && locust
# Open http://localhost:8089, set 50-200 users, 5/sec spawn rate
# Target: http://localhost:$(kubectl get svc -n teastore teastore-webui -o jsonpath='{.spec.ports[0].nodePort}')

# Terminal 2 — collect metrics with fault injection simulation
python fault-injection/metric_fault_injector.py collect \
  --deployment teastore-webui \
  --label-selector app=teastore-webui \
  --scenario cpu-spike \
  --duration 600 \
  --interval 15 \
  --output results/cpu-spike.csv
```

Valid `--scenario` values: `baseline`, `cpu-spike`, `cpu-drop`, `memory-spike`, `memory-drop`, `random`, `random-multiplier`

**VPA-specific overrides** (optional, defaults match `k8s/recommender-vpa.yaml`):
```bash
  --vpa-safety-margin 1.15   # approximates VPA 90th-percentile headroom
  --vpa-min-cpu-m     100
  --vpa-max-cpu-m     2000
  --vpa-min-memory-mi 128
  --vpa-max-memory-mi 1024
```

**Mitigation filter overrides** (optional):
```bash
  --window-size      5    # sliding window depth for windowed median
  --zscore-threshold 2.0  # samples beyond this many std-devs are rejected
```

**Flask serve mode** (exposes `/metric` on port 5001 for external polling):
```bash
python fault-injection/metric_fault_injector.py serve
# GET http://localhost:5001/metric?scenario=cpu-spike&fault_rate=0.3
```

## Observing the Live Cluster

### HPA

```bash
# Watch replica count change in real time
kubectl get hpa -n teastore -w

# Full HPA status: current vs desired replicas, metric readings, last scale time
kubectl describe hpa recommender-hpa -n teastore

# Check recent scale-up/scale-down events
kubectl get events -n teastore --sort-by='.lastTimestamp' | grep -i hpa
```

### VPA (requires VPA controller installed)

```bash
# Check what resources VPA currently recommends for the pod
kubectl describe vpa teastore-recommender-vpa -n teastore

# Quick view of VPA recommendation (Lower Bound / Target / Upper Bound)
kubectl get vpa teastore-recommender-vpa -n teastore -o json \
  | python3 -c "
import json, sys
v = json.load(sys.stdin)
recs = v['status'].get('recommendation', {}).get('containerRecommendations', [])
for r in recs:
    print(r['containerName'], r['target'])
"
```

> Note: VPA is deployed in `Off` mode — it never restarts pods or mutates requests. The recommendations are read-only and safe to observe alongside live traffic.

### Pods and Resource Usage

```bash
# Watch pods scale up/down (HPA driven)
kubectl get pods -n teastore -w

# Live CPU and memory consumption per pod
kubectl top pods -n teastore

# Detailed resource requests/limits actually set on each pod
kubectl get pods -n teastore -o json \
  | python3 -c "
import json, sys
pods = json.load(sys.stdin)['items']
for p in pods:
    name = p['metadata']['name']
    for c in p['spec']['containers']:
        res = c.get('resources', {})
        print(name, c['name'], 'requests:', res.get('requests'), 'limits:', res.get('limits'))
"
```

### Deployment status

```bash
kubectl get deployment teastore-webui -n teastore
kubectl rollout status deployment/teastore-webui -n teastore
```

## Architecture

```
Locust Load Generator
    ↓ HTTP requests
TeaStore Pod  (k8s/teastore.yaml: 500m-1000m CPU, 256-512Mi memory)
    ↓ real metrics
Kubernetes metrics-server
    ↓
Live HPA  (recommender-hpa.yaml: 1-10 replicas, 60% CPU target)
Live VPA  (recommender-vpa.yaml: Off mode — recommends only)

PARALLEL — Python collector (fault-injection/metric_fault_injector.py):
    ├── reads real metrics via `kubectl top pods`
    ├── applies fault multipliers to produce faulty metrics
    ├── runs MetricFilter: z-score outlier rejection → windowed median → effective metrics
    ├── simulates HPA desired-replica count (clean / faulty / mitigated)
    ├── simulates VPA resource recommendation (clean / faulty / mitigated)
    ├── classifies VPA risk: under_provisioned / over_provisioned / accurate
    └── writes 31-column CSV to results/
```

### Fault Models

| Scenario | CPU multiplier | Memory multiplier |
|---|---|---|
| `baseline` | ×1.0 | ×1.0 |
| `cpu-spike` | ×3.0 | — |
| `cpu-drop` | ×0.3 | — |
| `memory-spike` | — | ×2.0 |
| `memory-drop` | — | ×0.5 |
| `random` | one of the above, at `--fault-rate` probability | same |
| `random-multiplier` | uniform random ×0.0–100.0 | same multiplier |

### HPA desired-replicas formula

```python
desired = ceil(current_replicas × observed_value / target_value)
# clamped to [min_replicas, max_replicas]
```

### VPA recommendation formula (simulated)

```python
recommended = observed_value × safety_margin   # default 1.15
# clamped to [vpa_min, vpa_max]
```

Risk classification written to `vpa_cpu_risk` / `vpa_memory_risk`:
- `under_provisioned` — faulty rec < real usage → pod risks CPU throttle or OOM kill
- `over_provisioned` — faulty rec > real usage × 1.5 → node capacity wasted, scheduling may fail
- `accurate` — within normal headroom

### CSV Output Schema (31 columns)

```
timestamp, scenario, deployment, label_selector, fault_type,
pod_count, current_replicas,
real_cpu_m, faulty_cpu_m, real_memory_mi, faulty_memory_mi,
desired_replicas_cpu_clean, desired_replicas_cpu_faulty,
desired_replicas_memory_clean, desired_replicas_memory_faulty,
vpa_cpu_rec_clean_m, vpa_cpu_rec_faulty_m,
vpa_memory_rec_clean_mi, vpa_memory_rec_faulty_mi,
vpa_cpu_risk, vpa_memory_risk,
cpu_outlier_rejected, memory_outlier_rejected,
effective_cpu_m, effective_memory_mi,
desired_replicas_cpu_mitigated, desired_replicas_memory_mitigated,
vpa_cpu_rec_mitigated_m, vpa_memory_rec_mitigated_mi,
vpa_cpu_risk_mitigated, vpa_memory_risk_mitigated
```

Key comparisons:
- `desired_replicas_*_clean` vs `desired_replicas_*_faulty` — HPA divergence under bad metrics
- `desired_replicas_*_faulty` vs `desired_replicas_*_mitigated` — how much mitigation recovers toward clean
- `vpa_*_rec_clean_*` vs `vpa_*_rec_faulty_*` — VPA resource inflation/deflation under bad metrics
- `vpa_*_risk` vs `vpa_*_risk_mitigated` — whether mitigation resolves the reliability/availability risk
- `cpu_outlier_rejected` / `memory_outlier_rejected` — which samples were caught by z-score filter

## Key Files

- `fault-injection/metric_fault_injector.py` — core logic: metric collection, fault injection, HPA + VPA simulation, CSV writing
- `load/locustfile.py` — Locust load generator hitting `/tools.descartes.teastore.webui/`
- `k8s/teastore.yaml` — namespace, Deployment, Service
- `k8s/recommender-hpa.yaml` — CPU HPA; `recommender-hpa-memory.yaml` — memory HPA
- `k8s/recommender-vpa.yaml` — VPA in Off mode (observe recommendations without pod mutation)
- `results/` — CSV outputs (git-ignored)

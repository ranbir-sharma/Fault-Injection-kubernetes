# Kubernetes Fault Injection Experiment Guide

This project is a local Kubernetes autoscaling fault-injection testbed for TeaStore on Docker Desktop Kubernetes.

The default experiment path is now:

```text
Locust -> TeaStore WebUI -> TeaStore backend services
```

That is the right direction for this project because real users interact with the WebUI, not the recommender directly.

## What Changed

The codebase now deploys a minimal full TeaStore stack:

- `teastore-db`
- `teastore-registry`
- `teastore-persistence`
- `teastore-auth`
- `teastore-image`
- `teastore-recommender`
- `teastore-webui`

Locust should target the WebUI.

The default HPA experiment also targets the WebUI. Recommender-specific HPA manifests are still present for secondary experiments.

The Python fault-injection collector now defaults to the WebUI deployment and label selector, but you can override both from the command line.

The collector also computes a mitigated path for every sample using a transient fault filter, so each run now records clean, faulty, and mitigated outcomes side by side.

## What This Project Measures

The main question is:

```text
How would autoscaling behavior change if Kubernetes made scaling decisions from faulty CPU or memory telemetry while users interact through the TeaStore WebUI?
```

You can measure:

- CPU spike faults causing over-scaling.
- CPU drop faults causing under-scaling.
- Memory spike faults causing over-scaling.
- Memory drop faults causing under-scaling.
- Random faults causing unstable scaling estimates.
- Random multiplier faults that multiply both CPU and memory by a random value between `0` and `100`.
- Differences between clean, faulty, and mitigated replica estimates.
- Differences between clean, faulty, and mitigated VPA recommendations.
- Whether the mitigation filter recovers decisions toward the clean baseline.

## Important Terms

Horizontal scaling means changing the number of pods:

```text
1 pod -> 3 pods -> 6 pods
```

This is handled by HPA.

Vertical scaling means changing CPU and memory requests or limits for a pod:

```text
cpu request: 200m -> 500m
memory request: 256Mi -> 512Mi
```

This project simulates VPA recommendations in Python during every collector run.

The repo also includes a Kubernetes `VerticalPodAutoscaler` manifest for recommender, but live VPA resources only work if VPA CRDs and controllers are installed in the cluster.

## Current Architecture

```text
Locust load generator
        |
        v
TeaStore WebUI
        |
        v
TeaStore backend services
        |
        v
metrics-server reports CPU and memory
        |
        +--> Kubernetes HPA scales a chosen deployment from clean metrics
        |
        +--> Python collector simulates faulty metrics, applies mitigation, and writes CSV results
```

The collector does not inject corrupted metrics into the live Kubernetes metrics pipeline. It reads real CPU and memory, simulates faulty values in Python, applies a mitigation filter, and records what HPA/VPA decisions would have changed under clean, faulty, and mitigated inputs.

## Files That Matter

```text
k8s/teastore.yaml
```

Deploys the TeaStore namespace and the full app stack, including the WebUI service.

```text
k8s/webui-hpa.yaml
```

Default CPU-based HPA for the WebUI.

```text
k8s/webui-hpa-memory.yaml
```

Memory-based HPA for the WebUI.

```text
k8s/recommender-hpa.yaml
```

Optional CPU-based HPA for recommender-only experiments.

```text
k8s/recommender-hpa-memory.yaml
```

Optional memory-based HPA for recommender-only experiments.

```text
fault-injection/metric_fault_injector.py
```

Collects real pod metrics, injects synthetic faults, estimates HPA replica counts, simulates VPA recommendations, applies mitigation filtering, and writes CSV results.

```text
load/locustfile.py
```

Generates HTTP load against the TeaStore WebUI path.

```text
scripts/run_experiment.sh
```

Deploys TeaStore and applies the default WebUI CPU HPA.

## Setup

Start Docker Desktop and enable Kubernetes.

Verify the cluster:

```sh
kubectl get nodes
```

Install Python dependencies:

```sh
pip install -r requirements.txt
```

For a fresh Kubernetes cluster, install metrics-server from the upstream manifest first:

```sh
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

Do not run `kubectl apply -f k8s/metrics-server.yaml`. That file in this repo is only a partial deployment fragment and will fail if applied by itself.

Patch metrics-server for Docker Desktop using the repo patch file:

```sh
kubectl patch deployment metrics-server -n kube-system \
  --patch-file k8s/metrics-server-patch.yaml
```

Restart metrics-server:

```sh
kubectl rollout restart deployment metrics-server -n kube-system
```

Verify the metrics API:

```sh
kubectl top nodes
kubectl top pods -n kube-system
```

If `kubectl top` fails, do not continue with HPA experiments until metrics-server is healthy.

The shortest correct fresh-cluster setup is:

```sh
pip install -r requirements.txt
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deployment metrics-server -n kube-system --patch-file k8s/metrics-server-patch.yaml
kubectl rollout restart deployment metrics-server -n kube-system
kubectl top nodes
kubectl apply -f k8s/teastore.yaml
kubectl apply -f k8s/webui-hpa.yaml
```

Or use the bootstrap script that runs the full sequence and waits for readiness:

```sh
./scripts/bootstrap_fresh_k8s.sh
```

Optional HPA mode:

```sh
./scripts/bootstrap_fresh_k8s.sh webui
./scripts/bootstrap_fresh_k8s.sh recommender
./scripts/bootstrap_fresh_k8s.sh none
```

## Deploy The Workload

Deploy the TeaStore stack and the default WebUI CPU HPA:

```sh
./scripts/run_experiment.sh
```

Or manually:

```sh
kubectl apply -f k8s/teastore.yaml
kubectl apply -f k8s/webui-hpa.yaml
```

Check the objects:

```sh
kubectl get pods -n teastore
kubectl get service -n teastore
kubectl get hpa -n teastore
```

Forward the TeaStore WebUI service to your laptop:

```sh
kubectl port-forward -n teastore service/teastore-webui 8080:8080
```

Open:

```text
http://localhost:8080/tools.descartes.teastore.webui/
```

## Generate Load

In another terminal:

```sh
cd load
locust
```

Open:

```text
http://localhost:8089
```

Use this Locust host:

```text
http://localhost:8080
```

The Locust file already requests:

```text
/tools.descartes.teastore.webui/
```

That means all benchmark traffic enters through the WebUI.

## Default WebUI Experiment

The default experiment scales the WebUI while sending requests to the WebUI.

This is the cleanest end-to-end setup:

```text
Locust -> WebUI -> WebUI HPA -> fault-injection collector
```

Run a baseline:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario baseline \
  --duration 600 \
  --interval 15 \
  --output results/webui-baseline.csv
```

Run a CPU spike experiment:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario cpu-spike \
  --duration 600 \
  --interval 15 \
  --output results/webui-cpu-spike.csv
```

Run a CPU drop experiment:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario cpu-drop \
  --duration 600 \
  --interval 15 \
  --output results/webui-cpu-drop.csv
```

Run a memory spike experiment:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario memory-spike \
  --duration 600 \
  --interval 15 \
  --output results/webui-memory-spike.csv
```

Run a memory drop experiment:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario memory-drop \
  --duration 600 \
  --interval 15 \
  --output results/webui-memory-drop.csv
```

Run random faults:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario random \
  --fault-rate 0.2 \
  --duration 600 \
  --interval 15 \
  --output results/webui-random.csv
```

Run random multiplier faults:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario random-multiplier \
  --duration 600 \
  --interval 15 \
  --output results/webui-random-multiplier.csv
```

In `random-multiplier`, the collector samples a random floating-point value from `0` to `100` on each interval and multiplies both CPU and memory by that value.

## Mitigation Filter

The collector includes a two-layer transient fault mitigation filter named `MetricFilter`.

Layer 1: z-score outlier rejection

- before a new faulty sample enters the sliding window, the filter compares it with the current rolling mean and standard deviation
- if the sample is more than `zscore-threshold` standard deviations away, it is treated as a transient fault
- rejected samples are replaced with the rolling mean

Layer 2: windowed median

- the effective metric value used for mitigated HPA/VPA simulation is the median of the last `window-size` accepted samples

Default mitigation parameters:

```text
window-size = 5
zscore-threshold = 2.0
```

Optional tuning:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario random-multiplier \
  --window-size 5 \
  --zscore-threshold 2.0 \
  --duration 600 \
  --interval 15 \
  --output results/webui-random-multiplier.csv
```

## Switching The Collector Target

The collector defaults to:

```text
deployment     = teastore-webui
label selector = app=teastore-webui
```

To target recommender instead:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario cpu-spike \
  --deployment teastore-recommender \
  --label-selector app=teastore-recommender \
  --duration 600 \
  --interval 15 \
  --output results/recommender-cpu-spike.csv
```

This lets you keep WebUI as the entry point while collecting metrics for another deployment.

## Optional Recommender Experiment

If you want user traffic to enter through the WebUI but want HPA to scale recommender instead:

```sh
kubectl delete hpa webui-hpa -n teastore
kubectl apply -f k8s/recommender-hpa.yaml
```

Then collect recommender data:

```sh
python fault-injection/metric_fault_injector.py collect \
  --scenario cpu-spike \
  --deployment teastore-recommender \
  --label-selector app=teastore-recommender \
  --duration 600 \
  --interval 15 \
  --output results/recommender-cpu-spike.csv
```

That experiment path is:

```text
Locust -> WebUI -> recommender -> recommender HPA -> fault-injection collector
```

## CSV Columns

Each CSV row includes:

```text
timestamp
scenario
deployment
label_selector
fault_type
pod_count
current_replicas
real_cpu_m
faulty_cpu_m
real_memory_mi
faulty_memory_mi
desired_replicas_cpu_clean
desired_replicas_cpu_faulty
desired_replicas_memory_clean
desired_replicas_memory_faulty
vpa_cpu_rec_clean_m
vpa_cpu_rec_faulty_m
vpa_memory_rec_clean_mi
vpa_memory_rec_faulty_mi
vpa_cpu_risk
vpa_memory_risk
cpu_outlier_rejected
memory_outlier_rejected
effective_cpu_m
effective_memory_mi
desired_replicas_cpu_mitigated
desired_replicas_memory_mitigated
vpa_cpu_rec_mitigated_m
vpa_memory_rec_mitigated_mi
vpa_cpu_risk_mitigated
vpa_memory_risk_mitigated
```

The most important comparisons are:

```text
desired_replicas_cpu_clean vs desired_replicas_cpu_faulty
desired_replicas_cpu_clean vs desired_replicas_cpu_mitigated
desired_replicas_memory_clean vs desired_replicas_memory_faulty
desired_replicas_memory_clean vs desired_replicas_memory_mitigated
vpa_cpu_rec_clean_m vs vpa_cpu_rec_faulty_m vs vpa_cpu_rec_mitigated_m
vpa_memory_rec_clean_mi vs vpa_memory_rec_faulty_mi vs vpa_memory_rec_mitigated_mi
vpa_cpu_risk vs vpa_cpu_risk_mitigated
vpa_memory_risk vs vpa_memory_risk_mitigated
```

If the faulty desired replica count is higher than the clean desired replica count, the fault would cause over-scaling.

If the faulty desired replica count is lower than the clean desired replica count, the fault would cause under-scaling.

If the mitigated result moves back toward the clean result, the mitigation filter improved the decision quality.

## Suggested Experiment Matrix

Use the same Locust user count, spawn rate, and duration for every scenario.

Recommended matrix:

```text
WebUI HPA + baseline
WebUI HPA + cpu-spike
WebUI HPA + cpu-drop
WebUI HPA + memory-spike
WebUI HPA + memory-drop
WebUI HPA + random
WebUI HPA + random-multiplier
Recommender HPA + baseline
Recommender HPA + cpu-spike
Recommender HPA + cpu-drop
Recommender HPA + memory-spike
Recommender HPA + memory-drop
Recommender HPA + random
Recommender HPA + random-multiplier
```

## Useful Result Metrics

Over-scaling amount:

```text
desired_replicas_faulty - desired_replicas_clean
```

Under-scaling amount:

```text
desired_replicas_clean - desired_replicas_faulty
```

Fault impact duration:

```text
number of CSV rows where desired_replicas_faulty != desired_replicas_clean
```

Replica instability:

```text
number of times desired_replicas_faulty changes between consecutive rows
```

Maximum over-scaling:

```text
max(desired_replicas_faulty - desired_replicas_clean)
```

Maximum under-scaling:

```text
max(desired_replicas_clean - desired_replicas_faulty)
```

## What To Put In The Report

A strong report can use these sections:

```text
1. Objective
2. TeaStore architecture
3. Kubernetes deployment
4. Workload generation through WebUI
5. Fault model
6. Experiment matrix
7. Results
8. Analysis
9. Limitations
10. Future work
```

Important limitations:

```text
Faults are simulated after metrics collection.
Faulty metrics are not injected into the live Kubernetes HPA control loop.
Live Kubernetes VPA requires separate VPA CRDs and controllers in the cluster.
Docker Desktop Kubernetes is a local test environment, not a production cluster.
```

Important future work:

```text
Add Prometheus and Prometheus Adapter.
Expose faulty_cpu and faulty_memory as real external metrics.
Configure HPA to consume those external metrics directly.
Install VPA and compare HPA vs VPA under faulty metrics.
Add graph generation scripts for CSV outputs.
```

#!/bin/bash

kubectl apply -f k8s/teastore.yaml
kubectl apply -f k8s/webui-hpa.yaml

echo "Waiting for TeaStore pods..."
sleep 30

kubectl get pods -n teastore
kubectl get hpa -n teastore

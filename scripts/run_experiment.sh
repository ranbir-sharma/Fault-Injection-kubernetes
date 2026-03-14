#!/bin/bash

kubectl apply -f k8s/teastore.yaml
kubectl apply -f k8s/recommender-hpa.yaml

echo "Waiting for pods..."
sleep 20

kubectl get pods
kubectl get hpa
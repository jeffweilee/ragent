# Kubernetes Deployment

A Kubernetes Deployment manages a stateless application by declaring the
desired number of replicas of a Pod. The Deployment controller continuously
reconciles the actual cluster state toward the spec, replacing failed Pods
and rolling out new versions.

## Configuring a Deployment

A Deployment is configured via a YAML manifest with `apiVersion: apps/v1`
and `kind: Deployment`. Required fields under `spec` are `replicas`,
`selector.matchLabels`, and `template`, which is itself a Pod template
including the container image, ports, and resource requests/limits.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 3
  selector:
    matchLabels: { app: web }
  template:
    metadata:
      labels: { app: web }
    spec:
      containers:
        - name: web
          image: nginx:1.27
```

## Best Practices

- Pin container image tags to immutable digests, never `:latest`.
- Set CPU and memory `requests` and `limits` so the scheduler can place Pods correctly.
- Configure `readinessProbe` and `livenessProbe` so rolling updates do not send
  traffic to unready Pods.
- Use `RollingUpdate` strategy with `maxSurge` and `maxUnavailable` tuned to
  cluster capacity.

## Troubleshooting

- `kubectl rollout status deployment/<name>` shows progress.
- `kubectl describe deployment <name>` surfaces failed conditions such as
  `ProgressDeadlineExceeded`.
- `kubectl rollout undo deployment/<name>` reverts to the previous ReplicaSet
  when a new version misbehaves.

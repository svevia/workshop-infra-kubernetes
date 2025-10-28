# EKS Auto Mode Optimization for On-Demand Multi-Tenant CargoCATs Deployment

## Problem
- Each new namespace (demo-1, demo-2, etc.) triggers new node creation
- Node provisioning adds 2-3 minutes to deployment time
- Cold start problem for each tenant

## Solutions

### 1. Node Warm Pool Strategy (Recommended)

Create a "dummy" workload that keeps nodes warm:

```yaml
# warm-pool-keeper.yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: node-warm-pool
  namespace: kube-system
spec:
  selector:
    matchLabels:
      app: node-warm-pool
  template:
    metadata:
      labels:
        app: node-warm-pool
    spec:
      tolerations:
      - operator: Exists
      containers:
      - name: warm-pool
        image: busybox
        command: ["sleep", "infinity"]
        resources:
          requests:
            cpu: "10m"
            memory: "16Mi"
          limits:
            cpu: "50m"
            memory: "64Mi"
      nodeSelector:
        karpenter.sh/nodepool: default
```

### 2. Optimize Resource Requests for Better Bin Packing

Current CargoCATs resource totals per namespace:
- CPU Requests: ~640m (0.64 vCPU)
- Memory Requests: ~2.1GB
- This fits well on m5.large (2 vCPU, 8GB RAM)

### 3. Node Pool Configuration

Configure Karpenter for faster provisioning:

```yaml
# karpenter-nodepool.yaml
apiVersion: karpenter.sh/v1beta1
kind: NodePool
metadata:
  name: cargocats-pool
spec:
  template:
    metadata:
      labels:
        workload-type: cargocats
    spec:
      # Fast provisioning instance types
      nodeClassRef:
        apiVersion: karpenter.k8s.aws/v1beta1
        kind: EC2NodeClass
        name: cargocats-nodeclass
      requirements:
      - key: kubernetes.io/arch
        operator: In
        values: ["amd64"]
      - key: node.kubernetes.io/instance-type
        operator: In
        values: ["m5.large", "m5.xlarge", "m6i.large", "m6i.xlarge"]
      - key: karpenter.sh/capacity-type
        operator: In
        values: ["on-demand"] # Faster than spot for immediate provisioning
      taints: []
  limits:
    cpu: "1000"
    memory: "1000Gi"
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 10m # Keep nodes longer for reuse
    expireAfter: 60m # Don't expire too quickly
---
apiVersion: karpenter.k8s.aws/v1beta1
kind: EC2NodeClass
metadata:
  name: cargocats-nodeclass
spec:
  # Use faster EBS GP3 for better performance
  blockDeviceMappings:
  - deviceName: /dev/xvda
    ebs:
      volumeSize: 50Gi
      volumeType: gp3
      iops: 3000
      throughput: 125
  # Use pre-cached AMI
  amiFamily: AL2
  userData: |
    #!/bin/bash
    /etc/eks/bootstrap.sh cargocats-cluster
    
    # Pre-pull common images to reduce startup time
    docker pull 771960604435.dkr.ecr.eu-west-1.amazonaws.com/workshop-images/dataservice:v1.1 &
    docker pull 771960604435.dkr.ecr.eu-west-1.amazonaws.com/workshop-images/frontgateservice:v1 &
    docker pull 771960604435.dkr.ecr.eu-west-1.amazonaws.com/dockerhub-images/library/mysql:9 &
```

### 4. Pre-warming Strategy

Deploy a "canary" instance that keeps at least one node warm:

```yaml
# node-prewarmer.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: node-prewarmer
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: node-prewarmer
  template:
    metadata:
      labels:
        app: node-prewarmer
    spec:
      containers:
      - name: prewarmer
        image: busybox
        command: ["sleep", "infinity"]
        resources:
          requests:
            cpu: "500m"     # Reserve space for ~1 CargoCATs instance
            memory: "2Gi"   # Reserve memory for ~1 CargoCATs instance
          limits:
            cpu: "1000m"
            memory: "4Gi"
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: workload-type
                operator: In
                values: ["cargocats"]
```

### 5. Image Pre-caching

Use a DaemonSet to pre-cache CargoCATs images on all nodes:

```yaml
# image-precacher.yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: cargocats-image-cache
  namespace: kube-system
spec:
  selector:
    matchLabels:
      app: image-cache
  template:
    metadata:
      labels:
        app: image-cache
    spec:
      initContainers:
      # Pre-pull all CargoCATs images
      - name: cache-dataservice
        image: 771960604435.dkr.ecr.eu-west-1.amazonaws.com/workshop-images/dataservice:v1.1
        command: ["echo", "cached"]
      - name: cache-frontgate
        image: 771960604435.dkr.ecr.eu-west-1.amazonaws.com/workshop-images/frontgateservice:v1
        command: ["echo", "cached"]
      - name: cache-mysql
        image: 771960604435.dkr.ecr.eu-west-1.amazonaws.com/dockerhub-images/library/mysql:9
        command: ["echo", "cached"]
      # Add other images...
      containers:
      - name: sleep
        image: busybox
        command: ["sleep", "infinity"]
        resources:
          requests:
            cpu: "1m"
            memory: "1Mi"
```

### 6. Namespace Template with Node Selector

Update your Helm values to prefer existing nodes:

```yaml
# Add to values.yaml
nodeSelector:
  workload-type: cargocats

# Or use node affinity for softer preference
affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      preference:
        matchExpressions:
        - key: workload-type
          operator: In
          values: ["cargocats"]
```

## Implementation Priority

1. **Immediate (5 min)**: Deploy node pre-warmer
2. **Short-term (15 min)**: Configure Karpenter node pool with longer consolidation
3. **Medium-term (30 min)**: Implement image pre-caching
4. **Long-term**: Monitor and tune based on usage patterns

## Expected Results

- **Before**: 3-5 minutes for new namespace (node provisioning + image pulls)
- **After**: 30-60 seconds for new namespace (existing nodes + cached images)
- **Cost**: Minimal overhead (~$20-40/month for pre-warming)
- **Benefits**: Much faster tenant onboarding, better user experience
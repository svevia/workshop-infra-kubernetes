# Node Management Strategy

# Node Management Strategy

## Architecture

The cluster uses a dedicated node approach where each workshop namespace gets its own EC2 node:

- **System Nodes** (2x m5.large): Run infrastructure components (observability, contrast-agent-operator, workshop-deployer)
- **Workshop Nodes** (Nx m5.2xlarge): Each workshop namespace gets one dedicated node with `dedicated-namespace` label

## Management Approach

All node management is handled automatically by the **Workshop Deployer API** (`5-workshop-deployer/api/app/main.py`). Manual node management tools have been removed in favor of this API-based approach.

## Scale-In Protection

To prevent AWS Auto Scaling from randomly terminating nodes with active workloads, we use **Scale-In Protection**:

### How it works:

1. **When a namespace is created** (via Workshop Deployer API):
   - Scales up the workshop-nodes ASG
   - Waits for the new node to join the cluster
   - Labels the new node with `dedicated-namespace=<namespace>`
   - **Enables scale-in protection** on the EC2 instance
   - Creates Contrast agent connection resources
   - Deploys the workshop application

2. **When a namespace is deleted** (via Workshop Deployer API):
   - Finds the dedicated node for the namespace
   - **Removes scale-in protection** from the instance
   - Drains the node
   - Terminates the specific EC2 instance
   - Decreases the ASG desired capacity

### Benefits:
- ✅ Nodes with workloads are protected from random termination
- ✅ Only unprotected nodes can be scaled down by ASG
- ✅ Deterministic node removal (not random)
- ✅ No need to change ASG Termination Policy
- ✅ Automatic management via API

## Node Selectors and Tolerations

### System Nodes
All system nodes have:
- **Label**: `node-type: system`
- **Taint**: `node-type=system:NoSchedule`

Infrastructure pods (observability, contrast-agent-operator, workshop-deployer) require:
```yaml
nodeSelector:
  node-type: system

tolerations:
  - key: node-type
    value: system
    effect: NoSchedule
    operator: Equal
```

### Workshop Nodes
- **Label**: `node-type: workshop`
- **No taints** (workloads use `nodeName` for precise placement)

## Configuration Files

### 1. EKS Cluster Configuration
**File**: `1-eks/work-cluster.yaml`
```yaml
nodeGroups:
  - name: system-nodes
    instanceType: m5.large
    desiredCapacity: 2  # 2 nodes for HA
    minSize: 2
    maxSize: 4
    taints:
      - key: node-type
        value: system
        effect: NoSchedule

  - name: workshop-nodes
    instanceType: m5.2xlarge  # 8 vCPU, 32GB RAM - fits one cargo-cats deployment
    desiredCapacity: 1        # Adjust based on workshop count
    minSize: 0                # Can scale down to 0 when not in use
    maxSize: 20               # Can scale up to 20 workshops
```

### 2. Observability Stack
**File**: `3-observability-stack/values.yaml`

Includes nodeSelector and tolerations for:
- `opensearch`
- `opensearch-dashboard`
- `k8s-metacollector`
- `falco`
- `fluent-bit` (DaemonSet - runs on all nodes)

### 3. Contrast Agent Operator
**File**: `4-contrast-agent-operator/values.yaml`
```yaml
operator:
  nodeSelector:
    node-type: system
  tolerations:
    - key: node-type
      value: system
      effect: NoSchedule
      operator: Equal
```

### 4. Workshop Deployer
**File**: `5-workshop-deployer/k8s/values.yaml`
```yaml
nodeSelector:
  node-type: system

tolerations:
  - key: node-type
    value: system
    effect: NoSchedule
    operator: Equal
```

## Workshop Deployer API

The Workshop Deployer API provides RESTful endpoints for workshop lifecycle management:

### API Endpoints

**Create Workshop (Auto-Generated Namespace):**
```bash
POST /workshops/next
Headers: X-API-Key: <your-key>
```

**Create Workshop (Custom Namespace):**
```bash
POST /workshops/create?namespace=<name>
Headers: X-API-Key: <your-key>
```

**Create Workshop (With Custom Credentials):**
```bash
POST /workshops/create-with-params
Headers: X-API-Key: <your-key>
Body: {
  "namespace": "demo1",
  "agent_token": "optional-custom-token",
  "user_api_key": "optional-api-key",
  "user_auth": "optional-auth-header"
}
```

**Delete Workshop:**
```bash
DELETE /workshops/delete?namespace=<name>
Headers: X-API-Key: <your-key>
```

### Implementation Details

The API implementation in `5-workshop-deployer/api/app/main.py` includes:

1. **`scale_up_node_group_and_label_node(namespace)`**:
   - Finds the Auto Scaling Group for workshop-nodes
   - Scales up the ASG by 1
   - Waits for the new node to join (timeout: 180s)
   - Labels the node with `dedicated-namespace=<namespace>`
   - Enables scale-in protection on the instance
   
2. **`delete_workshop(namespace)`**:
   - Finds the node with `dedicated-namespace=<namespace>`
   - Removes scale-in protection
   - Drains the node
   - Terminates the EC2 instance
   - Decreases ASG desired capacity

3. **`setup_contrast_resources(namespace)`**:
   - Creates the namespace
   - Calls scale_up_node_group_and_label_node()
   - Creates Contrast agent connection secret
   - Creates ClusterAgentConnection CRD

See the [README.md](README.md) for API usage examples.

## Manual Node Management

### Check protected instances:
```bash
aws autoscaling describe-auto-scaling-instances \
  --region eu-west-1 \
  --query 'AutoScalingInstances[?ProtectedFromScaleIn==`true`]'
```

### Manually protect an instance:
```bash
aws autoscaling set-instance-protection \
  --instance-ids <instance-id> \
  --auto-scaling-group-name <asg-name> \
  --protected-from-scale-in \
  --region eu-west-1
```

### Manually unprotect an instance:
```bash
aws autoscaling set-instance-protection \
  --instance-ids <instance-id> \
  --auto-scaling-group-name <asg-name> \
  --no-protected-from-scale-in \
  --region eu-west-1
```

### Safe node removal procedure:
```bash
# 1. Find the node and its instance ID
kubectl get node <node-name> -o jsonpath='{.spec.providerID}'

# 2. Remove protection
aws autoscaling set-instance-protection --instance-ids <instance-id> --auto-scaling-group-name <asg-name> --no-protected-from-scale-in --region eu-west-1

# 3. Drain the node
kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --force

# 4. Terminate the instance
aws ec2 terminate-instances --instance-ids <instance-id> --region eu-west-1

# 5. Update ASG desired capacity
aws autoscaling set-desired-capacity --auto-scaling-group-name <asg-name> --desired-capacity <new-capacity> --region eu-west-1
```

## Troubleshooting

### Pods stuck in Pending on system nodes
**Symptom**: Pods with `nodeSelector: node-type: system` are Pending  
**Cause**: Missing tolerations for `node-type=system:NoSchedule` taint  
**Fix**: Add tolerations to the deployment/pod spec

### Infrastructure pods on workshop nodes
**Symptom**: Observability/operator pods running on workshop nodes  
**Cause**: Missing nodeSelector  
**Fix**: Add `nodeSelector: {node-type: system}` to the deployment

### Node terminated but workload running
**Symptom**: ASG terminated a node with active workshop  
**Cause**: Scale-in protection not set or removed prematurely  
**Fix**: Ensure workshop-deployer API is used for all workshop creation/deletion (don't manually terminate instances)

### Workshop creation times out
**Symptom**: API returns timeout error waiting for node  
**Cause**: Node taking too long to join cluster (>180s)  
**Fix**: Check AWS console for node status, check EC2 instance health, review CloudWatch logs

### Cannot scale down to 0
**Symptom**: Setting desired capacity to 0 fails  
**Cause**: ASG minSize is set higher than 0  
**Fix**: 
```bash
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name <asg-name> \
  --min-size 0 \
  --region eu-west-1
```

## Best Practices

1. **Use the Workshop Deployer API** for all workshop lifecycle management
2. **System nodes should have taints** to prevent workshop pods from landing there
3. **Infrastructure pods need tolerations** to run on system nodes
4. **Use 2+ system nodes** for HA (minimum 4 vCPU total needed)
5. **Workshop node size** (m5.2xlarge) is sized for one complete cargo-cats deployment
6. **Don't manually terminate protected instances** - use the API delete endpoint
7. **Monitor ASG metrics** in CloudWatch to track scaling activities
8. **Set minSize: 0** when cluster is not in use to reduce costs

## Cost Optimization

When the cluster is not actively used for workshops:

```bash
# Scale down workshop nodes to 0
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name <asg-name> \
  --min-size 0 \
  --desired-capacity 0 \
  --region eu-west-1

# System nodes will continue running (~$140/month)
# Workshop nodes will be terminated (saves ~$280/month per node)
```

To resume workshops, simply use the Workshop Deployer API - it will automatically scale up as needed.

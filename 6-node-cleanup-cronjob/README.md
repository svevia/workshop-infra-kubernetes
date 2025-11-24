# Node Cleanup CronJob

Kubernetes CronJob that automatically cleans up orphaned workshop nodes every 5 minutes.

## What It Does

This CronJob identifies and terminates "orphaned" workshop nodes - nodes that:

1. **Have no namespace label**: Workshop nodes with `node-type=workshop` but no `dedicated-namespace` label
2. **Reference deleted namespaces**: Workshop nodes with a `dedicated-namespace` label pointing to a namespace that no longer exists

When an orphaned node is found, the job will:
- Remove scale-in protection
- Drain the node (evict pods)
- Terminate the EC2 instance
- Update the Auto Scaling Group desired capacity

## Prerequisites

1. **AWS IAM Role** with the following permissions:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "autoscaling:DescribeAutoScalingGroups",
           "autoscaling:SetInstanceProtection",
           "autoscaling:SetDesiredCapacity",
           "ec2:TerminateInstances",
           "ec2:DescribeInstances"
         ],
         "Resource": "*"
       }
     ]
   }
   ```

2. **Service Account with IRSA** (IAM Roles for Service Accounts) configured

3. **Docker registry** access to push the container image

## Setup

### 1. Update Configuration

Edit `Makefile` and set your registry:
```makefile
REGISTRY ?= your-registry.azurecr.io
AWS_REGION ?= eu-west-1
```

Edit `cronjob.yaml` and update:
- Image registry path
- AWS region
- NodeSelector if needed

### 2. Configure IAM Role (IRSA)

```bash
# Create IAM policy
aws iam create-policy \
  --policy-name NodeCleanupPolicy \
  --policy-document file://iam-policy.json

# Associate IAM role with service account (using eksctl)
eksctl create iamserviceaccount \
  --name node-cleanup-sa \
  --namespace default \
  --cluster workshop-cluster \
  --attach-policy-arn arn:aws:iam::ACCOUNT_ID:policy/NodeCleanupPolicy \
  --approve \
  --override-existing-serviceaccounts
```

### 3. Build and Deploy

```bash
# Build, push, and deploy
make setup

# Or step by step:
make build
make push
make deploy
```

## Usage

### View CronJob Status
```bash
make status
```

### View Logs
```bash
make logs
```

### Manually Trigger Cleanup
```bash
make trigger
```

### Test Locally (requires kubeconfig and AWS credentials)
```bash
make test-run
```

### Delete CronJob
```bash
make delete
```

## Schedule

The CronJob runs **every 5 minutes** (`*/5 * * * *`).

To change the schedule, edit `cronjob.yaml`:
```yaml
spec:
  schedule: "*/5 * * * *"  # Change this line
```

Common schedules:
- Every 5 minutes: `*/5 * * * *`
- Every 10 minutes: `*/10 * * * *`
- Every hour: `0 * * * *`
- Every 6 hours: `0 */6 * * *`

## Monitoring

### Check Recent Jobs
```bash
kubectl get jobs -l app=node-cleanup --sort-by=.metadata.creationTimestamp
```

### Check Pods
```bash
kubectl get pods -l app=node-cleanup --sort-by=.metadata.creationTimestamp
```

### View Logs from Specific Job
```bash
kubectl logs job/node-cleanup-cronjob-<timestamp>
```

### Check CronJob Details
```bash
kubectl describe cronjob node-cleanup-cronjob
```

## Troubleshooting

### Job Fails with AWS Permission Errors

Check that:
1. IRSA is properly configured
2. Service account has the correct IAM role annotation
3. IAM policy includes all required permissions

```bash
# Check service account annotations
kubectl get sa node-cleanup-sa -o yaml
```

### Job Fails with Kubernetes Permission Errors

Check RBAC:
```bash
kubectl get clusterrole node-cleanup-role -o yaml
kubectl get clusterrolebinding node-cleanup-binding -o yaml
```

### No Nodes Being Cleaned Up

Check the logs:
```bash
make logs
```

The output will show:
- How many workshop nodes were found
- Which nodes are healthy
- Which nodes are orphaned and why

### Job Times Out

Increase the timeout in `cronjob.yaml`:
```yaml
spec:
  jobTemplate:
    spec:
      activeDeadlineSeconds: 600  # Add this (10 minutes)
```

## Safety Features

- **Concurrency Policy**: Set to `Forbid` - only one cleanup job runs at a time
- **Namespace Check**: Only terminates nodes referencing non-existent namespaces
- **Label Check**: Only terminates workshop nodes without proper labels
- **Graceful Draining**: Attempts to drain nodes before termination
- **ASG Updates**: Properly updates Auto Scaling Group capacity

## Example Output

```
🔍 Starting orphaned node cleanup...
✅ Loaded in-cluster Kubernetes config
✅ Found ASG: eksctl-workshop-cluster-nodegroup-workshop-nodes-NodeGroup-lURcHHW18Wc7
📊 Found 3 workshop nodes
📊 Found 52 namespaces in cluster
✅ Node ip-10-0-1-23 is healthy (namespace: demo1)
⚠️  Node ip-10-0-1-45 references non-existent namespace 'demo-old' - marking as orphaned
✅ Node ip-10-0-1-67 is healthy (namespace: demo2)

🗑️  Found 1 orphaned node(s) to clean up
🗑️  Terminating orphaned node: ip-10-0-1-45 (instance: i-0123456789abcdef0)
  ⚙️  Removing scale-in protection...
  ⚙️  Draining node...
  ⚙️  Terminating EC2 instance...
  ⚙️  Updating ASG desired capacity...
  ✅ Updated ASG desired capacity to 2

✅ Cleanup complete!
```

# Workshop Deployer IAM Setup

The workshop-deployer needs AWS IAM permissions to scale EKS node groups. Use **IRSA (IAM Roles for Service Accounts)** for secure, credential-less authentication.

## Setup Steps

### 1. Create IAM Policy

```bash
# Set your AWS account ID
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export CLUSTER_NAME="work-cluster"
export REGION="eu-west-1"

# Create the IAM policy
aws iam create-policy \
    --policy-name WorkshopDeployerPolicy \
    --policy-document file://iam-policy.json
```

### 2. Create IAM Role for Service Account

```bash
# Get the OIDC provider for your EKS cluster
export OIDC_PROVIDER=$(aws eks describe-cluster --name $CLUSTER_NAME --region $REGION --query "cluster.identity.oidc.issuer" --output text | sed -e "s/^https:\/\///")

# Create trust policy
cat > trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_PROVIDER}:sub": "system:serviceaccount:setup:workshop-deployer-sa",
          "${OIDC_PROVIDER}:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
EOF

# Create the IAM role
aws iam create-role \
    --role-name WorkshopDeployerRole \
    --assume-role-policy-document file://trust-policy.json

# Attach the policy to the role
aws iam attach-role-policy \
    --role-name WorkshopDeployerRole \
    --policy-arn arn:aws:iam::${AWS_ACCOUNT_ID}:policy/WorkshopDeployerPolicy
```

### 3. Deploy Workshop Deployer with IRSA

```bash
# Deploy with the IAM role annotation
helm upgrade --install workshop-deployer ./k8s \
    --namespace setup \
    --create-namespace \
    --set secret.apiKey="your-api-key-here" \
    --set ingress.host="setup.work.contrastdemo.com" \
    --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="arn:aws:iam::${AWS_ACCOUNT_ID}:role/WorkshopDeployerRole"
```

## Alternative: eksctl Shortcut

```bash
# eksctl can create the IAM role and service account in one command
eksctl create iamserviceaccount \
    --name workshop-deployer-sa \
    --namespace setup \
    --cluster $CLUSTER_NAME \
    --region $REGION \
    --attach-policy-arn arn:aws:iam::${AWS_ACCOUNT_ID}:policy/WorkshopDeployerPolicy \
    --approve \
    --override-existing-serviceaccounts
```

## Verify Setup

```bash
# Check if the service account has the annotation
kubectl get sa workshop-deployer-sa -n setup -o yaml | grep eks.amazonaws.com/role-arn

# Check the pod has AWS credentials
kubectl exec -n setup deployment/workshop-deployer-api -- aws sts get-caller-identity
```

## Troubleshooting

If you see "Unable to locate credentials":
1. Verify the service account annotation is correct
2. Check the IAM role trust policy matches the OIDC provider
3. Ensure the pod is using the correct service account
4. Restart the deployment: `kubectl rollout restart deployment/workshop-deployer-api -n setup`

## How It Works

1. **OIDC Provider**: EKS cluster has an OIDC identity provider
2. **Service Account**: Kubernetes ServiceAccount annotated with IAM role ARN
3. **Pod Token**: Pod gets a service account token projected as a volume
4. **AWS SDK**: AWS SDK reads the token and exchanges it for temporary AWS credentials
5. **No Secrets**: No AWS credentials stored anywhere - all via temporary tokens!

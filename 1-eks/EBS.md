# EBS CSI Driver Setup

## For EKS Auto Mode (Current Setup) ✅

**No action needed!** EKS Auto Mode comes with the EBS CSI driver pre-installed.

Just apply the storage class:
```bash
kubectl apply -f 1-eks/gp3-storageclass.yaml
```

The storage class uses `provisioner: ebs.csi.eks.amazonaws.com` which is built into EKS Auto Mode.

---

## For Standard EKS Clusters (Non-Auto Mode Only)

⚠️ **Only run these if NOT using EKS Auto Mode:**

```bash
eksctl create iamserviceaccount \
        --name ebs-csi-controller-sa \
        --namespace kube-system \
        --cluster workshop-cluster \
        --role-name AmazonEKS_EBS_CSI_DriverRole \
        --role-only \
        --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
        --approve --region eu-west-1

eksctl create addon --cluster workshop-cluster --name aws-ebs-csi-driver --version latest \
    --service-account-role-arn arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/AmazonEKS_EBS_CSI_DriverRole --force --region eu-west-1
```

Then use storage class with `provisioner: ebs.csi.aws.com` instead.
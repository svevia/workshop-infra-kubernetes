.PHONY: build push deploy delete test-run setup-iam setup-iam-manual trigger logs status setup

# Variables - Update these for your environment
REGISTRY ?= 771960604435.dkr.ecr.eu-west-1.amazonaws.com/workshop-images
IMAGE_NAME ?= node-cleanup
IMAGE_TAG ?= latest
FULL_IMAGE = $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
AWS_REGION ?= eu-west-1
AWS_ACCOUNT_ID ?= 771960604435
CLUSTER_NAME ?= workshop-cluster
NAMESPACE ?= setup
# Reuse existing IAM role from workshop-deployer-sa
EXISTING_IAM_ROLE ?= arn:aws:iam::771960604435:role/eksctl-workshop-cluster-addon-iamserviceaccou-Role1-KIlkTWtc2pv0

# Build the Docker image
build:
	@echo "🔨 Building Docker image..."
	docker buildx build --push --platform linux/amd64,linux/arm64 --tag $(FULL_IMAGE) .
	@echo "✅ Build complete: $(FULL_IMAGE)"

# Deploy the CronJob to Kubernetes
deploy:
	@echo "🚀 Deploying node cleanup CronJob..."
	kubectl apply -f cronjob.yaml
	@echo "✅ CronJob deployed successfully"
	@echo ""
	@echo "To view the CronJob:"
	@echo "  kubectl get cronjob node-cleanup-cronjob -n $(NAMESPACE)"
	@echo ""
	@echo "To view jobs:"
	@echo "  kubectl get jobs -l app=node-cleanup -n $(NAMESPACE)"
	@echo ""
	@echo "To view logs:"
	@echo "  kubectl logs -l app=node-cleanup --tail=100 -n $(NAMESPACE)"

# Delete the CronJob from Kubernetes
delete:
	@echo "🗑️  Deleting node cleanup CronJob..."
	kubectl delete -f cronjob.yaml || true
	@echo "✅ CronJob deleted"

# Test run the cleanup script locally (requires kubeconfig and AWS credentials)
test-run:
	@echo "🧪 Running cleanup script locally..."
	python3 cleanup.py

# Reuse existing ServiceAccount (no IAM setup needed)
setup-iam:
	@echo "✅ Using existing workshop-deployer-sa ServiceAccount"
	@echo ""
	@echo "The CronJob will use the 'workshop-deployer-sa' ServiceAccount which already has:"
	@echo "  IAM Role: $(EXISTING_IAM_ROLE)"
	@echo ""
	@echo "No IAM setup needed - the ServiceAccount and IAM role already exist!"
	@echo ""
	@echo "You can now deploy the CronJob with: make deploy"


# Manually trigger a job (for testing)
trigger:
	@echo "▶️  Manually triggering cleanup job..."
	kubectl create job --from=cronjob/node-cleanup-cronjob -n $(NAMESPACE) node-cleanup-manual-$(shell date +%s)
	@echo "✅ Job created. Check logs with:"
	@echo "  kubectl logs -l app=node-cleanup --tail=100 -f -n $(NAMESPACE)"

# View logs from the most recent job
logs:
	@echo "📋 Fetching logs from most recent cleanup job..."
	kubectl logs -l app=node-cleanup --tail=100 -f -n $(NAMESPACE)

# View CronJob status
status:
	@echo "📊 CronJob Status:"
	kubectl get cronjob node-cleanup-cronjob -n $(NAMESPACE)
	@echo ""
	@echo "Recent Jobs:"
	kubectl get jobs -l app=node-cleanup --sort-by=.metadata.creationTimestamp -n $(NAMESPACE)
	@echo ""
	@echo "Recent Pods:"
	kubectl get pods -l app=node-cleanup --sort-by=.metadata.creationTimestamp -n $(NAMESPACE)

# Complete setup (IAM, build, deploy)
setup: setup-iam build deploy
	@echo "🎉 Complete setup finished!"
	@echo ""
	@echo "Next steps:"
	@echo "  - Check status: make status"
	@echo "  - View logs: make logs"
	@echo "  - Trigger manually: make trigger"

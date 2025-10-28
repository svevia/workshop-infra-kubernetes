
aws-sso-login:
	@echo "Logging in to AWS SSO..."
	aws sso login

eksctl-create-cluster:
	@echo "Creating Work EKS Cluster..."
	eksctl create cluster -f work-cluster.yaml

create-cluster: eksctl-create-cluster

delete-cluster:
	@echo "Deleting Work EKS Cluster..."
	eksctl delete cluster -f work-cluster.yaml

scale-workshop-nodes:
	@echo "Scaling workshop nodes..."
	@if [ -z "$(DESIRED_CAPACITY)" ]; then \
		echo "Please provide DESIRED_CAPACITY. Example: make scale-workshop-nodes DESIRED_CAPACITY=5"; \
		exit 1; \
	fi
	eksctl scale nodegroup --cluster=workshop-cluster --region=eu-west-1 --name=workshop-nodes --nodes=$(DESIRED_CAPACITY)

add-workshop-node:
	@echo "Adding a new workshop node..."
	@CURRENT_NODES=$$(kubectl get nodes -l node-type=workshop --no-headers | wc -l); \
	NEW_COUNT=$$((CURRENT_NODES + 1)); \
	eksctl scale nodegroup --cluster=workshop-cluster --region=eu-west-1 --name=workshop-nodes --nodes=$$NEW_COUNT

remove-workshop-node:
	@echo "Removing a workshop node..."
	@CURRENT_NODES=$$(kubectl get nodes -l node-type=workshop --no-headers | wc -l); \
	if [ $$CURRENT_NODES -gt 1 ]; then \
		NEW_COUNT=$$((CURRENT_NODES - 1)); \
		eksctl scale nodegroup --cluster=workshop-cluster --region=eu-west-1 --name=workshop-nodes --nodes=$$NEW_COUNT; \
	else \
		echo "Cannot remove the last workshop node!"; \
	fi

setup-node-taints:
	@echo "Setting up node taints for system nodes..."
	kubectl taint nodes -l node-type=system node-type=system:NoSchedule --overwrite || true

add-storage-class:
	@echo "Adding default storage class to the cluster..."
	kubectl apply -f gp3-storageclass.yaml

update-kubeconfig:
	@echo "Updating kubeconfig for workshop-cluster..."
	aws eks update-kubeconfig --region eu-west-1 --name workshop-cluster
.SILENT:

ifneq (,$(wildcard ./.env))
    include .env
    export
endif

# Set the TLD for DNS resolution in Kubernetes
TLD=workshop.contrastdemo.com

# Cluster configuration
CLUSTER_NAME=workshop-cluster
REGION=eu-west-1

# ONE TIME SETUP TASKS - only do this once after deploying the cluster!
deploy-aws-load-balancer-controller:
	@echo "\nInstalling AWS Load Balancer Controller..."
	@echo "Creating IAM policy (ignoring if already exists)..."
	@aws iam create-policy --policy-name AWSLoadBalancerControllerIAMPolicy-work --policy-document file://iam_policy_complete.json 2>/dev/null || echo "Policy already exists, continuing..."
	@echo "Creating IAM service account..."
	@eksctl create iamserviceaccount \
		--cluster=$(CLUSTER_NAME) \
		--region=$(REGION) \
		--namespace=kube-system \
		--name=aws-load-balancer-controller \
		--role-name AmazonEKSLoadBalancerControllerRole-work \
		--attach-policy-arn=arn:aws:iam::$$(aws sts get-caller-identity --query Account --output text):policy/AWSLoadBalancerControllerIAMPolicy-work \
		--approve --override-existing-serviceaccounts 2>/dev/null || echo "Service account already exists, continuing..."
	@echo "Adding EKS Helm repository..."
	@helm repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true
	@helm repo update >/dev/null
	@echo "Installing AWS Load Balancer Controller..."
	@helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
		-n kube-system \
		--set clusterName=$(CLUSTER_NAME) \
		--set serviceAccount.create=false \
		--set serviceAccount.name=aws-load-balancer-controller
	@echo "Waiting for AWS Load Balancer Controller to be ready..."
	@kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=aws-load-balancer-controller -n kube-system --timeout=300s
	@echo "AWS Load Balancer Controller installed successfully!"

deploy-nginx: deploy-aws-load-balancer-controller
	@echo "\nPlease only do this once!\n\nSetting up NGINX Ingress Controller for workshop-cluster...\n"
	helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
	helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
	--namespace kube-system --create-namespace -f 2-nginx-ingress/values-nginx.yaml

	@echo "Fetching ALB URL..."
	$(eval ALB_URL:=$(shell kubectl get --namespace kube-system service/ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'))
	@echo "\nUse the URL ${ALB_URL} to access the application."

# ONE TIME SETUP TASKS - only do this once after deploying the cluster!
configure-dns:
	@echo "\nPlease only do this once!\n"
	@echo "Fetching ALB URL..."
	@echo "Waiting for ingress controller to get an external IP..."
	@until kubectl get --namespace kube-system service/ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null | grep -q .; do \
		echo "Waiting for load balancer..."; \
		sleep 10; \
	done
	$(eval ALB_URL:=$(shell kubectl get --namespace kube-system service/ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'))
	@echo "ALB URL: ${ALB_URL}"
	@echo "TLD: ${TLD}"
	@if [ -z "${ALB_URL}" ]; then \
		echo "Error: ALB URL is empty. Make sure the ingress controller is deployed and has an external IP."; \
		exit 1; \
	fi
	@if [ -z "${TLD}" ]; then \
		echo "Error: TLD is empty. Make sure TLD variable is set."; \
		exit 1; \
	fi
	@echo "\nConfiguring DNS rules for the workshop-cluster..."
	aws route53 change-resource-record-sets \
    	--hosted-zone-id /hostedzone/Z26P9SNXLR6Z7J \
    	--change-batch \
     '{"Changes": [ { "Action": "UPSERT", "ResourceRecordSet": { "Name": "${TLD}", "Type": "A", "AliasTarget":{ "HostedZoneId": "Z2IFOLAFXWLO4F","DNSName": "${ALB_URL}","EvaluateTargetHealth": true} } } ]}'
	aws route53 change-resource-record-sets \
    	--hosted-zone-id /hostedzone/Z26P9SNXLR6Z7J \
    	--change-batch \
	'{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"*.${TLD}","Type":"CNAME","TTL":300,"ResourceRecords":[{"Value":"${TLD}"}]}}]}'
	@echo "Note: It may take a few minutes for the DNS changes to propagate."
	@echo "Note: DNS records are not managed. Please manually delete the A and CNAME records when deleting the cluster"

prepare-cluster: deploy-nginx configure-dns

download-helm-dependencies:
	@echo "Downloading Helm chart dependencies..."
	@cd 3-observability-stack && helm dependency update
	@cd 4-contrast-agent-operator && helm dependency update
	@echo "Helm chart dependencies downloaded successfully."

validate-env-vars:
	@echo "Validating environment variables..."
	@if [ -z "$(CONTRAST__AGENT__TOKEN)" ]; then \
		echo "Error: CONTRAST__AGENT__TOKEN is not set in .env file"; \
		exit 1; \
	fi
	@if [ -z "$(CONTRAST__UNIQ__NAME)" ]; then \
		echo "Error: CONTRAST__UNIQ__NAME is not set in .env file"; \
		exit 1; \
	fi
	@if [ -z "$(CONTRAST__API__KEY)" ]; then \
		echo "Warning: CONTRAST__API__KEY is not set in .env file (optional for ADR data fetching and delete functionality)"; \
	fi
	@if [ -z "$(CONTRAST__API__AUTHORIZATION)" ]; then \
		echo "Warning: CONTRAST__API__AUTHORIZATION is not set in .env file (optional for ADR data fetching and delete functionality)"; \
	fi
	@if [ -z "$(DEPLOYER_API_KEY)" ]; then \
		echo "Warning: DEPLOYER_API_KEY is not set in .env file (the deployer won't be available)"; \
	fi
	@echo "Required environment variables are set."

deploy-contrast: download-helm-dependencies validate-env-vars
	@echo "\nDeploying Contrast Agent Operator..."
	helm upgrade --install contrast-agent-operator ./4-contrast-agent-operator --cleanup-on-fail 
	kubectl apply -f ./4-contrast-agent-operator/1-config.yaml -n contrast-agent-operator
	@echo "\nSetting Contrast Agent Operator Token..."
	kubectl -n contrast-agent-operator delete secret default-agent-connection-secret --ignore-not-found
	kubectl -n contrast-agent-operator create secret generic default-agent-connection-secret --from-literal=token=$(CONTRAST__AGENT__TOKEN)
	kubectl set env -n contrast-agent-operator deployment/contrast-agent-operator CONTRAST_INITCONTAINER_MEMORY_LIMIT="256Mi"
	@echo ""

deploy-observability-stack: download-helm-dependencies
	@echo "\nDeploying Observability Stack..."
	@echo "\nSetting up FluentBit and Falco..."
	helm upgrade --install observability-stack ./3-observability-stack --cleanup-on-fail \
		--namespace observability --create-namespace
	@echo ""
	@echo "\nSetting up OpenSearch..."
	sleep 10
	@until curl --insecure -s -o /dev/null -w "%{http_code}" http://opensearch.$(TLD)/ | grep -q "302"; do \
        echo "Waiting for OpenSearch..."; \
        sleep 5; \
    done

configure-opensearch:
	@echo "\nConfiguring OpenSearch Dashboard..."
	curl --insecure  -X POST -H "Content-Type: multipart/form-data" -H "osd-xsrf: osd-fetch" "http://opensearch.$(TLD)/api/saved_objects/_import?overwrite=true" -u admin:Contrast@123! --form file='@opesearch_savedobjects.ndjson'
	curl --insecure  -X POST -H 'Content-Type: application/json' -H 'osd-xsrf: osd-fetch' "http://opensearch.$(TLD)/api/opensearch-dashboards/settings" -u admin:Contrast@123! --data-raw '{"changes":{"defaultRoute":"/app/dashboards#/"}}'
	sleep 5;
	echo "OpenSearch setup complete."

setup-deployer-iam:
	@echo "\nSetting up IAM role for workshop-deployer (IRSA)..."
	@echo "This requires AWS credentials with IAM permissions"
	$(eval AWS_ACCOUNT_ID := $(shell aws sts get-caller-identity --query Account --output text))
	@echo "AWS Account ID: $(AWS_ACCOUNT_ID)"
	@echo "\nCreating IAM policy..."
	@aws iam create-policy \
		--policy-name WorkshopDeployerPolicy-$(CLUSTER_NAME) \
		--policy-document file://5-workshop-deployer/iam-policy.json 2>/dev/null || echo "Policy already exists"
	@echo "\nCreating IAM service account with eksctl..."
	@eksctl create iamserviceaccount \
		--name workshop-deployer-sa \
		--namespace setup \
		--cluster $(CLUSTER_NAME) \
		--region $(REGION) \
		--attach-policy-arn arn:aws:iam::$(AWS_ACCOUNT_ID):policy/WorkshopDeployerPolicy-$(CLUSTER_NAME) \
		--approve \
		--override-existing-serviceaccounts
	@echo "\nIAM setup complete! The workshop-deployer can now scale EKS node groups."

deploy-workshop-deployer: setup-deployer-iam
	@if [ -n "$(DEPLOYER_API_KEY)" ]; then \
		echo "\nDeploying Workshop Deployer..."; \
		helm upgrade --install workshop-deployer 5-workshop-deployer/k8s \
		--namespace setup \
		--create-namespace \
		--set serviceAccount.create=false \
		--set serviceAccount.name=workshop-deployer-sa \
		--set secret.apiKey="$(DEPLOYER_API_KEY)" \
		--set ingress.host="setup.$(TLD)"; \
		kubectl -n setup create secret generic contrast-agent-secret --from-literal=token=$(CONTRAST__AGENT__TOKEN) --dry-run=client -o yaml |kubectl apply -f -; \
		kubectl -n setup create secret generic contrast-api-secret --from-literal=api_key=$(CONTRAST__API__KEY) --from-literal=auth_header=$(CONTRAST__API__AUTHORIZATION) --dry-run=client -o yaml |kubectl apply -f -; \
	fi

print-deployment:
	echo "\n\nWork Infrastructure deployment complete!"
	echo "=================================================================="
	echo "Note: It may take a few minutes for the deployment to be fully ready."
	echo "==================================================================\n"
	echo ""
	echo " - Cluster Name: $(CLUSTER_NAME)"
	echo " - Region: $(REGION)"
	echo " - Contrast Agent Operator deployed to namespace: contrast-agent-operator"
	echo " - FluentBit deployed to namespace: observability"
	echo " - Falco deployed to namespace: observability"
	echo " - OpenSearch deployed to namespace: observability"
	echo ""
	echo "OpenSearch Dashboard: http://opensearch.$(TLD)"
	echo "  Username: admin"
	echo "  Password: Contrast@123!"
	echo ""
	echo "Workshop Deployer: http://setup.$(TLD)"
	echo ""

setup-kube: deploy-observability-stack configure-opensearch deploy-contrast deploy-workshop-deployer print-deployment
	@echo "\nSetting up Work Cluster monitoring and Contrast Agent Operator..."

uninstall:
	@echo "\nUninstalling Contrast Agent Operator and related components..."
	helm uninstall contrast-agent-operator || true
	kubectl delete namespace contrast-agent-operator --ignore-not-found
	helm uninstall observability-stack -n observability || true
	kubectl delete namespace observability --ignore-not-found
	helm uninstall workshop-deployer -n setup || true
	kubectl delete namespace setup --ignore-not-found
	@echo "\nUninstalling AWS Load Balancer Controller..."
	helm uninstall aws-load-balancer-controller -n kube-system || true
	helm uninstall ingress-nginx -n kube-system || true
	@echo "\nNote: IAM roles and policies need to be cleaned up manually:"
	@echo "  - eksctl delete iamserviceaccount --cluster=$(CLUSTER_NAME) --region=$(REGION) --name=aws-load-balancer-controller --namespace=kube-system"
	@echo "  - aws iam delete-policy --policy-arn arn:aws:iam::\$$(aws sts get-caller-identity --query Account --output text):policy/AWSLoadBalancerControllerIAMPolicy-work"
	@echo "Uninstallation complete."

# Cluster lifecycle management
create-cluster:
	@echo "Creating work cluster..."
	@cd 1-eks && make create-cluster

delete-cluster:
	@echo "Deleting work cluster..."
	@cd 1-eks && make delete-cluster

update-kubeconfig:
	@echo "Updating kubeconfig for work cluster..."
	@cd 1-eks && make update-kubeconfig

get-cluster-info:
	@echo "Getting cluster information..."
	@echo "Cluster: $(CLUSTER_NAME)"
	@echo "Region: $(REGION)"
	@kubectl get nodes -o wide
	@echo "\nNode groups:"
	@kubectl get nodes --show-labels | grep node-type

help:
	@echo "Available commands:"
	@echo ""
	@echo "Cluster Management:"
	@echo "  create-cluster              - Create the EKS cluster"
	@echo "  delete-cluster              - Delete the EKS cluster"
	@echo "  update-kubeconfig           - Update local kubeconfig"
	@echo "  get-cluster-info            - Show cluster and node information"
	@echo ""
	@echo "Infrastructure Setup:"
	@echo "  setup-kube                  - Deploy all components (observability, contrast, deployer)"
	@echo "  deploy-aws-load-balancer-controller - Install AWS Load Balancer Controller"
	@echo "  deploy-nginx                - Deploy NGINX ingress controller (includes AWS LB Controller)"
	@echo "  configure-dns               - Configure Route53 DNS records"
	@echo "  deploy-observability-stack  - Deploy FluentBit, Falco, OpenSearch"
	@echo "  deploy-contrast             - Deploy Contrast Agent Operator"
	@echo "  deploy-workshop-deployer    - Deploy Workshop Deployer"
	@echo ""
	@echo "Cleanup:"
	@echo "  uninstall                   - Remove all deployed components"
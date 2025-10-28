# Workshop Deployer Helm Chart

This Helm chart deploys the FastAPI service that creates the next demoN namespace and triggers `make demo-up` inside it.

## Usage

```bash
		helm upgrade --install workshop-deployer 5-workshop-deployer/k8s \
		--namespace setup \
		--create-namespace \
		--set secret.apiKey="$(DEPLOYER_API_KEY)" \
		--set ingress.host="setup.$(TLD)";
```


After deployment:
```bash
curl -s -X POST -H "X-API-Key: change-me" http://setup.workshop.contrastdemo.com/workshops/next'
```

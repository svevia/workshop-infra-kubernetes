# Working
This deploys the contrast agent operator

Still need to set the following: 
```shell
kubectl -n contrast-agent-operator delete secret default-agent-connection-secret --ignore-not-found
	kubectl -n contrast-agent-operator create secret generic default-agent-connection-secret --from-literal=token=$(CONTRAST__AGENT__TOKEN)
```

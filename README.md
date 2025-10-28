# Work Infrastructure Kubernetes# Work Infrastructure Kubernetes - Gestion Manuelle des Noeuds# Work Infrastructure Kubernetes - Manual Node Management# Workshop Infra Kubernetes



This is the infrastructure as code to deploy a Kubernetes cluster on AWS EKS with an observability stack and Contrast Security Agent Operator. This setup is designed for workshops and demos, where each workshop participant gets their own namespace with a dedicated node.



## Key FeaturesCeci est une version avec gestion manuelle des noeuds de l'infrastructure workshop, conçue pour fonctionner en parallèle de la configuration `workshop-infra-kubernetes` originale. La différence clé est que ce cluster utilise une gestion manuelle des noeuds avec la capacité d'assigner des noeuds dédiés à des namespaces spécifiques.



- **Manual EKS Cluster**: Full control over node groups with manual scaling

- **Dedicated Node Assignment**: Each workshop namespace gets its own EC2 node (m5.2xlarge - 8 vCPU, 32GB RAM)

- **API-Based Management**: Workshop Deployer API handles all node scaling and assignment automatically## Caractéristiques PrincipalesThis is a manual node management version of the workshop infrastructure, designed to run alongside the original `workshop-infra-kubernetes` setup. The key difference is that this cluster uses manual node management with the ability to assign dedicated nodes to specific namespaces.This repository contains the infrastructure as code to deploy a Kubernetes cluster on AWS with an observability stack 

- **Scale-In Protection**: Nodes with active workloads are protected from termination

- **System/Workshop Node Separation**: Infrastructure components run on dedicated system nodes

- **DNS Domain**: Uses `work.contrastdemo.com` for all ingress routes

- **Integrated Observability**: OpenSearch, FluentBit, and Falco for monitoring and security- **Cluster EKS Manuel**: Pas de mode auto - contrôle total des groupes de noeudsand Contrast Security Agent Operator. This setup is used for workshops and demos, where each workshop user will have



## Prerequisites-


- [AWS CLI](https://aws.amazon.com/cli/)- **DNS Séparé**: Utilise le domaine `work.contrastdemo.com`## Key Featurestheir own namespace and access to an application for testing with Contrast. 

- [eksctl](https://eksctl.io/installation/)

- [kubectl](https://kubernetes.io/docs/tasks/tools/)- **Séparation Système/Workshop**: Les composants d'infrastructure tournent sur les noeuds système

- [Helm](https://helm.sh/docs/intro/install/)

- Admin level AWS role configured to login with `aws sso login`- **Assignment Automatique**: Scripts et watchers pour l'assignment automatisé des noeuds



## Quick Deployment Steps



**Manual EKS Cluster**: No auto mode - full control over node groups

# 1. Setup environment

cp .env.template .env
aws configure sso

# Edit .env with your Contrast credentials and API keys

- **Dedicated Node Assignment**: 1 node per namespace capability 

# 2. Create the cluster (15-20 minutes)

make create-cluster
make update-kubeconfig

 **Separate DNS**: Uses `workshop.contrastdemo.com` domain

# 3. Add storage class

cd 1-eks && make add-storage-class && cd ..

# 4. Deploy NGINX ingress and configure DNS (one-time setup)

make deploy-nginx
make configure-dns



# 5. Deploy all infrastructure components## Étapes de Déploiement Rapide- **Automatic Node Assignment**: Scripts and watchers for automated node assignment- Admin level AWS role configured to login with `aws sso login`

make setup-kube


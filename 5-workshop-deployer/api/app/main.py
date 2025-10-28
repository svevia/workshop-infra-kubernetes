import os
import re
import subprocess
import base64
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Body
from pydantic import BaseModel
from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.client.rest import ApiException as RestApiException

API_KEY = os.getenv("API_KEY", "change-me")
WORKDIR = os.getenv("WORKDIR", "/cargo-cats")  
NS_PREFIX = os.getenv("NS_PREFIX", "demo")
DEFAULT_CONTRAST_URL = "https://eval.contrastsecurity.com/Contrast"

# Global variables for default Contrast credentials
DEFAULT_AGENT_TOKEN = None
DEFAULT_API_KEY = None
DEFAULT_AUTH_HEADER = None

app = FastAPI(title="Workshop Deployer API", version="1.0.0")

try:
    config.load_incluster_config()
except Exception:
    try:
        config.load_kube_config()
    except Exception:
        pass

core = client.CoreV1Api()
apps = client.AppsV1Api()
custom_objects = client.CustomObjectsApi()

class WorkshopCreateRequest(BaseModel):
    namespace: str
    agent_token: Optional[str] = None
    user_api_key: Optional[str] = None
    user_auth: Optional[str] = None

@app.on_event("startup")
async def load_default_credentials():
    """Load default Contrast credentials from secrets at startup"""
    global DEFAULT_AGENT_TOKEN, DEFAULT_API_KEY, DEFAULT_AUTH_HEADER
    
    try:
        DEFAULT_AGENT_TOKEN = get_secret_value("contrast-agent-secret", "token")
        print("✓ Loaded default agent token from contrast-agent-secret")
    except Exception as e:
        print(f"⚠ Warning: Could not load default agent token: {e}")
    
    try:
        DEFAULT_API_KEY = get_secret_value("contrast-api-secret", "api_key")
        print("✓ Loaded default API key from contrast-api-secret")
    except Exception as e:
        print(f"⚠ Warning: Could not load default API key: {e}")
    
    try:
        DEFAULT_AUTH_HEADER = get_secret_value("contrast-api-secret", "auth_header")
        print("✓ Loaded default auth header from contrast-api-secret")
    except Exception as e:
        print(f"⚠ Warning: Could not load default auth header: {e}")

def detect_namespace():
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            return f.read().strip()
    except FileNotFoundError:
        pass
    #fallback to default    
    return "default"

def get_secret_value(secret_name: str, key: str) -> str:
    secret = core.read_namespaced_secret(secret_name, detect_namespace())
    encoded_value = secret.data.get(key)
    if not encoded_value:
        raise KeyError(f"Key '{key}' not found in secret '{secret_name}'")
    return base64.b64decode(encoded_value).decode("utf-8")

def require_api_key(x_api_key: Optional[str]):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

def validate_namespace_name(namespace: str):
    """Validate that namespace follows Kubernetes naming conventions"""
    # Kubernetes namespace must be lowercase RFC 1123 label:
    # - lowercase alphanumeric characters or '-'
    # - must start and end with an alphanumeric character
    # - max 63 characters
    pattern = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
    
    if not namespace:
        raise HTTPException(status_code=400, detail="Namespace cannot be empty")
    
    if len(namespace) > 63:
        raise HTTPException(status_code=400, detail="Namespace must be 63 characters or less")
    
    if not pattern.match(namespace):
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid namespace '{namespace}'. Must be lowercase alphanumeric characters or '-', "
                   f"and must start and end with an alphanumeric character (e.g. 'my-name' or '123-abc')"
        )
    
    return namespace.lower()


def set_env_variable(env, namespace, node_name: str, agent_token: Optional[str] = None, user_api_key: Optional[str] = None, user_auth: Optional[str] = None):
    """Set environment variables for the workshop deployment"""
    env["NAMESPACE"] = namespace
    env["CONTRAST__UNIQ__NAME"] = namespace
    env["NODE_NAME"] = node_name
    
    # Use provided parameters or fall back to defaults
    env["CONTRAST__AGENT__TOKEN"] = agent_token or DEFAULT_AGENT_TOKEN or ""
    env["CONTRAST__API__KEY"] = user_api_key or DEFAULT_API_KEY or ""
    env["CONTRAST__API__AUTHORIZATION"] = user_auth or DEFAULT_AUTH_HEADER or ""

    return env

def setup_contrast_resources(namespace: str, agent_token: Optional[str] = None):
    """Create namespace, scale up node group, add dedicated node, secret, and AgentConnection for a workshop"""
    # Use provided token or default
    token = agent_token or DEFAULT_AGENT_TOKEN
    
    if not token:
        raise ValueError("No agent token provided and no default token available")
    
    # Create namespace with labels for workshop isolation
    create_namespace(namespace)
    
    # Scale up node group and label new node for this namespace
    node_name = scale_up_node_group_and_label_node(namespace)
    
    # Create secret with agent token
    secret_name = create_agent_connection_secret(namespace, token)
    
    # Create AgentConnection
    create_agent_connection(namespace, secret_name)
    
    return secret_name, node_name

def create_namespace(namespace: str):
    """Create a Kubernetes namespace if it doesn't exist"""
    ns = client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=namespace,
            labels={
                "workshop-namespace": "true",
                "dedicated-node-required": "true"
            }
        )
    )
    
    try:
        core.create_namespace(body=ns)
        print(f"Created namespace {namespace}")
    except RestApiException as e:
        if e.status == 409:  # Already exists
            print(f"Namespace {namespace} already exists")
        else:
            raise

def scale_up_node_group_and_label_node(namespace: str) -> str:
    """
    Scale up the workshop node group (via Auto Scaling Group) to add a new node and label it with the namespace name.
    Returns the node name.
    """
    import time
    
    # Get current workshop nodes
    workshop_nodes = core.list_node(label_selector="node-type=workshop")
    current_count = len(workshop_nodes.items)
    
    print(f"Current workshop nodes: {current_count}")
    
    # Find the Auto Scaling Group for workshop nodes
    cluster_name = os.getenv("CLUSTER_NAME", "workshop-cluster")
    region = os.getenv("AWS_REGION", "eu-west-1")
    
    # Get the ASG name for workshop-nodes
    # eksctl creates ASGs with tags: alpha.eksctl.io/nodegroup-name = workshop-nodes
    print(f"Finding Auto Scaling Group for workshop-nodes...")
    
    asg_result = subprocess.run(
        [
            "aws", "autoscaling", "describe-auto-scaling-groups",
            "--region", region,
            "--query", "AutoScalingGroups[?Tags[?Key=='alpha.eksctl.io/nodegroup-name' && Value=='workshop-nodes']].AutoScalingGroupName",
            "--output", "text"
        ],
        capture_output=True,
        text=True
    )
    
    if asg_result.returncode != 0:
        raise Exception(f"Failed to find Auto Scaling Group: {asg_result.stderr}")
    
    asg_name = asg_result.stdout.strip()
    if not asg_name:
        raise Exception("Could not find Auto Scaling Group for workshop-nodes")
    
    print(f"Found ASG: {asg_name}")
    
    # Calculate desired capacity
    desired_capacity = current_count + 1
    
    print(f"Scaling Auto Scaling Group to {desired_capacity} nodes...")
    
    # Scale the ASG
    scale_result = subprocess.run(
        [
            "aws", "autoscaling", "set-desired-capacity",
            "--auto-scaling-group-name", asg_name,
            "--desired-capacity", str(desired_capacity),
            "--region", region
        ],
        capture_output=True,
        text=True
    )
    
    if scale_result.returncode != 0:
        raise Exception(f"Failed to scale Auto Scaling Group: {scale_result.stderr}")
    
    print(f"Auto Scaling Group scaling initiated. Waiting for new node to join the cluster...")
    
    # Wait for the new node to appear (optimized: check more frequently, shorter timeout)
    timeout = 180  # Reduced from 300 to 180 seconds
    start_time = time.time()
    new_node_name = None
    
    while time.time() - start_time < timeout:
        time.sleep(5)  # Reduced from 10 to 5 seconds
        current_nodes = core.list_node(label_selector="node-type=workshop")
        
        if len(current_nodes.items) > current_count:
            # Find the newest node (the one without a dedicated-namespace label)
            for node in current_nodes.items:
                node_labels = node.metadata.labels or {}
                if "dedicated-namespace" not in node_labels:
                    new_node_name = node.metadata.name
                    break
            
            if new_node_name:
                print(f"New node joined: {new_node_name}")
                break
    
    if not new_node_name:
        raise Exception("Timeout waiting for new node to join the cluster")
    
    # Label the new node with the namespace
    print(f"Labeling node {new_node_name} with dedicated-namespace={namespace}")
    
    node_patch = {
        "metadata": {
            "labels": {
                "dedicated-namespace": namespace,
                "workshop-namespace": namespace
            }
        }
    }
    
    core.patch_node(new_node_name, node_patch)
    
    # Protect the node from scale-in
    print(f"Protecting node from scale-in...")
    
    # Get the instance ID from the node
    new_node = core.read_node(new_node_name)
    provider_id = new_node.spec.provider_id
    if provider_id:
        instance_id = provider_id.split('/')[-1]
        print(f"Instance ID: {instance_id}")
        
        # Set scale-in protection
        protect_result = subprocess.run(
            [
                "aws", "autoscaling", "set-instance-protection",
                "--instance-ids", instance_id,
                "--auto-scaling-group-name", asg_name,
                "--protected-from-scale-in",
                "--region", region
            ],
            capture_output=True,
            text=True
        )
        
        if protect_result.returncode != 0:
            print(f"Warning: Failed to set scale-in protection: {protect_result.stderr}")
        else:
            print(f"Node {new_node_name} is now protected from scale-in")
    
    print(f"Successfully scaled and labeled node {new_node_name} for namespace {namespace}")
    
    return new_node_name

def create_agent_connection_secret(namespace: str, token: str):
    """Create a Kubernetes secret for the agent token"""
    secret_name = f"{namespace}-agent-connection-secret"
    
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=secret_name,
            namespace="contrast-agent-operator"
        ),
        type="Opaque",
        string_data={
            "token": token
        }
    )
    
    try:
        core.create_namespaced_secret(namespace="contrast-agent-operator", body=secret)
        print(f"Created secret {secret_name} in namespace contrast-agent-operator")
    except RestApiException as e:
        if e.status == 409:  # Already exists
            print(f"Secret {secret_name} already exists, updating...")
            core.patch_namespaced_secret(name=secret_name, namespace="contrast-agent-operator", body=secret)
        else:
            raise
    
    return secret_name

def create_agent_connection(namespace: str, secret_name: str):
    """Create a ClusterAgentConnection custom resource"""
    agent_connection = {
        "apiVersion": "agents.contrastsecurity.com/v1beta1",
        "kind": "ClusterAgentConnection",
        "metadata": {
            "name": f"{namespace}-agent-connection",
            "namespace": "contrast-agent-operator"
        },
        "spec": {
            "namespaces": [namespace],
            "template": {
                "spec": {
                    "token": {
                        "secretName": secret_name,
                        "secretKey": "token"
                    },
                    "mountAsVolume": False
                }
            }
        }
    }
    
    try:
        custom_objects.create_namespaced_custom_object(
            group="agents.contrastsecurity.com",
            version="v1beta1",
            namespace="contrast-agent-operator",
            plural="clusteragentconnections",
            body=agent_connection
        )
        print(f"Created ClusterAgentConnection {namespace}-agent-connection in contrast-agent-operator namespace")
    except RestApiException as e:
        if e.status == 409:  # Already exists
            print(f"ClusterAgentConnection {namespace}-agent-connection already exists, updating...")
            custom_objects.patch_namespaced_custom_object(
                group="agents.contrastsecurity.com",
                version="v1beta1",
                namespace="contrast-agent-operator",
                plural="clusteragentconnections",
                name=f"{namespace}-agent-connection",
                body=agent_connection
            )
        else:
            raise

def find_next_namespace(prefix: str) -> str:
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    max_n = 0
    try:
        ns_list = core.list_namespace()
        for ns in ns_list.items:
            name = ns.metadata.name or ""
            m = pattern.match(name)
            if m:
                try:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
                except ValueError:
                    continue
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"Kubernetes API error: {e}")

    return f"{prefix}{max_n + 1}"


@app.post("/workshops/next")
def create_next_workshop(x_api_key: Optional[str] = Header(None)):
    require_api_key(x_api_key)

    next_ns = find_next_namespace(NS_PREFIX)

    # Setup Contrast resources with defaults
    try:
        secret_name, node_name = setup_contrast_resources(next_ns)
    except Exception as e:
        print(f"Error creating Contrast resources: {e}")
        raise HTTPException(status_code=500, detail=f"Error creating Contrast resources: {str(e)}")

    env = os.environ.copy()
    env = set_env_variable(env, next_ns, node_name)

    try:
        result = subprocess.run(
            ["make", "demo-up"],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return {
            "status": "ok",
            "namespace": next_ns,
            "node_name": node_name,
            "stdout": result.stdout,
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail={
            "namespace": next_ns,
            "stderr": e.stderr,
            "returncode": e.returncode,
        })

@app.post("/workshops/create")
def create_workshop(x_api_key: Optional[str] = Header(None), namespace: Optional[str] = None):
    require_api_key(x_api_key)

    if namespace is None:
        raise HTTPException(status_code=400, detail="'namespace' parameter is required")

    # Validate namespace name
    validated_namespace = validate_namespace_name(namespace)

    # Setup Contrast resources with defaults
    try:
        secret_name, node_name = setup_contrast_resources(validated_namespace)
    except Exception as e:
        print(f"Error creating Contrast resources: {e}")
        raise HTTPException(status_code=500, detail=f"Error creating Contrast resources: {str(e)}")

    env = os.environ.copy()
    env = set_env_variable(env, validated_namespace, node_name)

    try:
        result = subprocess.run(
            ["make", "demo-up"],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return {
            "status": "ok",
            "namespace": validated_namespace,
            "node_name": node_name,
            "stdout": result.stdout,
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail={
            "namespace": validated_namespace,
            "stderr": e.stderr,
            "returncode": e.returncode,
        })

@app.post("/workshops/create-with-params")
def create_workshop_with_params(
    request: WorkshopCreateRequest = Body(...),
    x_api_key: Optional[str] = Header(None)
):
    require_api_key(x_api_key)

    # Validate and normalize namespace name
    validated_namespace = validate_namespace_name(request.namespace)

    # Setup Contrast resources with provided or default values
    try:
        secret_name, node_name = setup_contrast_resources(
            validated_namespace,
            agent_token=request.agent_token
        )
    except Exception as e:
        print(f"Error creating Contrast resources: {e}")
        raise HTTPException(status_code=500, detail=f"Error creating Contrast resources: {str(e)}")

    env = os.environ.copy()
    env = set_env_variable(
        env, 
        validated_namespace,
        node_name,
        agent_token=request.agent_token,
        user_api_key=request.user_api_key,
        user_auth=request.user_auth
    )

    try:
        result = subprocess.run(
            ["make", "demo-up"],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return {
            "status": "ok",
            "namespace": validated_namespace,
            "node_name": node_name,
            "stdout": result.stdout,
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail={
            "namespace": validated_namespace,
            "stderr": e.stderr,
            "returncode": e.returncode,
        })

@app.delete("/workshops/delete")
def delete_workshop(x_api_key: Optional[str] = Header(None), namespace: Optional[str] = None):
    require_api_key(x_api_key)

    if namespace is None:
        raise HTTPException(status_code=500, detail=f"'namespace' parameter is required")

    try:
        # First, find the node dedicated to this namespace
        print(f"Finding node for namespace {namespace}...")
        nodes = core.list_node(label_selector=f"dedicated-namespace={namespace}")
        
        node_name = None
        instance_id = None
        
        if nodes.items:
            node = nodes.items[0]
            node_name = node.metadata.name
            
            # Extract instance ID from providerID (format: aws:///region/instance-id)
            provider_id = node.spec.provider_id
            if provider_id:
                instance_id = provider_id.split('/')[-1]
            
            print(f"Found node {node_name} (instance: {instance_id})")
        
        # Delete the namespace
        print(f"Deleting namespace {namespace}...")
        result = subprocess.run(
            ["kubectl", "delete", "namespace", namespace],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            check=True,
        )
        
        # If we found a dedicated node, drain and terminate it
        if node_name and instance_id:
            region = os.getenv("AWS_REGION", "eu-west-1")
            
            # Get the ASG name
            asg_result = subprocess.run(
                [
                    "aws", "autoscaling", "describe-auto-scaling-groups",
                    "--region", region,
                    "--query", "AutoScalingGroups[?Tags[?Key=='alpha.eksctl.io/nodegroup-name' && Value=='workshop-nodes']].AutoScalingGroupName",
                    "--output", "text"
                ],
                capture_output=True,
                text=True
            )
            
            asg_name = asg_result.stdout.strip()
            
            # Remove scale-in protection first
            if asg_name:
                print(f"Removing scale-in protection from instance {instance_id}...")
                unprotect_result = subprocess.run(
                    [
                        "aws", "autoscaling", "set-instance-protection",
                        "--instance-ids", instance_id,
                        "--auto-scaling-group-name", asg_name,
                        "--no-protected-from-scale-in",
                        "--region", region
                    ],
                    capture_output=True,
                    text=True
                )
                
                if unprotect_result.returncode != 0:
                    print(f"Warning: Failed to remove scale-in protection: {unprotect_result.stderr}")
            
            print(f"Draining node {node_name}...")
            
            # Drain the node
            drain_result = subprocess.run(
                ["kubectl", "drain", node_name, "--ignore-daemonsets", "--delete-emptydir-data", "--force", "--timeout=120s"],
                capture_output=True,
                text=True
            )
            
            if drain_result.returncode != 0:
                print(f"Warning: Failed to drain node: {drain_result.stderr}")
            
            # Terminate the EC2 instance
            print(f"Terminating instance {instance_id}...")
            
            terminate_result = subprocess.run(
                ["aws", "ec2", "terminate-instances", "--instance-ids", instance_id, "--region", region],
                capture_output=True,
                text=True
            )
            
            if terminate_result.returncode != 0:
                print(f"Warning: Failed to terminate instance: {terminate_result.stderr}")
            
            # Update ASG desired capacity
            if asg_name:
                print(f"Updating Auto Scaling Group desired capacity...")
                
                # Get current ASG settings
                asg_info_result = subprocess.run(
                    [
                        "aws", "autoscaling", "describe-auto-scaling-groups",
                        "--auto-scaling-group-names", asg_name,
                        "--region", region,
                        "--query", "AutoScalingGroups[0].DesiredCapacity",
                        "--output", "text"
                    ],
                    capture_output=True,
                    text=True
                )
                
                current_capacity = int(asg_info_result.stdout.strip())
                new_capacity = max(0, current_capacity - 1)  # Ensure at least 1 node remains
                
                # Update ASG desired capacity
                subprocess.run(
                    [
                        "aws", "autoscaling", "set-desired-capacity",
                        "--auto-scaling-group-name", asg_name,
                        "--desired-capacity", str(new_capacity),
                        "--region", region
                    ],
                    capture_output=True,
                    text=True
                )
                
                print(f"Updated ASG desired capacity to {new_capacity}")
        
        return {
            "status": "ok",
            "namespace": namespace,
            "node_deleted": node_name,
            "instance_terminated": instance_id,
            "stdout": result.stdout,
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail={
            "namespace": namespace,
            "stderr": e.stderr,
            "returncode": e.returncode,
        })


@app.get("/healthz")
def healthz():
    return {"status": "ok"}

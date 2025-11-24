import os
import re
import subprocess
import base64
import asyncio
import uuid
from typing import Optional, Dict
from datetime import datetime
from enum import Enum
from functools import partial
from concurrent.futures import ThreadPoolExecutor

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

# Global lock to serialize workshop creation and deletion operations
workshop_operation_lock = asyncio.Lock()

# Lock specifically for namespace allocation to prevent race conditions
namespace_allocation_lock = asyncio.Lock()

# Thread pool for running blocking operations
# Keep this small (3-5) since operations are serialized by the lock
# Increase only if you see thread pool exhaustion in logs
executor = ThreadPoolExecutor(max_workers=5)

# Job status tracking
class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class JobInfo(BaseModel):
    job_id: str
    status: JobStatus
    namespace: Optional[str] = None
    node_name: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    stdout: Optional[str] = None

# In-memory job store (in production, use Redis or a database)
jobs: Dict[str, JobInfo] = {}

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

class CreateReservedNodesRequest(BaseModel):
    count: int

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

async def setup_contrast_resources(namespace: str, agent_token: Optional[str] = None):
    """Create namespace, scale up node group, add dedicated node, secret, and AgentConnection for a workshop"""
    # Use provided token or default
    token = agent_token or DEFAULT_AGENT_TOKEN
    
    if not token:
        raise ValueError("No agent token provided and no default token available")
    
    # Create namespace with labels for workshop isolation
    create_namespace(namespace)
    
    # Scale up node group and label new node for this namespace
    node_name = await scale_up_node_group_and_label_node(namespace)
    
    # Create secret with agent token
    secret_name = create_agent_connection_secret(namespace, token)
    
    # Create AgentConnection
    create_agent_connection(namespace, secret_name)
    
    return secret_name, node_name

def find_available_reserved_node() -> Optional[str]:
    """Find a node with node-status=reserved label (pre-created node waiting for assignment)"""
    try:
        nodes = core.list_node(label_selector="node-type=workshop,node-status=reserved")
        if nodes.items:
            node = nodes.items[0]
            node_name = node.metadata.name
            print(f"Found available reserved node: {node_name}")
            return node_name
        print("No reserved nodes available")
        return None
    except Exception as e:
        print(f"Error finding reserved node: {e}")
        return None

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

async def scale_up_node_group_and_label_node(namespace: str) -> str:
    """
    Scale up the workshop node group (via Auto Scaling Group) to add a new node and label it with the namespace name.
    First checks for available reserved nodes before creating a new one.
    Returns the node name.
    """
    # Check if there's a reserved node available
    reserved_node = find_available_reserved_node()
    if reserved_node:
        print(f"Using reserved node {reserved_node} for namespace {namespace}")
        # Update the node labels to mark it as in-use
        node_patch = {
            "metadata": {
                "labels": {
                    "dedicated-namespace": namespace,
                    "workshop-namespace": namespace,
                    "node-status": "in-use"
                }
            }
        }
        core.patch_node(reserved_node, node_patch)
        print(f"Successfully assigned reserved node {reserved_node} to namespace {namespace}")
        return reserved_node
    
    # No reserved node available, create a new one
    print("No reserved nodes available, creating new node...")
    
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
    
    asg_result = await run_subprocess_async(
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
    scale_result = await run_subprocess_async(
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
    start_time = asyncio.get_event_loop().time()
    new_node_name = None
    
    while asyncio.get_event_loop().time() - start_time < timeout:
        await asyncio.sleep(5)  # Non-blocking async sleep
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
                "workshop-namespace": namespace,
                "node-status": "in-use"
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
        protect_result = await run_subprocess_async(
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

async def delete_reserved_nodes(count: int) -> list[str]:
    """
    Delete N reserved nodes by draining, terminating, and updating ASG.
    Returns list of deleted node names.
    """
    deleted_nodes = []
    region = os.getenv("AWS_REGION", "eu-west-1")
    
    # Get reserved nodes
    reserved_nodes = core.list_node(label_selector="node-type=workshop,node-status=reserved")
    available_count = len(reserved_nodes.items)
    
    if available_count == 0:
        raise Exception("No reserved nodes available to delete")
    
    if count > available_count:
        raise Exception(f"Cannot delete {count} nodes, only {available_count} reserved nodes available")
    
    print(f"Deleting {count} reserved nodes out of {available_count} available")
    
    # Find the Auto Scaling Group
    asg_result = await run_subprocess_async(
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
    
    # Delete nodes one by one
    for i in range(count):
        if i >= len(reserved_nodes.items):
            break
        
        node = reserved_nodes.items[i]
        node_name = node.metadata.name
        
        # Get instance ID
        provider_id = node.spec.provider_id
        if not provider_id:
            print(f"Warning: Node {node_name} has no provider ID, skipping...")
            continue
        
        instance_id = provider_id.split('/')[-1]
        print(f"Deleting node {node_name} (instance: {instance_id})...")
        
        try:
            # Remove scale-in protection
            print(f"  Removing scale-in protection from instance {instance_id}...")
            await run_subprocess_async(
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
            
            # Drain the node
            print(f"  Draining node {node_name}...")
            await run_subprocess_async(
                ["kubectl", "drain", node_name, "--ignore-daemonsets", "--delete-emptydir-data", "--force", "--timeout=120s"],
                capture_output=True,
                text=True
            )
            
            # Terminate the instance
            print(f"  Terminating instance {instance_id}...")
            await run_subprocess_async(
                ["aws", "ec2", "terminate-instances", "--instance-ids", instance_id, "--region", region],
                capture_output=True,
                text=True
            )
            
            deleted_nodes.append(node_name)
            print(f"  ✓ Successfully deleted node {node_name}")
            
        except Exception as e:
            print(f"  ✗ Failed to delete node {node_name}: {e}")
            # Continue with other nodes
    
    # Update ASG desired capacity
    if deleted_nodes:
        print(f"Updating Auto Scaling Group desired capacity...")
        asg_info_result = await run_subprocess_async(
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
        new_capacity = max(0, current_capacity - len(deleted_nodes))
        
        await run_subprocess_async(
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
    
    print(f"Successfully deleted {len(deleted_nodes)} reserved nodes")
    return deleted_nodes

async def create_reserved_nodes(count: int) -> list[str]:
    """
    Create N reserved nodes by scaling up the ASG and labeling them as reserved.
    Returns list of created node names.
    """
    created_nodes = []
    region = os.getenv("AWS_REGION", "eu-west-1")
    
    # Get current workshop nodes
    workshop_nodes = core.list_node(label_selector="node-type=workshop")
    current_count = len(workshop_nodes.items)
    
    print(f"Current workshop nodes: {current_count}, creating {count} reserved nodes")
    
    # Find the Auto Scaling Group
    asg_result = await run_subprocess_async(
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
    
    # Scale up by count
    desired_capacity = current_count + count
    
    print(f"Scaling Auto Scaling Group to {desired_capacity} nodes...")
    
    scale_result = await run_subprocess_async(
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
    
    print(f"Waiting for {count} new nodes to join...")
    
    # Wait for new nodes to appear
    timeout = 300
    start_time = asyncio.get_event_loop().time()
    
    while asyncio.get_event_loop().time() - start_time < timeout:
        await asyncio.sleep(5)
        current_nodes = core.list_node(label_selector="node-type=workshop")
        
        if len(current_nodes.items) >= desired_capacity:
            # Find nodes without dedicated-namespace or node-status labels (new unlabeled nodes)
            for node in current_nodes.items:
                node_labels = node.metadata.labels or {}
                if "node-status" not in node_labels and node.metadata.name not in created_nodes:
                    new_node_name = node.metadata.name
                    created_nodes.append(new_node_name)
                    
                    # Label as reserved
                    print(f"Labeling node {new_node_name} as reserved")
                    node_patch = {
                        "metadata": {
                            "labels": {
                                "node-status": "reserved"
                            }
                        }
                    }
                    core.patch_node(new_node_name, node_patch)
                    
                    # Protect from scale-in
                    new_node = core.read_node(new_node_name)
                    provider_id = new_node.spec.provider_id
                    if provider_id:
                        instance_id = provider_id.split('/')[-1]
                        await run_subprocess_async(
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
                        print(f"Node {new_node_name} protected from scale-in")
            
            if len(created_nodes) >= count:
                print(f"Successfully created {len(created_nodes)} reserved nodes")
                return created_nodes
    
    if len(created_nodes) < count:
        raise Exception(f"Timeout: only created {len(created_nodes)} of {count} requested nodes")
    
    return created_nodes

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


async def run_subprocess_async(*args, **kwargs):
    """Run subprocess in thread pool to avoid blocking the event loop"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, partial(subprocess.run, *args, **kwargs))


async def execute_workshop_creation(
    job_id: str,
    namespace: str,
    agent_token: Optional[str] = None,
    user_api_key: Optional[str] = None,
    user_auth: Optional[str] = None
):
    """Background task to create a workshop"""
    async with workshop_operation_lock:
        jobs[job_id].status = JobStatus.RUNNING
        jobs[job_id].started_at = datetime.utcnow().isoformat()
        print(f"🔒 Lock acquired for job {job_id} (namespace: {namespace})")
        
        try:
            # Setup Contrast resources
            secret_name, node_name = await setup_contrast_resources(namespace, agent_token=agent_token)
            jobs[job_id].node_name = node_name
            
            # Set environment variables
            env = os.environ.copy()
            env = set_env_variable(env, namespace, node_name, agent_token, user_api_key, user_auth)
            
            # Deploy the workshop (run in thread pool to avoid blocking)
            result = await run_subprocess_async(
                ["make", "demo-up"],
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            
            # Update job status
            jobs[job_id].status = JobStatus.COMPLETED
            jobs[job_id].completed_at = datetime.utcnow().isoformat()
            jobs[job_id].stdout = result.stdout
            
            print(f"🔓 Lock released for job {job_id} - SUCCESS")
            
        except Exception as e:
            jobs[job_id].status = JobStatus.FAILED
            jobs[job_id].completed_at = datetime.utcnow().isoformat()
            jobs[job_id].error = str(e)
            print(f"🔓 Lock released for job {job_id} - FAILED: {e}")


async def execute_workshop_deletion(job_id: str, namespace: str):
    """Background task to delete a workshop"""
    async with workshop_operation_lock:
        jobs[job_id].status = JobStatus.RUNNING
        jobs[job_id].started_at = datetime.utcnow().isoformat()
        print(f"🔒 Lock acquired for deletion job {job_id} (namespace: {namespace})")
        
        try:
            # Find the node dedicated to this namespace
            print(f"Finding node for namespace {namespace}...")
            nodes = core.list_node(label_selector=f"dedicated-namespace={namespace}")
            
            node_name = None
            instance_id = None
            
            if nodes.items:
                node = nodes.items[0]
                node_name = node.metadata.name
                
                provider_id = node.spec.provider_id
                if provider_id:
                    instance_id = provider_id.split('/')[-1]
                
                print(f"Found node {node_name} (instance: {instance_id})")
            
            jobs[job_id].node_name = node_name
            
            # Delete the namespace (run in thread pool to avoid blocking)
            print(f"Deleting namespace {namespace}...")
            result = await run_subprocess_async(
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
                asg_result = await run_subprocess_async(
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
                
                # Remove scale-in protection
                if asg_name:
                    print(f"Removing scale-in protection from instance {instance_id}...")
                    await run_subprocess_async(
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
                
                # Drain the node
                print(f"Draining node {node_name}...")
                await run_subprocess_async(
                    ["kubectl", "drain", node_name, "--ignore-daemonsets", "--delete-emptydir-data", "--force", "--timeout=120s"],
                    capture_output=True,
                    text=True
                )
                
                # Terminate the instance
                print(f"Terminating instance {instance_id}...")
                await run_subprocess_async(
                    ["aws", "ec2", "terminate-instances", "--instance-ids", instance_id, "--region", region],
                    capture_output=True,
                    text=True
                )
                
                # Update ASG desired capacity
                if asg_name:
                    print(f"Updating Auto Scaling Group desired capacity...")
                    asg_info_result = await run_subprocess_async(
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
                    new_capacity = max(0, current_capacity - 1)
                    
                    await run_subprocess_async(
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
            
            # Update job status
            jobs[job_id].status = JobStatus.COMPLETED
            jobs[job_id].completed_at = datetime.utcnow().isoformat()
            jobs[job_id].stdout = result.stdout
            
            print(f"🔓 Lock released for deletion job {job_id} - SUCCESS")
            
        except Exception as e:
            jobs[job_id].status = JobStatus.FAILED
            jobs[job_id].completed_at = datetime.utcnow().isoformat()
            jobs[job_id].error = str(e)
            print(f"🔓 Lock released for deletion job {job_id} - FAILED: {e}")


@app.post("/workshops/next")
async def create_next_workshop(
    x_api_key: Optional[str] = Header(None)
):
    require_api_key(x_api_key)

    # Protect namespace allocation from race conditions
    async with namespace_allocation_lock:
        next_ns = find_next_namespace(NS_PREFIX)
        job_id = str(uuid.uuid4())
        
        # Create job entry immediately while holding the lock
        jobs[job_id] = JobInfo(
            job_id=job_id,
            status=JobStatus.QUEUED,
            namespace=next_ns,
            created_at=datetime.utcnow().isoformat()
        )
        
        # Also create the namespace immediately to reserve it
        create_namespace(next_ns)
        
        print(f"✨ Created job {job_id} for namespace {next_ns} (namespace reserved)")
    
    # Fire and forget - start task in background without waiting (outside the lock)
    asyncio.create_task(execute_workshop_creation(job_id, next_ns))
    
    return {
        "status": "accepted",
        "job_id": job_id,
        "namespace": next_ns,
        "message": f"Workshop creation started. Use GET /workshops/status/{job_id} to check progress.",
        "url": f"http://console.{next_ns}.workshop.contrastdemo.com/"
    }

@app.post("/workshops/create")
async def create_workshop(
    x_api_key: Optional[str] = Header(None),
    namespace: Optional[str] = None
):
    require_api_key(x_api_key)

    if namespace is None:
        raise HTTPException(status_code=400, detail="'namespace' parameter is required")

    validated_namespace = validate_namespace_name(namespace)
    job_id = str(uuid.uuid4())
    
    # Create job entry
    jobs[job_id] = JobInfo(
        job_id=job_id,
        status=JobStatus.QUEUED,
        namespace=validated_namespace,
        created_at=datetime.utcnow().isoformat()
    )
    
    # Fire and forget - start task in background without waiting
    asyncio.create_task(execute_workshop_creation(job_id, validated_namespace))
    
    print(f"✨ Created job {job_id} for namespace {validated_namespace}")
    
    return {
        "status": "accepted",
        "job_id": job_id,
        "namespace": validated_namespace,
        "message": f"Workshop creation started. Use GET /workshops/status/{job_id} to check progress.",
        "url": f"http://console.{validated_namespace}.workshop.contrastdemo.com/"
    }

@app.post("/workshops/create-with-params")
async def create_workshop_with_params(
    request: WorkshopCreateRequest = Body(...),
    x_api_key: Optional[str] = Header(None)
):
    require_api_key(x_api_key)

    validated_namespace = validate_namespace_name(request.namespace)
    job_id = str(uuid.uuid4())
    
    # Create job entry
    jobs[job_id] = JobInfo(
        job_id=job_id,
        status=JobStatus.QUEUED,
        namespace=validated_namespace,
        created_at=datetime.utcnow().isoformat()
    )
    
    # Fire and forget - start task in background without waiting
    asyncio.create_task(
        execute_workshop_creation(
            job_id,
            validated_namespace,
            agent_token=request.agent_token,
            user_api_key=request.user_api_key,
            user_auth=request.user_auth
        )
    )
    
    print(f"✨ Created job {job_id} for namespace {validated_namespace}")
    
    return {
        "status": "accepted",
        "job_id": job_id,
        "namespace": validated_namespace,
        "message": f"Workshop creation started. Use GET /workshops/status/{job_id} to check progress.",
        "url": f"http://console.{validated_namespace}.workshop.contrastdemo.com/"
    }

@app.delete("/workshops/delete")
async def delete_workshop(
    x_api_key: Optional[str] = Header(None),
    namespace: Optional[str] = None
):
    require_api_key(x_api_key)

    if namespace is None:
        raise HTTPException(status_code=400, detail="'namespace' parameter is required")

    job_id = str(uuid.uuid4())
    
    # Create job entry
    jobs[job_id] = JobInfo(
        job_id=job_id,
        status=JobStatus.QUEUED,
        namespace=namespace,
        created_at=datetime.utcnow().isoformat()
    )
    
    # Fire and forget - start task in background without waiting
    asyncio.create_task(execute_workshop_deletion(job_id, namespace))
    
    print(f"✨ Created deletion job {job_id} for namespace {namespace}")
    
    return {
        "status": "accepted",
        "job_id": job_id,
        "namespace": namespace,
        "message": "Workshop deletion started. Use GET /workshops/status/{job_id} to check progress."
    }

@app.get("/workshops/status/{job_id}")
def get_job_status(job_id: str, x_api_key: Optional[str] = Header(None)):
    require_api_key(x_api_key)
    
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    return jobs[job_id]

@app.get("/workshops/jobs")
def list_jobs(x_api_key: Optional[str] = Header(None)):
    require_api_key(x_api_key)
    
    return {"jobs": list(jobs.values())}


@app.post("/nodes/reserve")
async def create_reserved_nodes_endpoint(
    request: CreateReservedNodesRequest = Body(...),
    x_api_key: Optional[str] = Header(None)
):
    """Create N reserved nodes that will be pre-created and ready for assignment"""
    require_api_key(x_api_key)
    
    if request.count < 1:
        raise HTTPException(status_code=400, detail="Count must be at least 1")
    
    if request.count > 10:
        raise HTTPException(status_code=400, detail="Cannot create more than 10 nodes at once")
    
    job_id = str(uuid.uuid4())
    
    # Create job entry
    jobs[job_id] = JobInfo(
        job_id=job_id,
        status=JobStatus.QUEUED,
        namespace=f"reserved-nodes-{request.count}",
        created_at=datetime.utcnow().isoformat()
    )
    
    # Start background task
    async def execute_reserve_nodes():
        async with workshop_operation_lock:
            jobs[job_id].status = JobStatus.RUNNING
            jobs[job_id].started_at = datetime.utcnow().isoformat()
            print(f"🔒 Lock acquired for reserve nodes job {job_id}")
            
            try:
                created_nodes = await create_reserved_nodes(request.count)
                jobs[job_id].status = JobStatus.COMPLETED
                jobs[job_id].completed_at = datetime.utcnow().isoformat()
                jobs[job_id].stdout = f"Created reserved nodes: {', '.join(created_nodes)}"
                print(f"🔓 Lock released for reserve nodes job {job_id} - SUCCESS")
            except Exception as e:
                jobs[job_id].status = JobStatus.FAILED
                jobs[job_id].completed_at = datetime.utcnow().isoformat()
                jobs[job_id].error = str(e)
                print(f"🔓 Lock released for reserve nodes job {job_id} - FAILED: {e}")
    
    asyncio.create_task(execute_reserve_nodes())
    
    return {
        "status": "accepted",
        "job_id": job_id,
        "message": f"Creating {request.count} reserved node(s). Use GET /workshops/status/{job_id} to check progress."
    }

@app.post("/nodes/delete-unused")
async def delete_all_unused_reserved_nodes(x_api_key: Optional[str] = Header(None)):
    """Delete all nodes with node-status=reserved (not in use)"""
    require_api_key(x_api_key)
    job_id = str(uuid.uuid4())
    jobs[job_id] = JobInfo(
        job_id=job_id,
        status=JobStatus.QUEUED,
        namespace="delete-unused-reserved-nodes",
        created_at=datetime.utcnow().isoformat()
    )
    async def execute_delete_all():
        async with workshop_operation_lock:
            jobs[job_id].status = JobStatus.RUNNING
            jobs[job_id].started_at = datetime.utcnow().isoformat()
            try:
                # Get all reserved nodes
                reserved_nodes = core.list_node(label_selector="node-type=workshop,node-status=reserved")
                count = len(reserved_nodes.items)
                if count == 0:
                    jobs[job_id].status = JobStatus.COMPLETED
                    jobs[job_id].completed_at = datetime.utcnow().isoformat()
                    jobs[job_id].stdout = "No reserved nodes to delete."
                    return
                # Use the same logic as before, but delete all
                deleted_nodes = await delete_reserved_nodes(count)
                jobs[job_id].status = JobStatus.COMPLETED
                jobs[job_id].completed_at = datetime.utcnow().isoformat()
                jobs[job_id].stdout = f"Deleted {len(deleted_nodes)} reserved nodes."
            except Exception as e:
                jobs[job_id].status = JobStatus.FAILED
                jobs[job_id].completed_at = datetime.utcnow().isoformat()
                jobs[job_id].error = str(e)
    asyncio.create_task(execute_delete_all())
    return {
        "status": "accepted",
        "job_id": job_id,
        "message": "Deleting all unused reserved nodes. Use GET /workshops/status/{job_id} to check progress."
    }

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

#!/usr/bin/env python3
"""
Kubernetes CronJob script to clean up orphaned workshop nodes.
Orphaned nodes are those that:
1. Have the 'node-type=workshop' label but no 'dedicated-namespace' label
2. Have a 'dedicated-namespace' label pointing to a namespace that no longer exists
"""

import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from kubernetes import client, config

def run_command(cmd):
    """Run a shell command and return the result"""
    result = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    return result

def get_asg_name(region):
    """Get the Auto Scaling Group name for workshop nodes"""
    result = run_command([
        "aws", "autoscaling", "describe-auto-scaling-groups",
        "--region", region,
        "--query", "AutoScalingGroups[?Tags[?Key=='alpha.eksctl.io/nodegroup-name' && Value=='workshop-nodes']].AutoScalingGroupName",
        "--output", "text"
    ])
    
    if result.returncode != 0:
        print(f"❌ Failed to get ASG name: {result.stderr}")
        return None
    
    return result.stdout.strip()

def terminate_node(node_name, instance_id, asg_name, region):
    """Terminate a node and update ASG capacity"""
    print(f"🗑️  Terminating orphaned node: {node_name} (instance: {instance_id})")
    
    # Remove scale-in protection
    print(f"  ⚙️  Removing scale-in protection...")
    result = run_command([
        "aws", "autoscaling", "set-instance-protection",
        "--instance-ids", instance_id,
        "--auto-scaling-group-name", asg_name,
        "--no-protected-from-scale-in",
        "--region", region
    ])
    
    if result.returncode != 0:
        print(f"  ⚠️  Warning: Failed to remove scale-in protection: {result.stderr}")
    
    # Drain the node
    print(f"  ⚙️  Draining node...")
    result = run_command([
        "kubectl", "drain", node_name, 
        "--ignore-daemonsets", 
        "--delete-emptydir-data", 
        "--force", 
        "--timeout=60s"
    ])
    
    if result.returncode != 0:
        print(f"  ⚠️  Warning: Failed to drain node: {result.stderr}")
    
    # Terminate the instance
    print(f"  ⚙️  Terminating EC2 instance...")
    result = run_command([
        "aws", "ec2", "terminate-instances", 
        "--instance-ids", instance_id, 
        "--region", region
    ])
    
    if result.returncode != 0:
        print(f"  ❌ Failed to terminate instance: {result.stderr}")
        return False
    
    # Update ASG desired capacity
    print(f"  ⚙️  Updating ASG desired capacity...")
    
    # Get current capacity
    result = run_command([
        "aws", "autoscaling", "describe-auto-scaling-groups",
        "--auto-scaling-group-names", asg_name,
        "--region", region,
        "--query", "AutoScalingGroups[0].DesiredCapacity",
        "--output", "text"
    ])
    
    if result.returncode != 0:
        print(f"  ⚠️  Warning: Failed to get ASG capacity: {result.stderr}")
        return True
    
    try:
        current_capacity = int(result.stdout.strip())
        new_capacity = max(0, current_capacity - 1)
        
        result = run_command([
            "aws", "autoscaling", "set-desired-capacity",
            "--auto-scaling-group-name", asg_name,
            "--desired-capacity", str(new_capacity),
            "--region", region
        ])
        
        if result.returncode != 0:
            print(f"  ⚠️  Warning: Failed to update ASG capacity: {result.stderr}")
        else:
            print(f"  ✅ Updated ASG desired capacity to {new_capacity}")
    except ValueError:
        print(f"  ⚠️  Warning: Invalid capacity value: {result.stdout}")
    
    return True

def main():
    print("🔍 Starting orphaned node cleanup...")
    
    # Load Kubernetes config
    try:
        config.load_incluster_config()
        print("✅ Loaded in-cluster Kubernetes config")
    except Exception as e:
        print(f"⚠️  Failed to load in-cluster config, trying local: {e}")
        try:
            config.load_kube_config()
            print("✅ Loaded local Kubernetes config")
        except Exception as e:
            print(f"❌ Failed to load Kubernetes config: {e}")
            sys.exit(1)
    
    core_v1 = client.CoreV1Api()
    region = os.getenv("AWS_REGION", "eu-west-1")
    
    # Get ASG name
    asg_name = get_asg_name(region)
    if not asg_name:
        print("❌ Could not find Auto Scaling Group for workshop nodes")
        sys.exit(1)
    
    print(f"✅ Found ASG: {asg_name}")
    
    # Get all workshop nodes
    try:
        nodes = core_v1.list_node(label_selector="node-type=workshop")
        print(f"📊 Found {len(nodes.items)} workshop nodes")
    except Exception as e:
        print(f"❌ Failed to list nodes: {e}")
        sys.exit(1)
    
    # Get all namespaces
    try:
        namespaces = core_v1.list_namespace()
        namespace_names = set(ns.metadata.name for ns in namespaces.items)
        print(f"📊 Found {len(namespace_names)} namespaces in cluster")
    except Exception as e:
        print(f"❌ Failed to list namespaces: {e}")
        sys.exit(1)
    
    orphaned_nodes = []
    
    for node in nodes.items:
        node_name = node.metadata.name
        node_labels = node.metadata.labels or {}
        
        # Get node creation time (Kubernetes returns timezone-aware datetime objects)
        node_creation_time = node.metadata.creation_timestamp
        current_time = datetime.now(timezone.utc)
        
        # Ensure node_creation_time is timezone-aware (it should be from k8s API)
        if node_creation_time.tzinfo is None:
            node_creation_time = node_creation_time.replace(tzinfo=timezone.utc)
        
        node_age = current_time - node_creation_time
        
        # Check if node has dedicated-namespace label
        if "dedicated-namespace" not in node_labels:
            # Only mark as orphaned if node is older than 15 minutes
            if node_age > timedelta(minutes=15):
                print(f"⚠️  Node {node_name} has no 'dedicated-namespace' label and is {node_age.total_seconds()/60:.1f} minutes old - marking as orphaned")
                orphaned_nodes.append(node)
            else:
                print(f"ℹ️  Node {node_name} has no 'dedicated-namespace' label but is only {node_age.total_seconds()/60:.1f} minutes old - skipping (grace period)")
            continue
        
        # Check if the namespace exists
        dedicated_namespace = node_labels.get("dedicated-namespace")
        if dedicated_namespace not in namespace_names:
            print(f"⚠️  Node {node_name} references non-existent namespace '{dedicated_namespace}' - marking as orphaned")
            orphaned_nodes.append(node)
            continue
        
        print(f"✅ Node {node_name} is healthy (namespace: {dedicated_namespace})")
    
    # Terminate orphaned nodes
    if not orphaned_nodes:
        print("🎉 No orphaned nodes found!")
        return
    
    print(f"\n🗑️  Found {len(orphaned_nodes)} orphaned node(s) to clean up")
    
    for node in orphaned_nodes:
        node_name = node.metadata.name
        provider_id = node.spec.provider_id
        
        if not provider_id:
            print(f"⚠️  Node {node_name} has no provider_id, skipping...")
            continue
        
        # Extract instance ID from provider_id (format: aws:///region/instance-id)
        instance_id = provider_id.split('/')[-1]
        
        try:
            terminate_node(node_name, instance_id, asg_name, region)
        except Exception as e:
            print(f"❌ Error terminating node {node_name}: {e}")
    
    print(f"\n✅ Cleanup complete!")

if __name__ == "__main__":
    main()

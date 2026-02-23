"""
GKE Infrastructure with Pulumi - Main Entry Point

This module orchestrates the deployment of a complete GKE cluster infrastructure
with networking, IAM, and cluster configuration.
"""

import pulumi
from modules import create_network, create_gke_cluster, create_node_pool, create_gpu_node_pool, create_service_accounts, create_artifact_registry, create_public_ip, create_argocd
import config

def main():
    """Main function to deploy GKE infrastructure"""
    
    pulumi.info(f"Deploying GKE cluster '{config.cluster_name}' in {config.gcp_region}")
    
    # 1. Create Network Infrastructure
    pulumi.info("Setting up network infrastructure...")
    network_resources = create_network(
        project_name=config.gcp_project,
        network_name=config.network_name,
        subnet_name=config.subnet_name,
        subnet_cidr=config.subnet_cidr,
        region=config.gcp_region,
        labels=config.labels,
        pods_cidr=config.pods_cidr,
        services_cidr=config.services_cidr,
    )
    
    # 2. Create Service Accounts
    pulumi.info("Creating service accounts...")
    iam_resources = create_service_accounts(
        project_name=config.gcp_project,
        cluster_name=config.cluster_name,
        region=config.gcp_region,
    )
    
    # 3. Create GKE Cluster
    pulumi.info("Creating GKE cluster...")
    cluster_resources = create_gke_cluster(
        cluster_name=config.cluster_name,
        region=config.gcp_region,
        zone=config.gke_zone,
        network_id=network_resources['network'].id,
        subnet_id=network_resources['subnet'].id,
        service_account_email=iam_resources['gke_service_account'].email,
        kubernetes_version=config.kubernetes_version,
        enable_network_policy=config.enable_network_policy,
        labels=config.labels,
        pods_cidr=config.pods_cidr,
        services_cidr=config.services_cidr,
        project=config.gcp_project,
    )
    
    # 4. Create Node Pool
    pulumi.info("Creating node pool...")
    node_pool_resources = create_node_pool(
        cluster=cluster_resources['cluster'],
        cluster_name=config.cluster_name,
        node_pool_name=config.node_pool_name,
        machine_type=config.machine_type,
        region=config.gke_zone,
        min_node_count=config.min_node_count,
        max_node_count=config.max_node_count,
        disk_size_gb=config.disk_size_gb,
        service_account_email=iam_resources['gke_service_account'].email,
        labels=config.labels,
    )
    
    # 4.5 Create GPU Node Pool
    pulumi.info("Creating GPU node pool...")
    gpu_node_pool_resources = create_gpu_node_pool(
        cluster=cluster_resources['cluster'],
        cluster_name=config.cluster_name,
        node_pool_name=config.gpu_node_pool_name,
        machine_type=config.gpu_machine_type,
        accelerator_type=config.gpu_accelerator_type,
        accelerator_count=config.gpu_accelerator_count,
        region=config.gke_zone,
        min_node_count=config.gpu_min_node_count,
        max_node_count=config.gpu_max_node_count,
        disk_size_gb=config.gpu_disk_size_gb,
        service_account_email=iam_resources['gke_service_account'].email,
        labels=config.labels,
    )
    
    # 5. Create Artifact Registry
    pulumi.info("Creating artifact registry...")
    artifact_registry_resources = create_artifact_registry(
        project_name=config.gcp_project,
        repository_name=f'gke-artifacts-{config.environment}',
        region=config.gcp_region,
        repository_format='DOCKER',
        labels=config.labels,
        service_account_emails=[iam_resources['gke_service_account'].email],
    )
    
    # 6. Create Public IP Address for Gateway API
    pulumi.info("Creating public IP address for gateway API...")
    public_ip_resources = create_public_ip(
        project_name=config.gcp_project,
        address_name='k8s-gateway-ip',
        region=config.gcp_region,
        description='Public IP address for Kubernetes Gateway API',
        labels=config.labels,
    )
    
    # 7. Create ArgoCD via Helm
    pulumi.info("Deploying ArgoCD...")
    argocd_resources = create_argocd(
        cluster_name=cluster_resources['cluster_name'],
        endpoint=cluster_resources['endpoint'],
        ca_certificate=cluster_resources['ca_certificate'],
        chart_version=config.argocd_chart_version,
        chart_repo=config.argocd_chart_repo,
        app_of_apps_path=config.argocd_app_of_apps_path,
        namespace=config.argocd_namespace
    )
    
    # Stack Outputs
    pulumi.export('cluster_name', cluster_resources['cluster_name'])
    pulumi.export('cluster_endpoint', cluster_resources['endpoint'])
    pulumi.export('network_name', network_resources['network'].name)
    pulumi.export('subnet_name', network_resources['subnet'].name)
    pulumi.export('artifact_registry_url', artifact_registry_resources['repository_url'])
    pulumi.export('gateway_ip_address', public_ip_resources['ip_address'])
    pulumi.export('argocd_namespace', argocd_resources['namespace'])
    
    # Export kubeconfig connection details
    pulumi.export('kubeconfig', pulumi.Output.concat(
        'kubectl config set-context--current --cluster=',
        cluster_resources['cluster_name'],
    ))
    
    pulumi.info("✅ GKE infrastructure deployment started")
    
    return {
        'network': network_resources,
        'cluster': cluster_resources,
        'node_pool': node_pool_resources,
        'gpu_node_pool': gpu_node_pool_resources,
        'iam': iam_resources,
        'artifact_registry': artifact_registry_resources,
        'public_ip': public_ip_resources,
        'argocd': argocd_resources,
    }

if __name__ == '__main__':
    main()

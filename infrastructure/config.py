import pulumi
import os

config = pulumi.Config()
gcp_config = pulumi.Config('gcp')
gke_config = pulumi.Config('gke')
network_config = pulumi.Config('network')
argocd_config = pulumi.Config('argocd')

# GCP Settings
gcp_project = gcp_config.require('project')
gcp_region = gcp_config.require('region')

# Environment
environment = config.require('environment')

# Network Settings
network_name = network_config.require('name')
subnet_name = network_config.require('subnet')
subnet_cidr = network_config.require('cidr')
pods_cidr = network_config.require('pods_cidr')
services_cidr = network_config.require('services_cidr')

# GKE Cluster Settings
gke_zone = gke_config.require('zone')
cluster_name = gke_config.require('cluster_name')
kubernetes_version = gke_config.require('version')
enable_network_policy = gke_config.require_bool('enable_network_policy')
enable_autoscaling = gke_config.require_bool('enable_autoscaling')

# Node Pool Settings
node_pool_name = gke_config.require('node_pool_name')
machine_type = gke_config.require('machine_type')
min_node_count = gke_config.require_int('min_node_count')
max_node_count = gke_config.require_int('max_node_count')
disk_size_gb = gke_config.require_int('disk_size_gb')

# GPU Node Pool Settings
gpu_zone = gke_config.require('gpu_zone')
gpu_node_pool_name = gke_config.require('gpu_node_pool_name')
gpu_machine_type = gke_config.require('gpu_machine_type')
gpu_accelerator_type = gke_config.require('gpu_accelerator_type')
gpu_accelerator_count = gke_config.require_int('gpu_accelerator_count')
gpu_partition_size = gke_config.get('gpu_partition_size')
gpu_min_node_count = gke_config.require_int('gpu_min_node_count')
gpu_max_node_count = gke_config.require_int('gpu_max_node_count')
gpu_disk_size_gb = gke_config.require_int('gpu_disk_size_gb')

# ArgoCD Settings
argocd_chart_version = argocd_config.require('chart_version')
argocd_chart_repo = argocd_config.require('chart_repo')
argocd_app_of_apps_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'services', 'argocd-apps'))
argocd_namespace = argocd_config.require('namespace')

# Labels and Tags
labels = {
    'environment': environment,
    'managed_by': 'pulumi',
    'project': 'gke-infra',
}

tags = [environment, 'gke', 'kubernetes']

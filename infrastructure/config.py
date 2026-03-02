import pulumi

config = pulumi.Config()
gcp_config = pulumi.Config('gcp')
gke_config = pulumi.Config('gke')
network_config = pulumi.Config('network')

# GCP Settings
gcp_project = gcp_config.require('project')
gcp_region = gcp_config.get('region') or 'us-central1'
gcp_zone = f"{gcp_region}-a"

# Environment
environment = config.get('environment') or 'dev'

# Network Settings
network_name = network_config.get('name') or f'gke-network-{environment}'
subnet_name = network_config.get('subnet') or f'gke-subnet-{environment}'
subnet_cidr = network_config.get('cidr') or '10.0.0.0/20'
pods_cidr = network_config.get('pods_cidr') or '10.4.0.0/14'
services_cidr = network_config.get('services_cidr') or '10.0.16.0/20'

# GKE Cluster Settings
gke_zone = gke_config.get('zone') or f'{gcp_region}-a'
cluster_name = gke_config.get('cluster_name') or f'gke-cluster-{environment}'
kubernetes_version = gke_config.get('version') or '1.31'
enable_network_policy = gke_config.get_bool('enable_network_policy') or True
enable_autoscaling = gke_config.get_bool('enable_autoscaling') or True

# Node Pool Settings
node_pool_name = gke_config.get('node_pool_name') or f'default-pool-{environment}'
machine_type = gke_config.get('machine_type') or 'n2-standard-4'
min_node_count = gke_config.get_int('min_node_count') or 2
max_node_count = gke_config.get_int('max_node_count') or 10
disk_size_gb = gke_config.get_int('disk_size_gb') or 100

# GPU Node Pool Settings
gpu_node_pool_name = gke_config.get('gpu_node_pool_name') or f't4-pool-{environment}'
gpu_machine_type = gke_config.get('gpu_machine_type') or 'n1-standard-4'
gpu_accelerator_type = gke_config.get('gpu_accelerator_type') or 'nvidia-t4'
gpu_accelerator_count = gke_config.get_int('gpu_accelerator_count') or 1
gpu_min_node_count = gke_config.get_int('gpu_min_node_count') or 1
gpu_max_node_count = gke_config.get_int('gpu_max_node_count') or 1
gpu_disk_size_gb = gke_config.get_int('gpu_disk_size_gb') or 100

# ArgoCD Settings
argocd_config = pulumi.Config('argocd')
argocd_chart_version = argocd_config.get('chart_version') or '6.7.11'
argocd_chart_repo = argocd_config.get('chart_repo') or 'https://argoproj.github.io/argo-helm'
import os
...
argocd_app_of_apps_path = argocd_config.get('app_of_apps_path') or os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'services', 'argocd-apps'))
argocd_namespace = argocd_config.get('namespace') or 'argocd'

# Labels and Tags
labels = {
    'environment': environment,
    'managed_by': 'pulumi',
    'project': 'gke-infra'
}

tags = [environment, 'gke', 'kubernetes']

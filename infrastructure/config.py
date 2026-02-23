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

# Labels and Tags
labels = {
    'environment': environment,
    'managed_by': 'pulumi',
    'project': 'gke-infra'
}

tags = [environment, 'gke', 'kubernetes']

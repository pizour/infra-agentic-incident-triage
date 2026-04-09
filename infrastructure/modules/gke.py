import pulumi
import pulumi_gcp as gcp

def create_gke_cluster(
    cluster_name: str,
    region: str,
    zone: str,
    network_id: str,
    subnet_id: str,
    service_account_email: str,
    kubernetes_version: str,
    enable_network_policy: bool,
    labels: dict,
    pods_cidr: str,
    services_cidr: str,
    node_pool_name: str,
    machine_type: str,
    min_node_count: int,
    max_node_count: int,
    disk_size_gb: int,
    project: str = None,
) -> dict:
    """
    Create GKE cluster with advanced networking and security settings
    
    Args:
        cluster_name: Name of the GKE cluster
        region: GCP region
        network_id: VPC network ID
        subnet_id: Subnet ID
        service_account_email: Service account email for nodes
        kubernetes_version: Kubernetes version
        enable_network_policy: Enable Calico network policy
        labels: Labels for resources
        pods_cidr: CIDR range for pods
        services_cidr: CIDR range for services
    
    Returns:
        Dictionary containing cluster reference and kubeconfig
    """
    
    # Create GKE cluster
    gke_cluster = gcp.container.Cluster(
        cluster_name,
        name=cluster_name,
        location=zone,  # single-zone cluster
        deletion_protection=False,
        network=network_id,
        subnetwork=subnet_id,
        resource_labels=labels,
        
        # Networking
        networking_mode='VPC_NATIVE',
        ip_allocation_policy=gcp.container.ClusterIpAllocationPolicyArgs(
            cluster_secondary_range_name='pods',
            services_secondary_range_name='services',
        ),
        default_snat_status=gcp.container.ClusterDefaultSnatStatusArgs(
            disabled=False,
        ),
        
        # Network policy
        network_policy=gcp.container.ClusterNetworkPolicyArgs(
            enabled=enable_network_policy,
            provider='CALICO' if enable_network_policy else 'PROVIDER_UNSPECIFIED',
        ),
        
        # Security
        logging_config=gcp.container.ClusterLoggingConfigArgs(
            enable_components=['SYSTEM_COMPONENTS', 'WORKLOADS', 'APISERVER'],
        ),
        monitoring_config=gcp.container.ClusterMonitoringConfigArgs(
            enable_components=['SYSTEM_COMPONENTS'],
        ),
        
        # Kubernetes release channel (manages version automatically)
        release_channel=gcp.container.ClusterReleaseChannelArgs(
            channel='REGULAR',
        ),
        
        # Addons
        addons_config=gcp.container.ClusterAddonsConfigArgs(
            http_load_balancing=gcp.container.ClusterAddonsConfigHttpLoadBalancingArgs(
                disabled=False,
            ),
            horizontal_pod_autoscaling=gcp.container.ClusterAddonsConfigHorizontalPodAutoscalingArgs(
                disabled=False,
            ),
            network_policy_config=gcp.container.ClusterAddonsConfigNetworkPolicyConfigArgs(
                disabled=not enable_network_policy,
            ),
        ),
        
        # Workload Identity
        workload_identity_config=gcp.container.ClusterWorkloadIdentityConfigArgs(
            workload_pool=f'{project}.svc.id.goog',
        ) if project else None,
        
        # Inline Node Pool
        node_pools=[{
            "name": node_pool_name,
            "initial_node_count": min_node_count,
            "autoscaling": {
                "min_node_count": min_node_count,
                "max_node_count": max_node_count,
            },
            "node_config": {
                "machine_type": machine_type,
                "disk_size_gb": disk_size_gb,
                "disk_type": 'pd-standard',
                "service_account": service_account_email,
                "oauth_scopes": ['https://www.googleapis.com/auth/cloud-platform'],
                "metadata": {'disable-legacy-endpoints': 'true'},
                "labels": labels,
                "tags": ['gke-node', cluster_name],
                "shielded_instance_config": {
                    "enable_secure_boot": True,
                    "enable_integrity_monitoring": True,
                },
                "workload_metadata_config": {
                    "mode": 'GKE_METADATA',
                },
            },
            "management": {
                "auto_repair": True,
                "auto_upgrade": True,
            },
        }],
        
        opts=pulumi.ResourceOptions(
            ignore_changes=['node_config'],
        ),
    )
    
    return {
        'cluster': gke_cluster,
        'cluster_name': gke_cluster.name,
        'endpoint': gke_cluster.endpoint,
        'ca_certificate': gke_cluster.master_auth.cluster_ca_certificate,
    }


def create_node_pool(
    cluster,
    cluster_name: str,
    node_pool_name: str,
    machine_type: str,
    region: str,
    min_node_count: int,
    max_node_count: int,
    disk_size_gb: int,
    service_account_email: str,
    labels: dict,
) -> dict:
    """
    Create GKE node pool with autoscaling and security settings
    
    Args:
        cluster: Cluster resource object
        cluster_name: Cluster name
        node_pool_name: Name of the node pool
        machine_type: Machine type (e.g., n2-standard-4)
        region: GCP region
        min_node_count: Minimum number of nodes
        max_node_count: Maximum number of nodes
        disk_size_gb: Disk size in GB
        service_account_email: Service account email
        labels: Labels for nodes
    
    Returns:
        Dictionary containing node pool reference
    """
    
    node_pool = gcp.container.NodePool(
        node_pool_name,
        name=node_pool_name,
        cluster=cluster.id,
        location=region,
        
        # Scaling
        autoscaling=gcp.container.NodePoolAutoscalingArgs(
            min_node_count=min_node_count,
            max_node_count=max_node_count,
        ),
        
        # Node configuration
        node_config=gcp.container.NodePoolNodeConfigArgs(
            machine_type=machine_type,
            disk_size_gb=disk_size_gb,
            disk_type='pd-standard',
            service_account=service_account_email,
            oauth_scopes=[
                'https://www.googleapis.com/auth/cloud-platform',
            ],
            metadata={
                'disable-legacy-endpoints': 'true',
            },
            labels=labels,
            tags=['gke-node', cluster_name],
            shielded_instance_config=gcp.container.NodePoolNodeConfigShieldedInstanceConfigArgs(
                enable_secure_boot=True,
                enable_integrity_monitoring=True,
            ),
            workload_metadata_config=gcp.container.NodePoolNodeConfigWorkloadMetadataConfigArgs(
                mode='GKE_METADATA',
            ),
        ),
        
        # Management
        management=gcp.container.NodePoolManagementArgs(
            auto_repair=True,
            auto_upgrade=True,
        ),
        
        # initial_node_count is intentionally omitted — setting it alongside autoscaling
        # causes Pulumi to see perpetual drift as the autoscaler changes node counts.
        # cluster is ignored because GCP state stores the full resource path but Pulumi
        # may resolve to just the name on subsequent runs.
        # node_config is ignored to suppress resourceManagerTags drift added by GCP.
        opts=pulumi.ResourceOptions(
            depends_on=[cluster],
            ignore_changes=['initial_node_count', 'node_count', 'cluster', 'node_config'],
        ),
    )
    
    return {
        'node_pool': node_pool,
        'node_pool_name': node_pool.name,
    }


def create_gpu_node_pool(
    cluster,
    cluster_name: str,
    node_pool_name: str,
    machine_type: str,
    accelerator_type: str,
    accelerator_count: int,
    region: str,
    min_node_count: int,
    max_node_count: int,
    disk_size_gb: int,
    service_account_email: str,
    labels: dict,
    node_locations: list = None,
    gpu_partition_size: str = None,
    spot: bool = False,
) -> dict:
    """
    Create GKE GPU node pool with autoscaling and specific taints
    
    Args:
        cluster: Cluster resource object
        cluster_name: Cluster name
        node_pool_name: Name of the node pool
        machine_type: Machine type (e.g., g2-standard-8)
        accelerator_type: GPU accelerator type (e.g., nvidia-l4)
        accelerator_count: Number of GPUs per node
        region: GCP region
        min_node_count: Minimum number of nodes
        max_node_count: Maximum number of nodes
        disk_size_gb: Disk size in GB
        service_account_email: Service account email
        labels: Labels for nodes
        node_locations: Specific zones to deploy nodes into (optional)
    
    Returns:
        Dictionary containing node pool reference
    """
    
    # Merge specific GPU labels with standard ones
    gpu_labels = {
        **labels,
        'gpu-node': 'true',
    }
    
    node_pool = gcp.container.NodePool(
        node_pool_name,
        name=node_pool_name,
        cluster=cluster.id,
        location=region,
        node_locations=node_locations,
        
        # Scaling
        autoscaling=gcp.container.NodePoolAutoscalingArgs(
            min_node_count=min_node_count,
            max_node_count=max_node_count,
        ),
        
        # Node configuration
        node_config=gcp.container.NodePoolNodeConfigArgs(
            machine_type=machine_type,
            disk_size_gb=disk_size_gb,
            disk_type='pd-standard',
            spot=spot,
            service_account=service_account_email,
            oauth_scopes=[
                'https://www.googleapis.com/auth/cloud-platform',
            ],
            metadata={
                'disable-legacy-endpoints': 'true',
            },
            labels=gpu_labels,
            tags=['gke-node', 'gpu-node', cluster_name],
            
            # GPU specifics
            guest_accelerators=[gcp.container.NodePoolNodeConfigGuestAcceleratorArgs(
                type=accelerator_type,
                count=accelerator_count,
                gpu_partition_size=gpu_partition_size,
                gpu_driver_installation_config=gcp.container.NodePoolNodeConfigGuestAcceleratorGpuDriverInstallationConfigArgs(
                    gpu_driver_version="LATEST"
                )
            )],
            taints=[gcp.container.NodePoolNodeConfigTaintArgs(
                key="nvidia.com/gpu",
                value="present",
                effect="NO_SCHEDULE",
            )],
            
            shielded_instance_config=gcp.container.NodePoolNodeConfigShieldedInstanceConfigArgs(
                enable_secure_boot=True,
                enable_integrity_monitoring=True,
            ),
            workload_metadata_config=gcp.container.NodePoolNodeConfigWorkloadMetadataConfigArgs(
                mode='GKE_METADATA',
            ),
        ),
        
        # Management
        management=gcp.container.NodePoolManagementArgs(
            auto_repair=True,
            auto_upgrade=True,
        ),
        
        opts=pulumi.ResourceOptions(
            depends_on=[cluster],
            ignore_changes=['initial_node_count', 'node_count', 'cluster'],
        ),
    )
    
    return {
        'node_pool': node_pool,
        'node_pool_name': node_pool.name,
    }


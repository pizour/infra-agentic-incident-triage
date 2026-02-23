import pulumi
import pulumi_gcp as gcp

def create_network(project_name: str, network_name: str, subnet_name: str, 
                   subnet_cidr: str, region: str, labels: dict,
                   pods_cidr: str = '10.4.0.0/14', services_cidr: str = '10.0.16.0/20') -> dict:
    """
    Create VPC network and subnet for GKE cluster
    
    Args:
        project_name: GCP project name
        network_name: Name of the VPC network
        subnet_name: Name of the subnet
        subnet_cidr: CIDR range for the subnet (e.g., 10.0.0.0/20)
        region: GCP region
        labels: Labels for resources
    
    Returns:
        Dictionary containing network and subnet references
    """
    
    # Create VPC Network
    network = gcp.compute.Network(
        network_name,
        name=network_name,
        auto_create_subnetworks=False,
        routing_mode='REGIONAL',
    )
    
    # Create Subnet with secondary ranges for GKE pods and services
    subnet = gcp.compute.Subnetwork(
        subnet_name,
        name=subnet_name,
        ip_cidr_range=subnet_cidr,
        region=region,
        network=network.id,
        private_ip_google_access=True,
        secondary_ip_ranges=[
            gcp.compute.SubnetworkSecondaryIpRangeArgs(
                range_name='pods',
                ip_cidr_range=pods_cidr,
            ),
            gcp.compute.SubnetworkSecondaryIpRangeArgs(
                range_name='services',
                ip_cidr_range=services_cidr,
            ),
        ],
        log_config=gcp.compute.SubnetworkLogConfigArgs(
            aggregation_interval='INTERVAL_5_SEC',
        ),
    )
    
    # Create Cloud Router for Cloud NAT (optional but recommended)
    router = gcp.compute.Router(
        f'{network_name}-router',
        name=f'{network_name}-router',
        region=region,
        network=network.self_link,
    )
    
    # Create Cloud NAT for pod egress
    nat = gcp.compute.RouterNat(
        f'{network_name}-nat',
        router=router.name,
        region=region,
        nat_ip_allocate_option='AUTO_ONLY',
        source_subnetwork_ip_ranges_to_nat='ALL_SUBNETWORKS_ALL_IP_RANGES',
        log_config=gcp.compute.RouterNatLogConfigArgs(
            enable=True,
            filter='ERRORS_ONLY',
        ),
    )
    
    return {
        'network': network,
        'subnet': subnet,
        'router': router,
        'nat': nat,
    }

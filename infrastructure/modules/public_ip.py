import pulumi
import pulumi_gcp as gcp


def create_public_ip(
    project_name: str,
    address_name: str,
    region: str = None,
    address_type: str = 'EXTERNAL',
    description: str = None,
    labels: dict = None,
) -> dict:
    """
    Create a public IP address in GCP
    
    Args:
        project_name: GCP project name
        address_name: Name of the public IP address
        region: GCP region (None for global)
        address_type: Type of address - 'EXTERNAL' (default) or 'INTERNAL'
        description: Description of the address
        labels: Labels for the address
    
    Returns:
        Dictionary containing address resource reference and details
    """
    
    if labels is None:
        labels = {}
    
    # Create public IP address
    address = gcp.compute.Address(
        address_name,
        project=project_name,
        name=address_name,
        address_type=address_type,
        region=region,
        description=description or f'Public IP address for {address_name}',
        labels=labels,
    )
    
    return {
        'address': address,
        'address_name': address.name,
        'ip_address': address.address,
        'self_link': address.self_link,
    }


def create_multiple_public_ips(
    project_name: str,
    addresses: list,
    region: str = None,
    labels: dict = None,
) -> dict:
    """
    Create multiple public IP addresses in GCP
    
    Args:
        project_name: GCP project name
        addresses: List of address configurations, each with 'name' and optional 'description'
        region: GCP region (None for global)
        labels: Labels for all addresses
    
    Returns:
        Dictionary containing all created address resources
    """
    
    if labels is None:
        labels = {}
    
    created_addresses = {}
    
    for addr_config in addresses:
        addr_name = addr_config['name']
        addr_description = addr_config.get('description', f'Public IP address for {addr_name}')
        
        address = gcp.compute.Address(
            addr_name,
            project=project_name,
            name=addr_name,
            address_type='EXTERNAL',
            region=region,
            description=addr_description,
            labels=labels,
        )
        
        created_addresses[addr_name] = {
            'address': address,
            'ip_address': address.address,
            'self_link': address.self_link,
        }
    
    return created_addresses

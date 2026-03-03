import pulumi
import pulumi_gcp as gcp

def create_testing_vm(project_id: str, region: str, zone: str, network_id: str, subnet_id: str, 
                      labels: dict, username: str, password_secret_id: str) -> dict:
    """
    Creates a testing VM in GCP with public IP and password auth.
    
    Args:
        project_id: GCP project ID
        region: GCP region
        zone: GCP zone
        network_id: VPC network ID
        subnet_id: Subnetwork ID
        labels: Resource labels
        username: Linux username to create
        password_secret_id: Secret ID in GCP Secret Manager for the user password
    
    Returns:
        Dictionary with instance details and public IP
    """
    
    # 1. Fetch the password from Secret Manager
    password_data = gcp.secretmanager.get_secret_version_output(
        secret=password_secret_id,
        project=project_id,
    )
    
    # 2. Startup script
    startup_script = pulumi.Output.all(password_data.secret_data).apply(lambda args: f"""#!/bin/bash
    set -e
    
    # Create user and set password
    useradd -m -s /bin/bash {username} || true
    echo "{username}:{args[0]}" | chpasswd
    usermod -aG sudo {username}
    
    # Enable password authentication in SSH
    sed -i 's/PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
    systemctl restart ssh
    
    # Install node_exporter
    wget -q https://github.com/prometheus/node_exporter/releases/download/v1.7.0/node_exporter-1.7.0.linux-amd64.tar.gz
    tar xzf node_exporter-1.7.0.linux-amd64.tar.gz
    cp node_exporter-1.7.0.linux-amd64/node_exporter /usr/local/bin/
    
    cat <<EOF > /etc/systemd/system/node_exporter.service
[Unit]
Description=Node Exporter
After=network.target

[Service]
User=root
ExecStart=/usr/local/bin/node_exporter

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now node_exporter
    
    echo "Testing VM ready."
    """)

    # 3. Create the Compute Instance
    instance = gcp.compute.Instance(
        "testing-vm",
        name="testing-vm-dev",
        machine_type="e2-medium",
        zone=zone,
        labels=labels,
        tags=["testing-vm"],
        boot_disk=gcp.compute.InstanceBootDiskArgs(
            initialize_params=gcp.compute.InstanceBootDiskInitializeParamsArgs(
                image="debian-cloud/debian-12",
                size=20,
            ),
        ),
        network_interfaces=[gcp.compute.InstanceNetworkInterfaceArgs(
            network=network_id,
            subnetwork=subnet_id,
            access_configs=[gcp.compute.InstanceNetworkInterfaceAccessConfigArgs(
                # Ephemeral IP
            )],
        )],
        metadata={
            "startup-script": startup_script,
            "enable-oslogin": "FALSE",
        },
        service_account=gcp.compute.InstanceServiceAccountArgs(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        ),
    )
    
    # 4. Create Firewall rule
    firewall = gcp.compute.Firewall(
        "testing-vm-firewall",
        network=network_id,
        allows=[
            gcp.compute.FirewallAllowArgs(
                protocol="tcp",
                ports=["22", "9100"],
            ),
        ],
        source_ranges=["0.0.0.0/0"],
        target_tags=["testing-vm"],
    )

    return {
        "instance": instance,
        "public_ip": instance.network_interfaces[0].access_configs[0].nat_ip,
    }

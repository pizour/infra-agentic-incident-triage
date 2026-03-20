import pulumi
import pulumi_gcp as gcp


def create_testing_vm(project_id: str, region: str, zone: str, network_id: str, subnet_id: str, 
                      labels: dict, username: str, password: str,
                      pods_cidr: str = '10.4.0.0/14',
                      loki_url: str = '') -> dict:
    """
    Creates a testing VM in GCP with public IP and password auth.
    Installs node_exporter and Promtail to ship SSH auth logs to Loki.
    
    Args:
        project_id: GCP project ID
        region: GCP region
        zone: GCP zone
        network_id: VPC network ID
        subnet_id: Subnetwork ID
        labels: Resource labels
        username: Linux username to create
        password: Password for the user (from GitHub secrets via env var)
        pods_cidr: GKE pods CIDR range (for internal firewall rule)
        loki_url: Loki push URL (e.g. http://<internal-ip>:3100/loki/api/v1/push)
    
    Returns:
        Dictionary with instance details and public IP
    """
    
    # Startup script to configure the VM
    startup_script = f"""#!/bin/bash

# --- SSH config FIRST, before anything that could fail ---
# Debian 12 cloud images have /etc/ssh/sshd_config.d/60-cloudimg-settings.conf
# which sets PasswordAuthentication no and overrides the main sshd_config.
# A 01-* file wins over 60-* because sshd parses drop-ins alphabetically and honors the first encountered setting.
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/01-password-auth.conf << 'SSHEOF'
PasswordAuthentication yes
KbdInteractiveAuthentication yes
SSHEOF
systemctl restart sshd

# Create user and set password
useradd -m -s /bin/bash {username} || true
echo "{username}:{password}" | chpasswd || echo "chpasswd failed, check password"
usermod -aG sudo,adm {username} || true

# ------------------------------------------------
# Install node_exporter (system metrics on :9100)
# ------------------------------------------------
NODE_EXPORTER_VERSION="1.7.0"
wget -q https://github.com/prometheus/node_exporter/releases/download/v${{NODE_EXPORTER_VERSION}}/node_exporter-${{NODE_EXPORTER_VERSION}}.linux-amd64.tar.gz
tar xzf node_exporter-${{NODE_EXPORTER_VERSION}}.linux-amd64.tar.gz
cp node_exporter-${{NODE_EXPORTER_VERSION}}.linux-amd64/node_exporter /usr/local/bin/
rm -rf node_exporter-${{NODE_EXPORTER_VERSION}}*

cat <<'EOF' > /etc/systemd/system/node_exporter.service
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

# ------------------------------------------------
# Install Promtail (ship auth logs to Loki)
# ------------------------------------------------
PROMTAIL_VERSION="3.0.0"
wget -q https://github.com/grafana/loki/releases/download/v${{PROMTAIL_VERSION}}/promtail-linux-amd64.zip
apt-get install -y unzip
unzip -o promtail-linux-amd64.zip -d /usr/local/bin/
chmod +x /usr/local/bin/promtail-linux-amd64
rm -f promtail-linux-amd64.zip

mkdir -p /etc/promtail

# Fetch internal IP from metadata
HOST_IP=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip)

cat <<PROMTAILEOF > /etc/promtail/config.yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /tmp/positions.yaml

clients:
  - url: {loki_url}

scrape_configs:
  - job_name: ssh-auth
    static_configs:
      - targets:
          - localhost
        labels:
          job: ssh-auth
          host: testing-vm-dev
          host_ip: $HOST_IP
          __path__: /var/log/auth.log
    pipeline_stages:
      - regex:
          expression: '.*sshd.*(?:Failed password|Accepted password|Invalid user).*from (?P<source_ip>\d+\.\d+\.\d+\.\d+).*'
      - labels:
          source_ip:
PROMTAILEOF

cat <<'EOF' > /etc/systemd/system/promtail.service
[Unit]
Description=Promtail - Log shipper for Loki
After=network.target

[Service]
User=root
ExecStart=/usr/local/bin/promtail-linux-amd64 -config.file=/etc/promtail/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now promtail

echo "Testing VM ready. node_exporter on :9100, promtail shipping to Loki"
"""

    # Create the Compute Instance
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
                # Ephemeral public IP
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
    
    # Firewall: SSH from internet (public)
    ssh_firewall = gcp.compute.Firewall(
        "testing-vm-ssh-firewall",
        network=network_id,
        allows=[
            gcp.compute.FirewallAllowArgs(
                protocol="tcp",
                ports=["22"],
            ),
        ],
        source_ranges=["0.0.0.0/0"],
        target_tags=["testing-vm"],
    )
    
    # Firewall: node_exporter from GKE pods only (internal)
    metrics_firewall = gcp.compute.Firewall(
        "testing-vm-metrics-firewall",
        network=network_id,
        allows=[
            gcp.compute.FirewallAllowArgs(
                protocol="tcp",
                ports=["9100"],
            ),
        ],
        source_ranges=[pods_cidr, "10.0.0.0/20"],  # GKE pods + subnet CIDR
        target_tags=["testing-vm"],
    )

    return {
        "instance": instance,
        "public_ip": instance.network_interfaces[0].access_configs[0].nat_ip,
        "internal_ip": instance.network_interfaces[0].network_ip,
    }

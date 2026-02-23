# GKE Infrastructure with Pulumi

A modular, production-ready Pulumi infrastructure-as-code project for deploying Google Kubernetes Engine (GKE) clusters on Google Cloud Platform.

## 📋 Project Structure

```
gke-pulumi/
├── __main__.py              # Main orchestration entry point
├── config.py                # Configuration management
├── Pulumi.yaml              # Pulumi project definition
├── Pulumi.dev.yaml          # Development stack configuration
├── requirements.txt         # Python dependencies
├── modules/
│   ├── __init__.py          # Module exports
│   ├── network.py           # VPC, Subnets, Cloud NAT, Cloud Router
│   ├── gke.py               # GKE cluster and node pool creation
│   └── iam.py               # Service accounts and IAM roles
└── README.md                # This file
```

## 🚀 Quick Start

### Prerequisites

1. **Pulumi CLI**: [Install Pulumi](https://www.pulumi.com/docs/get-started/install/)
2. **Google Cloud SDK**: `gcloud` CLI installed and configured
3. **Python 3.7+**: Python runtime environment
4. **GCP Project**: A Google Cloud project with billing enabled

```bash
# Authenticate with GCP
gcloud auth application-default login

# Set your GCP project
gcloud config set project YOUR-PROJECT-ID
```

### Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Initialize Pulumi stack
pulumi stack init dev

# Configure the stack
pulumi config set gcp:project your-gcp-project-id
pulumi config set gcp:region us-central1
pulumi config set environment dev
```

### Deploy

```bash
# Preview changes
pulumi preview

# Deploy the infrastructure
pulumi up

# Get cluster information
pulumi stack output cluster_name
pulumi stack output cluster_endpoint
```

### Cleanup

```bash
# Destroy all resources
pulumi destroy
```

## ⚙️ Configuration

Edit `Pulumi.dev.yaml` or use `pulumi config set` to customize:

### Network Configuration
- `network:name` - VPC network name (default: gke-network-dev)
- `network:subnet` - Subnet name (default: gke-subnet-dev)
- `network:cidr` - Subnet CIDR range (default: 10.0.0.0/20)
- `network:pods_cidr` - Pod secondary CIDR (default: 10.4.0.0/14)
- `network:services_cidr` - Service secondary CIDR (default: 10.0.16.0/20)

### GKE Cluster Configuration
- `gke:cluster_name` - Cluster name (default: gke-cluster-dev)
- `gke:version` - Kubernetes version (default: 1.28)
- `gke:enable_network_policy` - Enable Calico network policies (default: true)
- `gke:enable_autoscaling` - Enable cluster autoscaling (default: true)

### Node Pool Configuration
- `gke:node_pool_name` - Node pool name (default: default-pool-dev)
- `gke:machine_type` - Machine type (default: n2-standard-4)
- `gke:min_node_count` - Minimum nodes (default: 2)
- `gke:max_node_count` - Maximum nodes (default: 10)
- `gke:disk_size_gb` - Node disk size (default: 100)

## 📦 Modules Overview

### network.py
Creates the foundation networking layer:
- VPC Network with custom CIDR
- Subnet with secondary IP ranges (for pods and services)
- Cloud Router for routing
- Cloud NAT for pod egress traffic

**Key Features:**
- Private Google Access enabled
- VPC Flow Logs configured
- Cloud NAT for outbound traffic

### gke.py
Deploys GKE cluster and node pools:
- Regional GKE cluster
- VPC-native networking
- Network policy support (Calico)
- Workload Identity integration
- Cloud Logging and Monitoring enabled

**Security Features:**
- Shielded GKE nodes (Secure Boot, Integrity Monitoring)
- GKE Metadata Server for Workload Identity
- Service account scoping

### iam.py
Manages identity and access:
- Service accounts for nodes
- Service accounts for pod workloads
- IAM role bindings
- Workload Identity configuration

**Default Roles:**
- `roles/logging.logWriter` - Write logs
- `roles/monitoring.metricWriter` - Write metrics
- `roles/monitoring.viewer` - Read metrics
- `roles/cloudtrace.agent` - Write traces

## 📊 Outputs

After deployment, the stack exports:

```bash
pulumi stack output cluster_name      # GKE cluster name
pulumi stack output cluster_endpoint  # Kubernetes API endpoint
pulumi stack output network_name      # VPC network name
pulumi stack output subnet_name       # Subnet name
```

## 🔐 Security Best Practices Implemented

✅ **Network Security**
- VPC-native cluster with custom CIDR ranges
- Network policies enabled for pod-to-pod communication control
- Cloud NAT for secure pod egress

✅ **Node Security**
- Shielded GKE nodes with Secure Boot
- Integrity Monitoring enabled
- Metadata server with Workload Identity

✅ **Access Control**
- Least-privilege service accounts
- IAM role bindings for logging/monitoring
- Workload Identity for pod authentication

✅ **Observability**
- Cloud Logging enabled
- Cloud Monitoring configured
- Cloud Trace agent integration

## 📚 Documentation

For more information:
- [Pulumi GCP Documentation](https://www.pulumi.com/docs/reference/pkg/gcp/)
- [GKE Best Practices](https://cloud.google.com/kubernetes-engine/docs/best-practices)
- [GCP Network Architecture](https://cloud.google.com/architecture/best-practices-for-cloud-gke-network-design)

## 🤝 Contributing

To extend this infrastructure:

1. **Add new modules** in the `modules/` directory
2. **Import and use** in `__main__.py`
3. **Add configuration** options in `config.py`
4. **Update documentation** as needed

## 📝 License

This project is provided as-is for educational and commercial use.

## 🆘 Troubleshooting

**Issue: Authentication errors**
```bash
gcloud auth application-default login
```

**Issue: Project quota exceeded**
- Check GCP quotas in Cloud Console
- Reduce node counts or machine types

**Issue: Network conflicts**
- Ensure CIDR ranges don't overlap with existing networks
- Modify `network:cidr`, `network:pods_cidr`, `network:services_cidr`

**View detailed logs:**
```bash
pulumi up --log-verbosity=9
```

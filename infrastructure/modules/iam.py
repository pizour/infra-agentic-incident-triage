import pulumi
import pulumi_gcp as gcp

def create_service_accounts(project_name: str, cluster_name: str, region: str) -> dict:
    """
    Create GCP service accounts for GKE cluster components
    
    Args:
        project_name: GCP project name
        cluster_name: Name of the GKE cluster
        region: GCP region
    
    Returns:
        Dictionary containing service account references
    """
    
    # Service account for GKE nodes
    gke_sa = gcp.serviceaccount.Account(
        f'{cluster_name}-sa',
        account_id=f'{cluster_name}-sa',
        display_name=f'Service account for {cluster_name} GKE cluster',
    )
    
    # Grant necessary roles to GKE service account
    roles = [
        'roles/logging.logWriter',
        'roles/monitoring.metricWriter',
        'roles/monitoring.viewer',
        'roles/cloudtrace.agent',
        'roles/aiplatform.user',
    ]
    
    iam_members = []
    for idx, role in enumerate(roles):
        iam_member = gcp.projects.IAMBinding(
            f'{cluster_name}-sa-{idx}',
            project=project_name,
            role=role,
            members=[gke_sa.email.apply(lambda email: f'serviceAccount:{email}')],
        )
        iam_members.append(iam_member)
    
    # Service account for Workload Identity (optional, for pod authentication)
    pods_sa = gcp.serviceaccount.Account(
        f'{cluster_name}-pods-sa',
        account_id=f'{cluster_name}-pods-sa',
        display_name=f'Service account for {cluster_name} pod workloads',
    )
    
    # IAM binding for Workload Identity
    workload_identity_binding = gcp.serviceaccount.IAMBinding(
        f'{cluster_name}-workload-identity',
        service_account_id=pods_sa.name,
        role='roles/iam.workloadIdentityUser',
        members=[
            gke_sa.email.apply(lambda email: f'serviceAccount:{email}'),
        ],
    )
    
    return {
        'gke_service_account': gke_sa,
        'pods_service_account': pods_sa,
        'iam_members': iam_members,
        'workload_identity_binding': workload_identity_binding,
    }

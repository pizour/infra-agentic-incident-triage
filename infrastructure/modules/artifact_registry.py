import pulumi
import pulumi_gcp as gcp


def create_artifact_registry(
    project_name: str,
    repository_name: str,
    region: str,
    repository_format: str = 'DOCKER',
    description: str = 'Artifact Registry for container images',
    labels: dict = None,
    service_account_emails: list = None,
) -> dict:
    """
    Create Google Artifact Registry repository for container images or other artifacts
    
    Args:
        project_name: GCP project name
        repository_name: Name of the artifact registry repository
        region: GCP region
        repository_format: Format of the repository (DOCKER, MAVEN, NPM, PYTHON, etc.)
        description: Description of the repository
        labels: Labels for the repository
        service_account_emails: List of service account emails to grant access
    
    Returns:
        Dictionary containing repository reference and IAM bindings
    """
    
    if labels is None:
        labels = {}
    
    # Create Artifact Registry Repository
    repository = gcp.artifactregistry.Repository(
        repository_name,
        project=project_name,
        location=region,
        repository_id=repository_name,
        format=repository_format,
        description=description,
        labels=labels,
    )
    
    # Create IAM bindings for service accounts to access the repository
    iam_members = []
    
    if service_account_emails:
        for idx, sa_email in enumerate(service_account_emails):
            # Convert Output to string if needed
            sa_email_value = sa_email if isinstance(sa_email, str) else sa_email.apply(lambda x: x)
            
            # Grant roles/artifactregistry.writer role using RepositoryIamMember
            writer_member = gcp.artifactregistry.RepositoryIamMember(
                f'{repository_name}-writer-member-{idx}',
                project=project_name,
                location=region,
                repository=repository.repository_id,
                role='roles/artifactregistry.writer',
                member=sa_email_value.apply(lambda email: f'serviceAccount:{email}'),
                opts=pulumi.ResourceOptions(depends_on=[repository]),
            )
            iam_members.append(writer_member)
    
    return {
        'repository': repository,
        'repository_name': repository.repository_id,
        'repository_url': repository.docker_config.apply(
            lambda config: config.get('repository_url') if config else None
        ) if repository_format == 'DOCKER' else None,
        'iam_members': iam_members,
    }

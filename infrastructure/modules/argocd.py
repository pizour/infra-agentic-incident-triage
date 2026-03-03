import pulumi
import pulumi_kubernetes as kubernetes

def create_argocd(
    cluster_name: pulumi.Output[str],
    endpoint: pulumi.Output[str],
    ca_certificate: pulumi.Output[str],
    chart_version: str = "6.7.11",
    chart_repo: str = "https://argoproj.github.io/argo-helm",
    app_of_apps_path: str = "../services/argocd-apps",
    namespace: str = "argocd"
) -> dict:
    """
    Deploy ArgoCD to GKE cluster using Helm
    """
    
    # Generate kubeconfig
    kubeconfig = pulumi.Output.all(
        cluster_name,
        endpoint,
        ca_certificate
    ).apply(lambda args: f"""apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: {args[2]}
    server: https://{args[1]}
  name: {args[0]}
contexts:
- context:
    cluster: {args[0]}
    user: {args[0]}
  name: {args[0]}
current-context: {args[0]}
kind: Config
preferences: {{}}
users:
- name: {args[0]}
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: gke-gcloud-auth-plugin
      installHint: Install gke-gcloud-auth-plugin
      provideClusterInfo: true
""")

    # Create Kubernetes provider
    k8s_provider = kubernetes.Provider(
        'gke-k8s',
        kubeconfig=kubeconfig,
    )

    # Create argocd namespace
    argocd_ns = kubernetes.core.v1.Namespace(
        "argocd-ns",
        metadata={"name": namespace},
        opts=pulumi.ResourceOptions(provider=k8s_provider)
    )

    # Deploy ArgoCD Helm chart
    argocd_chart = kubernetes.helm.v3.Release(
        "argocd",
        name="argocd",
        chart="argo-cd",
        version=chart_version,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo=chart_repo
        ),
        namespace=argocd_ns.metadata.name,
        values={
            "server": {
                "service": {
                    "type": "LoadBalancer"
                }
            }
        },
        opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[argocd_ns])
    )

    # Replace Helm Release with ArgoCD Application for self-management
    argocd_apps = kubernetes.apiextensions.CustomResource(
        "argocd-apps",
        api_version="argoproj.io/v1alpha1",
        kind="Application",
        metadata={
            "name": "argocd-apps",
            "namespace": argocd_ns.metadata.name,
        },
        spec={
            "project": "default",
            "source": {
                "repoURL": "https://github.com/pizour/infra-agentic-incident-triage.git",
                "path": "services/argocd-apps",
                "targetRevision": "main",
            },
            "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": argocd_ns.metadata.name,
            },
            "syncPolicy": {
                "automated": {
                    "prune": True,
                    "selfHeal": True,
                },
                "syncOptions": ["CreateNamespace=true"]
            },
        },
        opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[argocd_chart])
    )

    return {
        "namespace": argocd_ns.metadata.name,
        "release_name": argocd_chart.name,
        "provider": k8s_provider,
    }

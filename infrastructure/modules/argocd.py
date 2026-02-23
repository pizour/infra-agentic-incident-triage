import pulumi
import pulumi_kubernetes as kubernetes

def create_argocd(
    cluster_name: pulumi.Output[str],
    endpoint: pulumi.Output[str],
    ca_certificate: pulumi.Output[str]
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
        metadata={"name": "argocd"},
        opts=pulumi.ResourceOptions(provider=k8s_provider)
    )

    # Deploy ArgoCD Helm chart
    argocd_chart = kubernetes.helm.v3.Release(
        "argocd",
        name="argocd",
        chart="argo-cd",
        version="6.7.11",
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://argoproj.github.io/argo-helm"
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

    return {
        "namespace": argocd_ns.metadata.name,
        "release_name": argocd_chart.name,
        "provider": k8s_provider,
    }

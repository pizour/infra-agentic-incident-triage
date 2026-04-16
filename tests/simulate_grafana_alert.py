"""
Simulate Grafana alert webhooks to the AI-Orchestrator /task endpoint.

Usage:
    python tests/simulate_grafana_alert.py                          # default: localhost:8009
    python tests/simulate_grafana_alert.py --url http://host:8009   # custom URL
    python tests/simulate_grafana_alert.py --api-key SECRET         # with auth
"""

import asyncio
import argparse
from datetime import datetime, timezone

import httpx

# ── Sample alert payloads ────────────────────────────────────────────────────

ALERTS = [
    {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SSHBruteForce",
                    "severity": "critical",
                    "host": "web-server-01",
                    "host_ip": "10.0.1.15",
                    "instance": "10.0.1.15:9100",
                    "source_ip": "203.0.113.42",
                    "user": "root",
                },
                "annotations": {
                    "summary": "SSH brute force attack on web-server-01",
                    "description": "Multiple failed SSH login attempts detected",
                },
                "startsAt": datetime.now(timezone.utc).isoformat(),
                "generatorURL": "http://grafana.local/alerting/SSHBruteForce/view",
            }
        ],
    },
    # {
    #     "status": "firing",
    #     "alerts": [
    #         {
    #             "status": "firing",
    #             "labels": {
    #                 "alertname": "PodOOMKilled",
    #                 "severity": "warning",
    #                 "namespace": "ai-agent",
    #                 "pod": "ai-agent-deep-investigate-1713200000",
    #                 "host": "gke-node-pool-01",
    #                 "host_ip": "10.128.0.45",
    #             },
    #             "annotations": {
    #                 "summary": "Pod OOMKilled in ai-agent namespace",
    #                 "description": "Container 'agent' in pod ai-agent-deep-investigate-1713200000 was OOMKilled. Current memory limit: 512Mi.",
    #             },
    #             "startsAt": datetime.now(timezone.utc).isoformat(),
    #             "generatorURL": "http://grafana.local/alerting/PodOOMKilled/view",
    #         }
    #     ],
    # },
    # {
    #     "status": "firing",
    #     "alerts": [
    #         {
    #             "status": "firing",
    #             "labels": {
    #                 "alertname": "HighCPUUsage",
    #                 "severity": "warning",
    #                 "host": "db-server-03",
    #                 "host_ip": "10.0.2.20",
    #                 "instance": "10.0.2.20:9100",
    #             },
    #             "annotations": {
    #                 "summary": "High CPU usage on db-server-03",
    #                 "description": "CPU usage has been above 95% for more than 10 minutes on db-server-03.",
    #             },
    #             "startsAt": datetime.now(timezone.utc).isoformat(),
    #             "generatorURL": "http://grafana.local/alerting/HighCPUUsage/view",
    #         }
    #     ],
    # },
    # {
    #     "status": "resolved",
    #     "alerts": [
    #         {
    #             "status": "resolved",
    #             "labels": {
    #                 "alertname": "DiskSpaceLow",
    #                 "severity": "info",
    #                 "host": "app-server-02",
    #             },
    #             "annotations": {
    #                 "summary": "Disk space recovered on app-server-02",
    #                 "description": "Disk usage dropped below 80% threshold.",
    #             },
    #             "startsAt": datetime.now(timezone.utc).isoformat(),
    #             "generatorURL": "http://grafana.local/alerting/DiskSpaceLow/view",
    #         }
    #     ],
    # },
]


async def send_alert(
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    headers: dict,
) -> None:
    alert_name = payload["alerts"][0]["labels"]["alertname"]
    status = payload["status"]
    description = payload["alerts"][0]["annotations"].get("description", "")
    print(f"[>>] Sending {alert_name} (status={status}) ...")

    # Transform Grafana payload into TaskRequest format
    task_request = {
        "input": f"Alert: {alert_name}\n{description}",
        "context": {
            "source": "grafana",
            "raw_payload": payload,
            "alert_status": status,
            "alert_name": alert_name,
        }
    }

    try:
        resp = await client.post(url, json=task_request, headers=headers, timeout=30.0)
        print(f"[<<] {alert_name}: HTTP {resp.status_code} — {resp.json()}")
    except httpx.HTTPStatusError as exc:
        print(f"[!!] {alert_name}: HTTP {exc.response.status_code} — {exc.response.text}")
    except Exception as exc:
        print(f"[!!] {alert_name}: {exc}")


async def main(base_url: str, api_key: str | None) -> None:
    url = f"{base_url.rstrip('/')}/task"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    print(f"Target: {url}")
    print(f"Sending {len(ALERTS)} alert(s) concurrently ...\n")

    async with httpx.AsyncClient() as client:
        tasks = [send_alert(client, url, payload, headers) for payload in ALERTS]
        await asyncio.gather(*tasks)

    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate Grafana alerts to AI-Orchestrator")
    parser.add_argument("--url", default="http://localhost:8009", help="Orchestrator base URL")
    parser.add_argument("--api-key", default=None, help="X-API-Key for authentication")
    args = parser.parse_args()

    asyncio.run(main(args.url, args.api_key))

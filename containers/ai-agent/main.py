import os
from typing import Optional, List
from dotenv import load_dotenv

load_dotenv()

# --- OpenTelemetry / Arize Phoenix Setup ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from openinference.instrumentation.openai import OpenAIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator

# Initialize TracerProvider
provider = TracerProvider()
trace.set_tracer_provider(provider)

# Configure OTLP Exporter (sending to Phoenix)
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")
exporter = OTLPSpanExporter(endpoint=endpoint)
provider.add_span_processor(BatchSpanProcessor(exporter))

# Instrument OpenAI calls (this captures LLM inputs/outputs sent to Ollama via OpenAIProvider)
OpenAIInstrumentor().instrument()
HTTPXClientInstrumentor().instrument()
# ---------------------------------------------

from fastapi import FastAPI, HTTPException, Security, status, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

# --- SlowAPI Setup ---
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AI Agent API (Ollama/Llama)")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

GUARDRAILS_URL = os.getenv("GUARDRAILS_URL", "http://guardrails.ai-agent.svc.cluster.local:8080")

async def guardrails_check(message: str) -> tuple[str, bool]:
    """Send message through the guardrails service. Returns (content, blocked)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{GUARDRAILS_URL}/check", json={"message": message})
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content", message)
                blocked = data.get("blocked", False)
                return content, blocked
    except Exception as e:
        print(f"Guardrails service unreachable, passing through: {e}")
    return message, False

# --- MCP Client Setup ---
from mcp import ClientSession
from mcp.client.sse import sse_client
import httpx

MCP_SERVER_URL = "http://linux-mcp-server:8001/sse"
MCP_API_KEY = os.getenv("MCP_API_KEY")

# NetBox MCP
NETBOX_MCP_URL = "http://netbox-mcp-server:8002/sse"
NETBOX_MCP_API_KEY = os.getenv("NETBOX_MCP_API_KEY")

# ---------------------------------------------

FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)

# Authentication Setup
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

def get_api_key(api_key_header: str = Security(api_key_header)):
    expected_key = os.getenv("APP_API_KEY")
    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="APP_API_KEY is not configured on the server",
        )
    if api_key_header and api_key_header == expected_key:
        return api_key_header
    
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Could not validate credentials",
    )

# Model and Agent Setup — Ollama via OpenAI-compatible API
model_name = os.getenv("LLM_MODEL", "llama3.1:8b")
ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama-stack-ollama:11434/v1")

ollama_provider = OpenAIProvider(
    base_url=ollama_base_url,
    api_key="ollama",  # Ollama doesn't need a real key, but the client requires one
)
model = OpenAIChatModel(model_name, provider=ollama_provider)

agent = Agent(
    model,
    system_prompt=(
        "You are a strict, concise security AI for cloud infrastructure. "
        "You MUST call tools to investigate alerts. DO NOT output conversational plans or explain steps."
        "ONLY use the tools provided. If a tool fails, report the error and create a ticket."
    ),
)

class AgentRequest(BaseModel):
    prompt: str

class AgentResponse(BaseModel):
    result: str

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "AI Agent API (Ollama/Llama) is running"}

@app.post("/agent", response_model=AgentResponse)
async def run_agent(request: AgentRequest, api_key: str = Security(get_api_key)):
    """Standard agent endpoint for manual queries, with guardrails."""
    try:
        safe_prompt, blocked = await guardrails_check(request.prompt)
        if blocked:
            raise HTTPException(status_code=400, detail=f"Blocked by guardrails: {safe_prompt}")
        result = await agent.run(safe_prompt)
        return AgentResponse(result=str(result.output))
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error running agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- NetBox MCP Tool ---

# @agent.tool
# async def lookup_device_in_netbox(ctx: RunContext[None], hostname: str) -> str:
#     """Look up a device in NetBox CMDB by hostname. Returns device info including IP address."""
#     headers = {"X-MCP-API-Key": NETBOX_MCP_API_KEY}
#     try:
#         async with sse_client(NETBOX_MCP_URL, headers=headers) as (read_stream, write_stream):
#             async with ClientSession(read_stream, write_stream) as session:
#                 await session.initialize()
#                 result = await session.call_tool("lookup_device", arguments={"name": hostname})
#                 return str(result.content[0].text) if result.content else "No device found."
#     except Exception as e:
#         return f"NetBox lookup error: {e}"

# --- MCP Investigation Tools ---

@agent.tool
async def investigate_logs(ctx: RunContext[None], host: str, lines: int = 20) -> str:
    """
    Investigate system auth logs on a remote host via the Linux MCP proxy.
    """
    headers = {"X-MCP-API-Key": MCP_API_KEY}
    try:
        async with sse_client(MCP_SERVER_URL, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                command = f"tail -n {lines} /var/log/auth.log"
                result = await session.call_tool("execute_command", arguments={"command": command, "host": host})
                return str(result.content[0].text) if result.content else "No logs returned."
    except Exception as e:
        return f"Investigation error: {e}"

@agent.tool
async def check_system_stats(ctx: RunContext[None], host: str) -> str:
    """
    Check CPU and Memory usage on a remote host via the Linux MCP proxy.
    """
    headers = {"X-MCP-API-Key": MCP_API_KEY}
    try:
        async with sse_client(MCP_SERVER_URL, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                command = "top -bn1 | head -n 20"
                result = await session.call_tool("execute_command", arguments={"command": command, "host": host})
                return str(result.content[0].text) if result.content else "No stats returned."
    except Exception as e:
        return f"Stats error: {e}"

@agent.tool
async def list_active_connections(ctx: RunContext[None], host: str, port: Optional[int] = None) -> str:
    """
    Lists active network connections on a remote host via the Linux MCP proxy.
    Optional: provide a port to filter results.
    """
    headers = {"X-MCP-API-Key": MCP_API_KEY}
    try:
        async with sse_client(MCP_SERVER_URL, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                command = "ss -tuln"
                if port:
                    command += f" | grep :{port}"

                result = await session.call_tool("execute_command", arguments={"command": command, "host": host})
                return str(result.content[0].text) if result.content else "No connections returned."
    except Exception as e:
        return f"Connections error: {e}"

@agent.tool
async def execute_remote_command(ctx: RunContext[None], host: str, command: str) -> str:
    """
    Executes an arbitrary shell command on a remote Linux host.
    Use this for custom investigations not covered by other tools.
    """
    headers = {"X-MCP-API-Key": MCP_API_KEY}
    try:
        async with sse_client(MCP_SERVER_URL, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool("execute_command", arguments={"command": command, "host": host})
                return str(result.content[0].text) if result.content else "Command executed, no output."
    except Exception as e:
        return f"Remote command error: {e}"

@agent.tool
async def create_zammad_ticket(ctx: RunContext[None], summary: str, risk_level: str) -> str:
    """
    Create a security incident ticket in Zammad.
    ONLY use this if the investigation confirms a real and critical threat.
    CRITICAL: The `summary` parameter MUST contain the FULL, detailed investigation report, including all evidence found (IPs, log lines) and a detailed remediation plan. Do not just put a short sentence.
    """
    zammad_url = os.getenv("ZAMMAD_URL", "http://zammad.zammad.svc.cluster.local:8080")
    zammad_token = os.getenv("ZAMMAD_TOKEN")
    zammad_user = os.getenv("ZAMMAD_USER")
    zammad_pass = os.getenv("ZAMMAD_PASS")

    if not zammad_token and not (zammad_user and zammad_pass):
        return "Ticket creation skipped: neither ZAMMAD_TOKEN nor ZAMMAD_USER/ZAMMAD_PASS set."

    url = f"{zammad_url}/api/v1/tickets"
    priority = "3 high" if risk_level.lower() == "critical" else "2 normal"
    customer_id = int(os.getenv("ZAMMAD_CUSTOMER_ID", "3"))
    payload = {
        "title": f"[AI Alert] Security Incident: {risk_level} threat detected",
        "group": "Users",
        "customer_id": customer_id,
        "article": {
            "subject": "AI Agent Investigation Findings & Summary",
            "body": f"The AI Security Agent has completed an investigation and determined this is a {risk_level} threat.\n\n### AI Findings & Summary:\n{summary}",
            "type": "note",
            "internal": False
        },
        "state": "new",
        "priority": priority
    }
    headers = {"Content-Type": "application/json"}
    auth = None
    if zammad_user and zammad_pass:
        auth = (zammad_user, zammad_pass)
    elif zammad_token:
        headers["Authorization"] = f"Token token={zammad_token}"

    tracer = trace.get_tracer("ai-agent.zammad")
    async with httpx.AsyncClient(timeout=30.0) as client:
        with tracer.start_as_current_span("zammad.create_ticket") as span:
            span.set_attribute("zammad.url", zammad_url)
            span.set_attribute("zammad.risk_level", risk_level)
            span.set_attribute("zammad.priority", priority)
            span.set_attribute("incident.summary", summary[:500])
            try:
                response = await client.post(url, json=payload, headers=headers, auth=auth)
                span.set_attribute("http.status_code", response.status_code)
                if response.status_code in (200, 201):
                    ticket = response.json()
                    ticket_id = ticket.get("id", "unknown")
                    ticket_number = ticket.get("number", "unknown")
                    span.set_attribute("zammad.ticket_id", ticket_id)
                    span.set_attribute("zammad.ticket_number", ticket_number)
                    span.set_attribute("outcome", "ticket_created")
                    print(f"ZAMMAD TICKET CREATED: #{ticket_number} (id: {ticket_id})")
                    return f"Incident ticket #{ticket_number} created in Zammad (id: {ticket_id})."
                else:
                    error_msg = response.text[:200]
                    span.set_attribute("outcome", "http_error")
                    span.set_attribute("error.message", error_msg)
                    return f"Failed to create Zammad ticket: HTTP {response.status_code} - {error_msg}"
            except Exception as e:
                span.set_attribute("outcome", "exception")
                span.set_attribute("error.message", str(e))
                return f"Failed to create Zammad ticket: {e}"

# ---------------------------------------------

@app.post("/webhook")
@limiter.limit("5/minute")
async def handle_alert(request: Request, payload: dict):
    """
    Webhook receiver for Grafana alerts.
    Extracts hostname from alert, looks it up in NetBox for IP, then investigates.
    """
    alert_status = payload.get("status", "unknown")
    alerts = payload.get("alerts", [])
    
    if alert_status == "firing" and alerts:
        alert = alerts[0]
        alert_desc = alert.get("annotations", {}).get("description", "No description")
        
        # Extract hostname from alert labels or annotations
        hostname = (
            alert.get("labels", {}).get("host")
            or alert.get("labels", {}).get("hostname")
            or alert.get("labels", {}).get("instance", "").split(":")[0]
            or "unknown"
        )
        
        prompt = (
            f"ALERT: {alert_desc} on '{hostname}'.\n"
            "Investigate this alert.\n"
            "CRITICAL RULES:\n"
            "1. You MUST use all available tools to investigate.\n"
            "2. You MUST ALWAYS use the `create_zammad_ticket` tool to raise a ticket, regardless of the findings.\n"
            "3. Do NOT write your thought process.\n"
            "4. Your final text response must be a max 500 characters summary of findings.\n"
        )
        
        try:
            result = await agent.run(prompt)
            output_text = str(result.output).strip().replace("```json", "").replace("```", "")
            
            import json
            try:
                parsed = json.loads(output_text)
                analysis = parsed.get("summary", output_text)
            except Exception:
                analysis = output_text
                
            # Fallback: if the LLM states it's critical but failed to call the tool natively
            if "critical" in analysis.lower():
                try:
                    ticket_res = await create_zammad_ticket(None, analysis[:250], "Critical")
                    analysis += f"\n\n[System] Auto-escalated to Zammad: {ticket_res}"
                except Exception as e:
                    analysis += f"\n\n[System] Failed to auto-escalate to Zammad: {e}"
            guarded_analysis, blocked = await guardrails_check(f"Verified investigation: {analysis}")
            if blocked:
                print(f"GUARDRAILS BLOCKED OUTPUT: {guarded_analysis}")
                return {"status": "blocked", "hostname": hostname, "reason": guarded_analysis}
            print(f"INVESTIGATION COMPLETE: {guarded_analysis}")
            return {"status": "investigated", "hostname": hostname, "analysis": guarded_analysis}
        except Exception as e:
            print(f"Error in investigation: {e}")
            return {"status": "error", "message": str(e)}
    
    return {"status": "ignored"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

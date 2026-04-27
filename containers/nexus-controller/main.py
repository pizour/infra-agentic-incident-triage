import os
import json
import httpx
import base64
import asyncio
import jwt
import time
from typing import Optional, List, Dict, Any, Literal
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from loguru import logger

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel

load_dotenv()

# --- OpenTelemetry / Arize Phoenix Setup ---
from opentelemetry import trace, propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import attach, detach
from prometheus_fastapi_instrumentator import Instrumentator
import httpx

from langfuse.opentelemetry import LangfuseSpanProcessor

resource = Resource.create({
    SERVICE_NAME: "nexus-controller",
    "openinference.project.name": "ai-agent-triage",
})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# Register W3C propagators so incoming traceparent headers are honoured
propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))

# OpenInference enrichment FIRST — must run before exporters so spans have LLM attributes
provider.add_span_processor(OpenInferenceSpanProcessor())

# Arize Phoenix OTLP Export (all spans)
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://monitoring-phoenix.monitoring.svc.cluster.local:6006/v1/traces")
try:
    phoenix_exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(SimpleSpanProcessor(phoenix_exporter))
    logger.info(f"Phoenix OTLP exporter initialized: {endpoint}")
except Exception as e:
    logger.error(f"Failed to initialize Phoenix exporter: {e}")

# Langfuse via SDK SpanProcessor (auto-filters to LLM spans only)
provider.add_span_processor(LangfuseSpanProcessor())
logger.info("Langfuse SpanProcessor enabled")

HTTPXClientInstrumentor().instrument()

# --- Configuration ---
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.5-flash")
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "http://github-mcp-server.ai-agent.svc.cluster.local:8080/mcp")
GITHUB_REPO = os.getenv("GITHUB_REPO", "pizour/infra-agentic-incident-triage")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
MCP_API_KEY = os.getenv("MCP_API_KEY", "")
GH_PERSONAL_ACCESS_TOKEN = os.getenv("GH_PERSONAL_ACCESS_TOKEN", "")

# --- GitHub OAuth Setup ---
GH_OAUTH_APP_ID = os.getenv("GH_OAUTH_APP_ID", "")
GH_OAUTH_PRIVATE_KEY = os.getenv("GH_OAUTH_PRIVATE_KEY", "")
GH_OAUTH_INSTALLATION_ID = os.getenv("GH_OAUTH_INSTALLATION_ID", "")

async def get_github_oauth_token() -> Optional[str]:
    """Get a GitHub App installation access token using OAuth credentials."""
    if not all([GH_OAUTH_APP_ID, GH_OAUTH_PRIVATE_KEY, GH_OAUTH_INSTALLATION_ID]):
        return None

    try:
        # Create JWT from App private key
        now = int(time.time())
        payload = {
            "iss": GH_OAUTH_APP_ID,
            "iat": now,
            "exp": now + 600,  # 10 minutes
        }
        jwt_token = jwt.encode(payload, GH_OAUTH_PRIVATE_KEY, algorithm="RS256")

        # Get installation access token
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.github.com/app/installations/{GH_OAUTH_INSTALLATION_ID}/access_tokens",
                headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"},
                timeout=10.0,
            )
            if response.status_code == 201:
                data = response.json()
                return data.get("token")
    except Exception as e:
        logger.warning(f"Failed to get GitHub OAuth token: {e}")

    return None

app = FastAPI(title="Nexus-Controller (Pydantic-AI)")
FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
Instrumentator().instrument(app).expose(app)

model = GoogleModel(MODEL_NAME, provider="google-vertex")

# --- Registry cache (loaded once at startup) ---
_registry_cache: Dict[str, str] = {}

async def _fetch_file_from_github(path: str) -> str:
    """Fetch a raw file from GitHub via MCP and return its decoded text content."""
    owner, repo = GITHUB_REPO.split('/')
    gh_token = await get_github_oauth_token() or GH_PERSONAL_ACCESS_TOKEN
    headers = {"Content-Type": "application/json"}
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    payload = {
        "jsonrpc": "2.0",
        "id": "startup",
        "method": "tools/call",
        "params": {
            "name": "get_file_contents",
            "arguments": {"owner": owner, "repo": repo, "path": path},
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(GITHUB_MCP_URL, json=payload, headers=headers, timeout=30.0)
        resp.raise_for_status()
        for line in resp.text.strip().splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if "result" in data:
                    content_blocks = data["result"].get("content", [])
                    # Prefer resource blocks — they contain the actual file text
                    for block in content_blocks:
                        if block.get("type") == "resource":
                            return block.get("resource", {}).get("text", "")
                    # Fallback: text block with base64-encoded content (older MCP versions)
                    for block in content_blocks:
                        text = block.get("text", "")
                        try:
                            inner = json.loads(text)
                            raw = inner.get("content", "")
                            if raw:
                                return base64.b64decode(raw).decode("utf-8")
                        except Exception:
                            pass
    return ""


@app.on_event("startup")
async def load_registries():
    """Fetch agent and skill registries from GitHub once and cache them."""
    for key, path in [
        ("agents", "agents/REGISTRY.md"),
        ("skills", "skills/REGISTRY.md"),
    ]:
        try:
            content = await _fetch_file_from_github(path)
            _registry_cache[key] = content
            logger.info(f"Registry loaded: {path} ({len(content)} chars)")
        except Exception as e:
            logger.error(f"Failed to load registry {path}: {e}")
            _registry_cache[key] = f"(unavailable: {e})"

class AgentValidationResult(BaseModel):
    agent_key: str
    agent_class: str  # "control-plane" | "interaction" | "specialist"
    accuracy: float
    correctness: float
    completeness: float
    safety_check: bool
    reasoning: str = Field(..., max_length=50)
    data: Optional[dict] = None

class NexusRoutingDecision(BaseModel):
    action: Literal["next_agent", "retry", "finish"]
    feedback: str
    target_agent: Optional[str] = None
    agent_env_vars: Optional[dict] = None  # env_vars from agent .md frontmatter (e.g. SYSTEM_PROMPT)

BASE_ROUTER_SYSTEM_PROMPT = """You are the Nexus Controller. Evaluate the incoming validation state and produce a NexusRoutingDecision.

The `action` field MUST be exactly one of: "next_agent", "retry", "finish". No other values are valid.

## Registries (pre-loaded — do NOT use the github tool to discover agents or skills)

The agent and skill registries are injected below. Use them as your sole source of truth for routing_key values.
Only use the github tool to read a specific agent or skill file AFTER you have selected it.

{agent_registry}

{skill_registry}

## agent_env_vars

When action is "next_agent", you MUST include the full env_vars block from that agent's .md file.
Use the github tool to read the specific agent .md file (e.g. agents/specialists/vm-tshooter.md) ONLY after you have decided on a target_agent.
Copy the YAML env_vars block as a dict into agent_env_vars. If no env_vars exist, set null.

## Understanding latest_validation

The `latest_validation` field contains `agent_key` (which agent ran) and `raw` (the agent's full text output).
The `raw` field may contain a JSON object with `accuracy`, `correctness`, `completeness`, `safety_check` fields
as defined by the agent output contract. Parse these from the raw text if present.

## Routing Rules (apply in order)

1. First request (latest_validation is null or empty):
   - Set action="next_agent" with the appropriate investigation agent.
   - VM/Linux alerts → target_agent="vm_tshooter"
   - Kubernetes/GKE alerts → target_agent="k8s_tshooter"

2. Safety: if safety_check is explicitly False in the parsed output → action="finish".

3. Grafana pipeline (context contains source=grafana):
   Apply this fixed sequence — always advance to the next step when the current step completes:
   - If latest agent_key is "vm_tshooter" or "k8s_tshooter" → action="next_agent", target_agent="create_ticket"
   - If latest agent_key is "create_ticket" → action="finish"
   Only action="retry" if the output is clearly an error or empty (max once per agent).

4. Non-grafana source:
   - If quality scores are present and all >= 0.8 → action="finish" with summary.
   - If scores are low → action="retry" (max once), then action="finish".

## Error handling
If the github tool fails with 'failed after 3 attempts', set action="finish", feedback="Tool failures prevent routing decision", target_agent=null.
"""

def build_system_prompt() -> str:
    agent_reg = _registry_cache.get("agents", "(not loaded)")
    skill_reg = _registry_cache.get("skills", "(not loaded)")
    return BASE_ROUTER_SYSTEM_PROMPT.format(
        agent_registry=f"### Agent Registry\n\n{agent_reg}",
        skill_registry=f"### Skill Registry\n\n{skill_reg}",
    )

agent = Agent(
    model,
    output_type=NexusRoutingDecision,
    instrument=True,
)

@agent.system_prompt
def dynamic_system_prompt() -> str:
    """Returns the system prompt with registry content injected at call time."""
    return build_system_prompt()

@agent.tool
async def github(
    ctx: RunContext[None],
    tool: str,
    path: Optional[str] = None,
) -> str:
    """
    Call GitHub MCP tools. The agent specifies which tool to use.
    Examples:
      - tool='get_file_contents', path='/skills/nexus_routing/skill.md'
      - tool='get_file_contents', path='agents/'  (works for directories too)
    """
    # Normalize: the only valid GitHub MCP tool for reads is get_file_contents
    mcp_tool = "get_file_contents"
    logger.info(f"GITHUB TOOL CALL: tool={tool} -> {mcp_tool}, path={path}")

    if not path:
        return "Error: 'path' required"

    owner, repo = GITHUB_REPO.split('/')

    # Try to get OAuth token first, fallback to PAT
    gh_token = await get_github_oauth_token()
    if not gh_token:
        gh_token = GH_PERSONAL_ACCESS_TOKEN

    # Build arguments based on tool
    arguments = {
        "owner": owner,
        "repo": repo,
    }

    arguments["path"] = path

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            headers = {
                "Content-Type": "application/json",
            }
            if gh_token:
                headers["Authorization"] = f"Bearer {gh_token}"

            json_rpc_request = {
                "jsonrpc": "2.0",
                "id": f"call-{attempt}",
                "method": "tools/call",
                "params": {
                    "name": mcp_tool,
                    "arguments": arguments,
                }
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    GITHUB_MCP_URL,
                    json=json_rpc_request,
                    headers=headers,
                    timeout=30.0,
                )

                await asyncio.sleep(2.0)

                logger.debug(f"MCP response status: {response.status_code}")
                logger.debug(f"MCP response body: {response.text[:200]}")

                if response.status_code == 202:
                    logger.debug("MCP request accepted (202)")
                    if attempt < max_retries:
                        logger.info(f"Waiting for async response (attempt {attempt}/{max_retries})...")
                        await asyncio.sleep(1.0)
                        continue
                    return "MCP request accepted but no response received"

                elif response.status_code == 200:
                    if not response.text:
                        if attempt < max_retries:
                            logger.warning(f"Empty response (attempt {attempt}/{max_retries}). Retrying...")
                            await asyncio.sleep(0.5)
                            continue
                        return "Empty response from MCP server"

                    try:
                        lines = response.text.strip().split('\n')
                        json_data = None

                        for line in lines:
                            if line.startswith('data: '):
                                json_str = line[6:]
                                json_data = json.loads(json_str)
                                break

                        if not json_data:
                            logger.warning(f"No data line found in SSE response")
                            if attempt < max_retries:
                                await asyncio.sleep(0.5)
                                continue
                            return "No data in SSE response"

                        if "error" in json_data:
                            error_msg = json_data["error"].get("message", str(json_data["error"]))
                            if attempt < max_retries:
                                logger.warning(f"MCP error (attempt {attempt}/{max_retries}): {error_msg}. Retrying...")
                                await asyncio.sleep(0.5)
                                continue
                            return f"MCP Error (failed after {max_retries} attempts): {error_msg}"

                        # Return raw JSON result for agent to process
                        if "result" in json_data:
                            result_json = json.dumps(json_data["result"])
                            logger.info(f"MCP call successful, result: {result_json[:150]}...")
                            return result_json
                        else:
                            return f"Unexpected response format: {json_data}"

                    except Exception as json_err:
                        logger.warning(f"Failed to parse response: {json_err}")
                        if attempt < max_retries:
                            await asyncio.sleep(0.5)
                            continue
                        return f"Failed to parse MCP response: {str(json_err)}"
                else:
                    if attempt < max_retries:
                        logger.warning(f"MCP HTTP {response.status_code} (attempt {attempt}/{max_retries}). Retrying...")
                        await asyncio.sleep(0.5)
                        continue
                    return f"MCP HTTP Error {response.status_code}: {response.text}"

        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"GitHub MCP attempt {attempt}/{max_retries} exception: {str(e)}. Retrying...")
                await asyncio.sleep(0.5)
                continue
            logger.error(f"GitHub MCP call failed after {max_retries} attempts: {e}")
            return f"Exception during GitHub MCP call (failed after {max_retries} attempts): {str(e)}"

    return "Failed after retries"

class RunRequest(BaseModel):
    input: str
    context_summary: str = ""
    latest_validation: Optional[Dict[str, Any]] = None

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Flushing OpenTelemetry spans to Phoenix...")
    provider.force_flush(timeout_millis=5000)

@app.post("/run")
async def run_nexus_controller(run_request: RunRequest, raw_request: Request):
    logger.info(f"NEXUS CONTROLLER REQUEST: {run_request.input[:100]}...")
    # Extract the traceparent from the orchestrator so all spans share one trace
    upstream_ctx = propagate.extract(dict(raw_request.headers))
    token = attach(upstream_ctx)
    try:
        prompt = f"Goal: {run_request.input}\nContext: {run_request.context_summary}\nLatest Validation: {json.dumps(run_request.latest_validation or {})}"
        result = await asyncio.wait_for(
            agent.run(prompt),
            timeout=180.0,
        )

        # Robustly handle result attribute (Pydantic-AI 0.x uses .data, 1.x uses .output)
        decision = getattr(result, "output", getattr(result, "data", None))
        if decision is None:
            raise AttributeError(f"AgentRunResult has neither 'output' nor 'data': {dir(result)}")

        logger.info(f"ROUTING DECISION COMPLETE: {decision.action}")
        return decision.model_dump()

    except asyncio.TimeoutError:
        logger.error("NEXUS CONTROLLER: agent.run exceeded 80s timeout, returning finish")
        return NexusRoutingDecision(action="finish", feedback="Routing decision timed out", target_agent=None).model_dump()
    except Exception as e:
        logger.error(f"Controller execution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        detach(token)

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Router-Agent Pydantic-AI running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)

import os
import json
import httpx
import base64
import asyncio
import jwt
import time
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from loguru import logger

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel

load_dotenv()

# --- OpenTelemetry / Arize Phoenix Setup ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator
import httpx

# from langfuse import Langfuse
# from langfuse.opentelemetry import LangfuseExporter

resource = Resource.create({SERVICE_NAME: "nexus-controller"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# Arize Phoenix OTLP Export
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://monitoring-phoenix.monitoring.svc.cluster.local:6006/v1/traces")
try:
    phoenix_exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(phoenix_exporter))
    logger.info(f"Phoenix OTLP exporter initialized: {endpoint}")
except Exception as e:
    logger.error(f"Failed to initialize Phoenix exporter: {e}")

# Langfuse native exporter (uses /api/public/ingestion, works with Langfuse v2+)
# langfuse_host = os.getenv("LANGFUSE_HOST", "http://langfuse.ai-agent.svc.cluster.local:3000")
# langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
# langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")
#
# if langfuse_public_key and langfuse_secret_key:
#     langfuse_exporter = LangfuseExporter(
#         public_key=langfuse_public_key,
#         secret_key=langfuse_secret_key,
#         host=langfuse_host,
#     )
#     provider.add_span_processor(BatchSpanProcessor(langfuse_exporter))
#     logger.info(f"Langfuse exporter initialized (native SDK) targeting {langfuse_host}")

provider.add_span_processor(OpenInferenceSpanProcessor())
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
    action: str
    feedback: str
    target_agent: Optional[str] = None

ROUTER_SYSTEM_PROMPT = (
    "You are the Nexus Controller. Your task is to evaluate the provided validation state and make a routing decision.\n"
    "CRITICAL: You MUST use your 'github' tool to read 'agents/control-plane/nexus-controller.md' and follow the operating procedures defined there to the detail.\n"
    "IMPORTANT: If the github tool fails with 'failed after 3 attempts', you MUST return action='finish' to gracefully end the workflow. Do NOT retry or get stuck.\n"
    "Return NexusRoutingDecision with: action='finish', feedback='Tool failures prevent routing decision', target_agent=None\n"
)

agent = Agent(
    model,
    output_type=NexusRoutingDecision,
    system_prompt=ROUTER_SYSTEM_PROMPT,
    instrument=True,
)

@agent.tool
async def github(
    ctx: RunContext[None],
    action: str,
    path: Optional[str] = None,
) -> str:
    """
    Interact with the GitHub repository via hosted MCP endpoint.
    Actions:
      - read_file / get_file_contents: Read content of a file
      - list_files / list_directory: List files in a directory
    """
    logger.info(f"GITHUB MCP CALL: action={action}, path={path}")

    if action not in ["read_file", "get_file_contents", "list_files", "list_directory"]:
        return f"Unsupported action: {action}. Use 'read_file', 'get_file_contents', 'list_files', or 'list_directory'"

    if not path:
        return "Error: 'path' required"

    owner, repo = GITHUB_REPO.split('/')

    # Try to get OAuth token first, fallback to PAT
    gh_token = await get_github_oauth_token()
    if not gh_token:
        gh_token = GH_PERSONAL_ACCESS_TOKEN

    # Determine MCP tool name
    mcp_tool_name = "get_file_contents"
    if action in ["list_files", "list_directory"]:
        mcp_tool_name = "list_directory"

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            # Send JSON-RPC request to MCP server via POST
            headers = {
                "Content-Type": "application/json",
            }
            if gh_token:
                headers["Authorization"] = f"Bearer {gh_token}"

            # Build arguments based on action
            arguments = {
                "owner": owner,
                "repo": repo,
                "path": path,
            }

            # JSON-RPC 2.0 request
            json_rpc_request = {
                "jsonrpc": "2.0",
                "id": f"call-{attempt}",
                "method": "tools/call",
                "params": {
                    "name": mcp_tool_name,
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

                # Add delay after tool reply
                await asyncio.sleep(2.0)

                logger.debug(f"MCP response status: {response.status_code}, content length: {len(response.text)}")
                logger.debug(f"MCP response body: {response.text[:200]}")

                if response.status_code == 202:
                    # Accepted - request was received
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
                        # Parse SSE format (event: message\ndata: {...JSON...})
                        lines = response.text.strip().split('\n')
                        json_data = None

                        for line in lines:
                            if line.startswith('data: '):
                                json_str = line[6:]  # Remove 'data: ' prefix
                                json_data = json.loads(json_str)
                                break

                        if not json_data:
                            logger.warning(f"No data line found in SSE response")
                            if attempt < max_retries:
                                await asyncio.sleep(0.5)
                                continue
                            return "No data in SSE response"

                        if "result" in json_data:
                            result = json_data["result"]

                            # Handle list_directory responses
                            if action in ["list_files", "list_directory"]:
                                # For directory listings, format the output
                                if isinstance(result, list):
                                    items = []
                                    for item in result:
                                        if isinstance(item, dict):
                                            # Typical directory entry
                                            name = item.get("name", item.get("path", ""))
                                            file_type = item.get("type", "file")
                                            items.append(f"- {name} ({file_type})")
                                        else:
                                            items.append(f"- {str(item)}")
                                    return "Files in directory:\n" + "\n".join(items)
                                elif isinstance(result, dict) and "content" in result:
                                    # Handle if wrapped in content
                                    items = []
                                    for item in result.get("content", []):
                                        if isinstance(item, dict):
                                            name = item.get("name", item.get("path", ""))
                                            items.append(f"- {name}")
                                        else:
                                            items.append(f"- {str(item)}")
                                    return "Files in directory:\n" + "\n".join(items)
                                else:
                                    return f"Directory listing: {str(result)}"

                            # Handle file content responses
                            content_list = result.get("content", []) if isinstance(result, dict) else result

                            # Extract actual file content
                            text_parts = []
                            for item in content_list:
                                if isinstance(item, dict):
                                    item_type = item.get("type")

                                    if item_type == "text":
                                        text = item.get("text", "")
                                        # Skip metadata messages, include actual content
                                        if text and len(text) > 100:  # Real content is usually longer
                                            text_parts.append(text)
                                    elif item_type == "resource":
                                        # Extract file content from nested resource object
                                        resource = item.get("resource", {})
                                        if isinstance(resource, dict):
                                            file_content = resource.get("text", "")
                                            if file_content:
                                                text_parts.append(file_content)

                            if text_parts:
                                content = "\n".join(text_parts)
                                logger.info(f"Extracted content ({len(content)} bytes): {content[:150]}...")
                                return content
                            else:
                                logger.error(f"No content extracted from response")
                                return "No file content in result"

                        elif "error" in json_data:
                            error_msg = json_data["error"].get("message", str(json_data["error"]))
                            if attempt < max_retries:
                                logger.warning(f"MCP error (attempt {attempt}/{max_retries}): {error_msg}. Retrying...")
                                await asyncio.sleep(0.5)
                                continue
                            return f"MCP Error (failed after {max_retries} attempts): {error_msg}"
                        else:
                            logger.debug(f"Unexpected response format: {json_data}")
                            if attempt < max_retries:
                                await asyncio.sleep(0.5)
                                continue
                            return f"Unexpected MCP response format: {json_data}"
                    except Exception as json_err:
                        logger.warning(f"Failed to parse response: {json_err}, response: {response.text[:100]}")
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

    return "Failed to retrieve file after retries"

class RunRequest(BaseModel):
    input: str
    context_summary: str = ""
    latest_validation: Optional[Dict[str, Any]] = None

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Flushing OpenTelemetry spans to Phoenix...")
    provider.force_flush(timeout_millis=5000)

@app.post("/run")
async def run_nexus_controller(request: RunRequest):
    logger.info(f"NEXUS CONTROLLER REQUEST: {request.input[:100]}...")
    try:
        prompt = f"Goal: {request.input}\nContext: {request.context_summary}\nLatest Validation: {json.dumps(request.latest_validation or {})}"
        result = await agent.run(prompt)
        
        # Robustly handle result attribute (Pydantic-AI 0.x uses .data, 1.x uses .output)
        decision = getattr(result, "output", getattr(result, "data", None))
        if decision is None:
            raise AttributeError(f"AgentRunResult has neither 'output' nor 'data': {dir(result)}")

        logger.info(f"ROUTING DECISION COMPLETE: {decision.action}")
        return decision.model_dump()

    except Exception as e:
        logger.error(f"Controller execution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Router-Agent Pydantic-AI running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)

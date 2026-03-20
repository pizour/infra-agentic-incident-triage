import os
import subprocess
import asyncio
import asyncssh
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from typing import Optional, Annotated

# --- OpenTelemetry / Arize Phoenix Setup ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# Initialize TracerProvider with Service Name
resource = Resource.create({SERVICE_NAME: "linux-mcp-server"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__)

# Configure OTLP Exporter (sending to Phoenix)
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")
exporter = OTLPSpanExporter(endpoint=endpoint)
provider.add_span_processor(BatchSpanProcessor(exporter))
# ---------------------------------------------


REMOTE_USER = os.getenv("REMOTE_USER", "testuser")
REMOTE_PASS = os.getenv("REMOTE_PASS")

#ANTIGRAVITY BUG
# --- THE FINAL FIX: Target the ServerSession subclass ---
# import mcp.server.session

# # 1. Grab the handler from the SERVER session, not the base session
# original_receive = mcp.server.session.ServerSession._received_request

# # 2. Define the auto-initializer
# async def auto_init_receive(self, responder):
#     # Force initialization state so the strict check at session.py:383 passes
#     if hasattr(self, "_initialization_state"):
#         enum_class = type(self._initialization_state)
#         if hasattr(enum_class, "Initialized"):
#             self._initialization_state = enum_class.Initialized
            
#     # Fallback for older SDK versions
#     if hasattr(self, "_initialized"):
#         self._initialized = True
        
#     return await original_receive(self, responder)

# # 3. Apply the patch to the correct subclass
# mcp.server.session.ServerSession._received_request = auto_init_receive
# --------------------------------------------------------
mcp = FastMCP(
    "linux-server",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
)


async def run_command(command: str, host: str) -> str:
    """Executes a command on a remote host via SSH."""
    try:
        connect_kwargs = {}
        if REMOTE_PASS:
            connect_kwargs['password'] = REMOTE_PASS
        
        async with asyncssh.connect(host, username=REMOTE_USER, **connect_kwargs, known_hosts=None) as conn:
            result = await conn.run(command)
            return result.stdout if result.exit_status == 0 else f"Remote error ({result.exit_status}): {result.stderr}"
    except Exception as e:
        return f"SSH Connection Error to {host}: {str(e)}"

@mcp.tool()
async def execute_command(
    command: Annotated[str, "The shell command to execute (e.g., 'ls -la' or 'df -h')"], 
    host: Annotated[str, "The IP address or hostname of the remote Linux server"]
) -> str:
    """
    Executes an arbitrary shell command on a remote host via SSH.
    """
    with tracer.start_as_current_span("mcp.execute_command") as span:
        span.set_attribute("mcp.command", command)
        span.set_attribute("mcp.host", host)
        print(f"DEBUG: execute_command called with command='{command}', host='{host}'")
        result = await run_command(command, host)
        span.set_attribute("mcp.success", "SSH Connection Error" not in result and "Remote error" not in result)
        return result

# Expose the ASGI app for uvicorn with auth middleware
app = mcp.sse_app()
FastAPIInstrumentor.instrument_app(app)

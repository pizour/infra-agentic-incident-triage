import os
import httpx
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# --- OpenTelemetry / Arize Phoenix Setup ---
import os
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

# Initialize TracerProvider with Service Name
resource = Resource.create({SERVICE_NAME: "netbox-mcp-server"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__)

# Configure OTLP Exporter (sending to Phoenix)
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")
exporter = OTLPSpanExporter(endpoint=endpoint)
provider.add_span_processor(BatchSpanProcessor(exporter))

# Instrument outgoing HTTPX calls (to NetBox API)
HTTPXClientInstrumentor().instrument()
# ---------------------------------------------

NETBOX_URL = os.getenv("NETBOX_URL", "http://netbox:8080")
NETBOX_API_TOKEN = os.getenv("NETBOX_API_TOKEN")
NETBOX_MCP_API_KEY = os.getenv("NETBOX_MCP_API_KEY")


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Validates X-MCP-API-Key header on every request."""
    async def dispatch(self, request: Request, call_next):
        if not NETBOX_MCP_API_KEY:
            return JSONResponse(
                status_code=500,
                content={"detail": "NETBOX_MCP_API_KEY not configured"}
            )
        key = request.headers.get("X-MCP-API-Key")
        if key != NETBOX_MCP_API_KEY:
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid or missing API key"}
            )
        return await call_next(request)


mcp = FastMCP("NetBoxManager", host="0.0.0.0")

HEADERS = {
    "Authorization": f"Token {NETBOX_API_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


@mcp.tool()
def lookup_device(name: str) -> str:
    """Look up a device in NetBox by name. Returns device info including primary IP."""
    with tracer.start_as_current_span("mcp.lookup_device") as span:
        span.set_attribute("mcp.device_name", name)
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{NETBOX_URL}/api/dcim/devices/",
                    params={"name": name},
                    headers=HEADERS,
                )
                resp.raise_for_status()
                data = resp.json()

                if data["count"] == 0:
                    return f"No device found with name '{name}'"

                device = data["results"][0]
                result = {
                    "id": device["id"],
                    "name": device["name"],
                    "status": device["status"]["value"] if device.get("status") else None,
                    "site": device["site"]["name"] if device.get("site") else None,
                    "role": device["role"]["name"] if device.get("role") else None,
                    "device_type": device["device_type"]["display"] if device.get("device_type") else None,
                    "primary_ip": None,
                }

                # Get primary IP
                if device.get("primary_ip"):
                    result["primary_ip"] = device["primary_ip"]["address"]
                elif device.get("primary_ip4"):
                    result["primary_ip"] = device["primary_ip4"]["address"]

                return str(result)
        except Exception as e:
            return f"Error looking up device: {e}"


@mcp.tool()
def list_devices() -> str:
    """List all devices registered in NetBox."""
    with tracer.start_as_current_span("mcp.list_devices") as span:
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{NETBOX_URL}/api/dcim/devices/",
                    headers=HEADERS,
                )
                resp.raise_for_status()
                data = resp.json()

                devices = []
                for d in data["results"]:
                    ip = None
                    if d.get("primary_ip"):
                        ip = d["primary_ip"]["address"]
                    elif d.get("primary_ip4"):
                        ip = d["primary_ip4"]["address"]
                    devices.append({
                        "name": d["name"],
                        "ip": ip,
                        "site": d["site"]["name"] if d.get("site") else None,
                        "role": d["role"]["name"] if d.get("role") else None,
                    })
                return str(devices) if devices else "No devices found in NetBox"
        except Exception as e:
            return f"Error listing devices: {e}"


# Expose the ASGI app for uvicorn with auth middleware
app = mcp.sse_app()
FastAPIInstrumentor.instrument_app(app)
app.add_middleware(APIKeyAuthMiddleware)

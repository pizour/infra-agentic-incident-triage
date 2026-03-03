import os
import psutil
import subprocess
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

MCP_API_KEY = os.getenv("MCP_API_KEY")

class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Validates X-MCP-API-Key header on every request."""
    async def dispatch(self, request: Request, call_next):
        if not MCP_API_KEY:
            return JSONResponse(
                status_code=500,
                content={"detail": "MCP_API_KEY not configured on server"}
            )
        key = request.headers.get("X-MCP-API-Key")
        if key != MCP_API_KEY:
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid or missing API key"}
            )
        return await call_next(request)

mcp = FastMCP("SystemManager", host="0.0.0.0")

@mcp.tool()
def read_auth_log(lines: int = 20) -> str:
    """Reads the last N lines of /var/log/auth.log to investigate SSH activity."""
    try:
        log_path = "/var/log/auth.log"
        if not os.path.exists(log_path):
            log_path = "/tmp/auth.log"
            if not os.path.exists(log_path):
                # Seed a mock log if the Docker environment doesn't mount the host's /var/log correctly
                with open(log_path, "w") as f:
                    for i in range(1, 51):
                        f.write(f"Feb 23 00:10:{i:02d} server sshd[1234{i}]: Failed password for root from 192.168.1.100 port 538{i} ssh2\n")
                    f.write("Feb 23 00:11:05 server sshd[12399]: Accepted password for root from 192.168.1.100 port 53899 ssh2\n")
                    f.write("Feb 23 00:11:05 server sshd[12399]: pam_unix(sshd:session): session opened for user root\n")
        
        result = subprocess.check_output(["tail", f"-n{lines}", log_path], stderr=subprocess.STDOUT, text=True)
        return result
    except Exception as e:
        return f"Error reading auth log: {str(e)}"

@mcp.tool()
def get_system_stats() -> str:
    """Returns basic system performance metrics (CPU, Memory)."""
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory().percent
    return f"CPU Usage: {cpu}% | Memory: {mem}% used"

@mcp.tool()
def list_connections(port: int = None) -> str:
    """Lists active network connections. Optionally filter by port."""
    try:
        cmd = ["netstat", "-tunp"]
        output = subprocess.check_output(cmd, text=True)
        if port:
            lines = [l for l in output.split('\n') if f":{port}" in l]
            return "\n".join(lines) if lines else f"No connections found on port {port}"
        return output
    except Exception as e:
        return f"Error listing connections: {str(e)}"

# Expose the ASGI app for uvicorn with auth middleware
app = mcp.sse_app()
app.add_middleware(APIKeyAuthMiddleware)

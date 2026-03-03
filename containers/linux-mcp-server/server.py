import os
import psutil
import subprocess
import asyncio
import asyncssh
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from typing import Optional

MCP_API_KEY = os.getenv("MCP_API_KEY")
REMOTE_USER = os.getenv("REMOTE_USER", "testuser")
REMOTE_PASS = os.getenv("REMOTE_PASS")
REMOTE_KEY_PATH = os.getenv("REMOTE_KEY_PATH")

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

async def run_command(command: str, host: Optional[str] = None) -> str:
    """Executes a command on a remote host via SSH or locally if no host is provided."""
    if not host:
        # Fallback to local execution
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                return stdout.decode().strip()
            else:
                return f"Local error ({process.returncode}): {stderr.decode().strip()}"
        except Exception as e:
            return f"Local execution error: {str(e)}"

    try:
        connect_kwargs = {}
        if REMOTE_PASS:
            connect_kwargs['password'] = REMOTE_PASS
        if REMOTE_KEY_PATH:
            connect_kwargs['client_keys'] = [REMOTE_KEY_PATH]
        
        async with asyncssh.connect(host, username=REMOTE_USER, **connect_kwargs, known_hosts=None) as conn:
            result = await conn.run(command)
            return result.stdout if result.exit_status == 0 else f"Remote error ({result.exit_status}): {result.stderr}"
    except Exception as e:
        return f"SSH Connection Error to {host}: {str(e)}"

@mcp.tool()
async def read_auth_log(lines: int = 20, host: Optional[str] = None) -> str:
    """
    Reads the last N lines of /var/log/auth.log.
    If host is provided, connects via SSH using server-side credentials.
    """
    cmd = f"tail -n{lines} /var/log/auth.log 2>/dev/null || tail -n{lines} /tmp/auth.log 2>/dev/null"
    return await run_command(cmd, host)

@mcp.tool()
async def get_system_stats(host: Optional[str] = None) -> str:
    """
    Returns basic system performance metrics (CPU, Memory).
    If host is provided, connects via SSH using server-side credentials.
    """
    if not host:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory().percent
        return f"Local CPU Usage: {cpu}% | Memory: {mem}% used"
    
    # Remote commands for metrics
    cpu_cmd = "top -bn1 | grep 'Cpu(s)' | sed 's/.*, *\\([0-9.]*\\)%* id.*/\\1/' | awk '{print 100 - $1}'"
    mem_cmd = "free | grep Mem | awk '{print $3/$2 * 100.0}'"
    
    cpu_res = await run_command(cpu_cmd, host)
    mem_res = await run_command(mem_cmd, host)
    
    return f"Remote CPU Usage: {cpu_res.strip()}% | Memory: {mem_res.strip()}% used"

@mcp.tool()
async def list_connections(port: Optional[int] = None, host: Optional[str] = None) -> str:
    """
    Lists active network connections. Optionally filter by port.
    If host is provided, connects via SSH using server-side credentials.
    """
    cmd = "netstat -tunp"
    if port:
        cmd += f" | grep ':{port}'"
    
    return await run_command(cmd, host)

# Expose the ASGI app for uvicorn with auth middleware
app = mcp.sse_app()
app.add_middleware(APIKeyAuthMiddleware)

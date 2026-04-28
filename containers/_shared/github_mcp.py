"""GitHub MCP client helpers shared by ai-agent and nexus-controller."""
import asyncio
import json
import time
from typing import Any, Dict, Optional

import httpx
import jwt
from loguru import logger


async def get_oauth_token(
    app_id: Optional[str],
    private_key: Optional[str],
    installation_id: Optional[str],
) -> Optional[str]:
    """Mint a GitHub App installation access token from OAuth credentials.

    Returns None if any credential is missing or the GitHub call fails.
    """
    if not all([app_id, private_key, installation_id]):
        return None

    try:
        now = int(time.time())
        payload = {"iss": app_id, "iat": now, "exp": now + 600}
        jwt_token = jwt.encode(payload, private_key, algorithm="RS256")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10.0,
            )
            if response.status_code == 201:
                return response.json().get("token")
    except Exception as e:
        logger.warning(f"Failed to get GitHub OAuth token: {e}")

    return None


async def call_mcp(
    url: str,
    tool_name: str,
    arguments: Dict[str, Any],
    gh_token: Optional[str] = None,
    max_retries: int = 3,
) -> str:
    """Call a GitHub MCP tool over JSON-RPC and parse the SSE response.

    On success returns the JSON-encoded `result` field for the caller to parse.
    On failure returns a human-readable error string (never raises).
    """
    headers = {"Content-Type": "application/json"}
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    for attempt in range(1, max_retries + 1):
        try:
            json_rpc_request = {
                "jsonrpc": "2.0",
                "id": f"call-{attempt}",
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=json_rpc_request, headers=headers, timeout=30.0)
                await asyncio.sleep(2.0)

                logger.debug(f"MCP response status: {response.status_code}")
                logger.debug(f"MCP response body: {response.text[:200]}")

                if response.status_code == 202:
                    if attempt < max_retries:
                        logger.info(f"MCP 202 (attempt {attempt}/{max_retries}). Retrying...")
                        await asyncio.sleep(1.0)
                        continue
                    return "MCP request accepted but no response received"

                if response.status_code == 200:
                    if not response.text:
                        if attempt < max_retries:
                            logger.warning(f"Empty MCP response (attempt {attempt}/{max_retries}). Retrying...")
                            await asyncio.sleep(0.5)
                            continue
                        return "Empty response from MCP server"

                    try:
                        json_data = None
                        for line in response.text.strip().splitlines():
                            if line.startswith("data: "):
                                json_data = json.loads(line[6:])
                                break

                        if not json_data:
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

                        if "result" in json_data:
                            return json.dumps(json_data["result"])
                        return f"Unexpected response format: {json_data}"

                    except Exception as json_err:
                        if attempt < max_retries:
                            logger.warning(f"Failed to parse MCP response: {json_err}. Retrying...")
                            await asyncio.sleep(0.5)
                            continue
                        return f"Failed to parse MCP response: {json_err}"

                if attempt < max_retries:
                    logger.warning(f"MCP HTTP {response.status_code} (attempt {attempt}/{max_retries}). Retrying...")
                    await asyncio.sleep(0.5)
                    continue
                return f"MCP HTTP Error {response.status_code}: {response.text}"

        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"MCP attempt {attempt}/{max_retries} exception: {e}. Retrying...")
                await asyncio.sleep(0.5)
                continue
            logger.error(f"MCP call failed after {max_retries} attempts: {e}")
            return f"Exception during MCP call (failed after {max_retries} attempts): {e}"

    return "Failed after retries"

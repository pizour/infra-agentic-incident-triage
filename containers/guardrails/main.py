import os
from fastapi import FastAPI, Request
from pydantic import BaseModel
from nemoguardrails import LLMRails, RailsConfig

app = FastAPI(title="NeMo Guardrails Service")

rails_config = RailsConfig.from_path("/app")
rails = LLMRails(rails_config)


class GuardrailsRequest(BaseModel):
    message: str


class GuardrailsResponse(BaseModel):
    content: str
    blocked: bool = False


@app.get("/")
async def health():
    return {"status": "ok", "service": "guardrails"}


@app.post("/check", response_model=GuardrailsResponse)
async def check(request: GuardrailsRequest):
    """Run a message through NeMo Guardrails and return the (possibly blocked) response."""
    result = await rails.generate_async(
        messages=[{"role": "user", "content": request.message}]
    )
    content = result.get("content", request.message) if isinstance(result, dict) else str(result)
    blocked = content != request.message
    return GuardrailsResponse(content=content, blocked=blocked)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

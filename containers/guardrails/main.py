import os
from fastapi import FastAPI, Request
from pydantic import BaseModel
from nemoguardrails import LLMRails, RailsConfig
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from openinference.instrumentation.langchain import LangChainInstrumentor
from openinference.instrumentation.openai import OpenAIInstrumentor

# Initialize TracerProvider
provider = TracerProvider()
trace.set_tracer_provider(provider)

# Configure OTLP Exporter (sending to Phoenix)
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")
exporter = OTLPSpanExporter(endpoint=endpoint)
provider.add_span_processor(BatchSpanProcessor(exporter))

LangChainInstrumentor().instrument()
OpenAIInstrumentor().instrument()

app = FastAPI(title="NeMo Guardrails Service")
FastAPIInstrumentor.instrument_app(app)

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
    
    # Check if the output matches any predefined refusal patterns in our colang
    refusals = [
        "I'm sorry, I cannot fulfill this request as it violates security policies.",
        "I am a security-focused AI assistant. I can only help with security alerts and analysis.",
        "Yes, I'm operational. I'm a security-focused AI assistant ready to investigate alerts."
    ]
    
    if any(refusal in content for refusal in refusals):
        return GuardrailsResponse(content=content, blocked=True)

    # Return original message, bypassing LLM conversational rewrites on accepted input
    return GuardrailsResponse(content=request.message, blocked=False)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

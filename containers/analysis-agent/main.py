import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

# --- OpenTelemetry Setup ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator

resource = Resource.create({SERVICE_NAME: "analysis-agent"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

provider.add_span_processor(OpenInferenceSpanProcessor())
HTTPXClientInstrumentor().instrument()
# ---------------------------

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# PydanticAI imports
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

# --- Shared config ---
# _MODEL_NAME = os.getenv("LLM_MODEL", "llama3.1:8b")
# _OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")
#
# def make_model() -> OpenAIChatModel:
#     return OpenAIChatModel(
#         _MODEL_NAME,
#         provider=OpenAIProvider(base_url=_OLLAMA_BASE_URL, api_key="ollama"),
#     )

from pydantic_ai.models.google import GoogleModel
def make_model() -> GoogleModel:
    return GoogleModel(os.getenv("LLM_MODEL", "gemini-2.5-flash"), provider="google-vertex")


@dataclass
class AnalysisDeps:
    host: str
    host_ip: str
    source_ip: str
    alert_description: str
    evidence: str


# --- Analysis Agent Definition ---
analysis_agent: Agent[AnalysisDeps, str] = Agent(
    make_model(),
    instrument=True,
    deps_type=AnalysisDeps,
    result_type=str,
    system_prompt=(
        "You are an expert security analyst. "
        "Review the alert details, the target host, the attacker IP, "
        "and all EVIDENCE gathered from the system. "
        "Provide a concise technical summary of what actually happened. "
        "End your response with EXACTLY one of these verdicts on a new line: "
        "'VERDICT: THREAT' (if true malicious activity is confirmed) "
        "or 'VERDICT: BENIGN' (if it's a false positive or harmless noise)."
    ),
)


@analysis_agent.system_prompt
def inject_evidence(ctx: RunContext[AnalysisDeps]) -> str:
    """Dynamically inject the gathered evidence into the LLM context."""
    return (
        f"--- CONTEXT ---\n"
        f"Alert: {ctx.deps.alert_description}\n"
        f"Target Host: {ctx.deps.host} ({ctx.deps.host_ip})\n"
        f"Source/Attacker: {ctx.deps.source_ip}\n\n"
        f"--- EVIDENCE GATHERED BY INVESTIGATOR ---\n"
        f"{ctx.deps.evidence}\n"
        f"-----------------------------------------\n"
    )


# --- FastAPI Implementation ---
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Analysis Agent API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)


class RunRequest(BaseModel):
    host: str
    host_ip: str
    source_ip: str
    alert_description: str
    evidence: str


class RunResponse(BaseModel):
    analysis: str


@app.get("/")
def health_check():
    return {"status": "ok", "service": "analysis-agent"}


@app.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest):
    """
    Kicks off the PydanticAI analysis agent to evaluate the evidence.
    """
    with tracer.start_as_current_span("analysis_agent.run") as span:
        span.set_attribute("request.host", req.host)
        span.set_attribute("request.host_ip", req.host_ip)
        span.set_attribute("request.source_ip", req.source_ip)
        span.set_attribute("evidence_length", len(req.evidence))

        deps = AnalysisDeps(
            host=req.host,
            host_ip=req.host_ip,
            source_ip=req.source_ip,
            alert_description=req.alert_description,
            evidence=req.evidence,
        )

        try:
            result = await analysis_agent.run(
                "Analyse the security evidence and produce your verdict.", deps=deps
            )
            analysis = str(result.output)
            span.set_attribute("analysis", analysis[:500])
            return RunResponse(analysis=analysis)
        except Exception as e:
            span.record_exception(e)
            raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)

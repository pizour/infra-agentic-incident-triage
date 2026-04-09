import os
import httpx
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

resource = Resource.create({SERVICE_NAME: "ticket-agent"})
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

# Zammad Config
ZAMMAD_URL = os.getenv("ZAMMAD_URL", "http://zammad-railsserver:8080")
ZAMMAD_USER = os.getenv("ZAMMAD_USER", "")
ZAMMAD_PASS = os.getenv("ZAMMAD_PASS", "")
ZAMMAD_TOKEN = os.getenv("ZAMMAD_TOKEN", "")
ZAMMAD_CUSTOMER_ID = int(os.getenv("ZAMMAD_CUSTOMER_ID", "1"))

# def make_model() -> OpenAIChatModel:
#     return OpenAIChatModel(
#         _MODEL_NAME,
#         provider=OpenAIProvider(base_url=_OLLAMA_BASE_URL, api_key="ollama"),
#     )

from pydantic_ai.models.google import GoogleModel

def make_model() -> GoogleModel:
    return GoogleModel(os.getenv("LLM_MODEL", "gemini-2.5-flash"), provider="google-vertex")


@dataclass
class TicketDeps:
    host: str
    host_ip: str
    source_ip: str
    alert_description: str
    analysis: str


# --- Ticket Agent Definition ---
ticket_agent: Agent[TicketDeps, str] = Agent(
    make_model(),
    instrument=True,
    deps_type=TicketDeps,
    result_type=str,
    system_prompt=(
        "You are a ticketing service. You MUST call the create_zammad_ticket "
        "tool with the provided alert context and return a confirmation string. "
        "Use the analysis provided to generate a helpful summary."
    ),
)


@ticket_agent.tool
async def create_zammad_ticket(
    ctx: RunContext[TicketDeps],
    title: str,
    article_body: str,
    group: str = "Users",
    priority: str = "3 normal",
    state: str = "new",
) -> str:
    """Creates a ticket in Zammad."""
    with tracer.start_as_current_span("ticket_agent.create_zammad_ticket") as span:
        span.set_attribute("service.name", "ticket-agent")
        span.set_attribute("tool.name", "create_zammad_ticket")
        span.set_attribute("ticket.title", title)
        span.set_attribute("ticket.group", group)

        headers = {"Content-Type": "application/json"}
        auth_kwarg = {}
        if ZAMMAD_TOKEN:
            headers["Authorization"] = f"Token token={ZAMMAD_TOKEN}"
        elif ZAMMAD_USER and ZAMMAD_PASS:
            auth_kwarg["auth"] = (ZAMMAD_USER, ZAMMAD_PASS)
        else:
            span.record_exception(Exception("No Zammad credentials"))
            return "Failed to create ticket: No Zammad credentials configured."

        payload = {
            "title": title,
            "group": group,
            "priority": priority,
            "state": state,
            "customer_id": ZAMMAD_CUSTOMER_ID,
            "article": {
                "subject": title,
                "body": article_body,
                "type": "note",
                "internal": False,
            },
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{ZAMMAD_URL}/api/v1/tickets",
                    headers=headers,
                    json=payload,
                    timeout=10.0,
                    **auth_kwarg,
                )
                response.raise_for_status()
                data = response.json()
                ticket_id = data.get("id")
                status_str = f"Ticket #{ticket_id} created successfully."
                span.set_attribute("tool.status", "success")
                span.set_attribute("ticket.id", ticket_id)
                return status_str
            except Exception as e:
                err_msg = f"Failed to create Zammad ticket: {str(e)}"
                span.record_exception(e)
                span.set_attribute("tool.status", "error")
                return err_msg


# --- FastAPI Implementation ---
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Ticket Agent API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)


class RunRequest(BaseModel):
    host: str
    host_ip: str
    source_ip: str
    alert_description: str
    analysis: str


class RunResponse(BaseModel):
    ticket_result: str


@app.get("/")
def health_check():
    return {"status": "ok", "service": "ticket-agent"}


@app.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest):
    """
    Kicks off the PydanticAI ticket agent to create a Zammad ticket.
    """
    with tracer.start_as_current_span("ticket_agent.run") as span:
        span.set_attribute("request.host", req.host)
        span.set_attribute("request.host_ip", req.host_ip)
        span.set_attribute("request.source_ip", req.source_ip)

        deps = TicketDeps(
            host=req.host,
            host_ip=req.host_ip,
            source_ip=req.source_ip,
            alert_description=req.alert_description,
            analysis=req.analysis,
        )
        prompt = (
            f"Create a Zammad ticket for this security incident on host {req.host} "
            f"({req.host_ip}). Attacker IP: {req.source_ip}. "
            f"Alert: {req.alert_description}\n\n"
            f"Analysis Summary:\n{req.analysis}"
        )

        try:
            result = await ticket_agent.run(prompt, deps=deps)
            ticket_result = str(result.output)
            span.set_attribute("ticket_result", ticket_result)
            return RunResponse(ticket_result=ticket_result)
        except Exception as e:
            span.record_exception(e)
            raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8006)

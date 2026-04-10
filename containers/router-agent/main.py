import os
import glob
import httpx
import json
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import frontmatter
from loguru import logger

from langchain_google_vertexai import ChatVertexAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

load_dotenv()

# --- OpenTelemetry / Arize Phoenix Setup ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from openinference.instrumentation.langchain import LangChainInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator

# Initialize TracerProvider with Service Name
resource = Resource.create({SERVICE_NAME: "router-agent"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__)

# Configure OTLP Exporter
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")
exporter = OTLPSpanExporter(endpoint=endpoint)
provider.add_span_processor(BatchSpanProcessor(exporter))

HTTPXClientInstrumentor().instrument()
LangChainInstrumentor().instrument()
# ---------------------------------------------

app = FastAPI(title="Router-Agent API")

FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)

# --- Configuration ---
SKILLS_DIR = os.getenv("SKILLS_DIR", "/app/skills")
model = ChatVertexAI(model_name="gemini-2.0-flash", temperature=0)

class AgentStep(BaseModel):
    agent_id: str
    skills: List[str]
    env_vars: dict = {}
    output_key: str = "evidence"
    reasoning: str

class RouterResponse(BaseModel):
    parsed_intent: str
    plan: List[AgentStep]
    reasoning: str

parser = JsonOutputParser(pydantic_object=RouterResponse)

# --- Router Logic ---

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are the 'Router-Agent'. Your job is to examine an incoming request and design a multi-agent execution plan (a chain).\n"
               "Follow these internal routing instructions precisely:\n"
               "{router_instructions}\n"
               "\n"
               "Available Target Agents (metadata includes default env vars and output keys):\n"
               "{available_agents}\n"
               "\n"
               "Available skills (SOPs) for those agents:\n"
               "{available_skills}\n"
               "\n"
               "Your response MUST be a valid JSON object with a 'plan' key. Each step in the plan must specify:\n"
               "- 'agent_id': The routing_key of the target agent.\n"
               "- 'skills': The list of skills they should use.\n"
               "- 'env_vars': All required environment variables (drawn from the agent metadata).\n"
               "- 'output_key': Where to store the result in the shared state (drawn from the agent metadata, e.g., 'evidence', 'analysis', 'ticket').\n"
               "- 'reasoning': Why this step is necessary.\n"
               "\n"
               "Example format:\n"
               "{{ \"parsed_intent\": \"...\", \"plan\": [{{ \"agent_id\": \"vm_tshooter\", \"skills\": [\"linux_operations/SKILL.md\"], \"env_vars\": {{ \"SYSTEM_PROMPT\": \"...\" }}, \"output_key\": \"evidence\", \"reasoning\": \"...\" }}], \"reasoning\": \"...\" }}"),
    ("human", "Request: {input}\nContext: {context}"),
])


def get_router_instructions() -> str:
    path = os.path.join(SKILLS_DIR, "agent_router/SKILL.md")
    if os.path.exists(path):
        with open(path, 'r') as f:
            return f.read()
    return "Follow standard routing procedures."

def get_available_agents() -> str:
    agent_files = glob.glob(os.path.join(SKILLS_DIR, "agents/*.md"))
    agents = []
    for f in agent_files:
        post = frontmatter.load(f)
        agents.append(json.dumps(post.metadata))
    return "\n---\n".join(agents)

def get_available_skills() -> str:
    skill_files = glob.glob(os.path.join(SKILLS_DIR, "**/SKILL.md"), recursive=True)
    skills = []
    for f in skill_files:
        rel_path = os.path.relpath(f, SKILLS_DIR)
        skills.append(rel_path)
    return "\n".join(skills)

class RunRequest(BaseModel):
    input: str
    context: Optional[dict] = None

@app.post("/run")
async def run_router(request: RunRequest):
    logger.info(f"ROUTER REQUEST: input='{request.input[:100]}...'")
    try:
        skills_list = get_available_skills()
        instructions = get_router_instructions()
        agents_list = get_available_agents()
        chain = prompt | model | parser
        
        result = await chain.ainvoke({
            "input": request.input,
            "context": json.dumps(request.context or {}),
            "available_skills": skills_list,
            "router_instructions": instructions,
            "available_agents": agents_list
        })
        
        logger.info(f"PLAN GENERATED: {len(result.get('plan', []))} steps")
        for i, step in enumerate(result.get('plan', [])):
            logger.info(f"  Step {i+1}: {step['agent_id']} ({step['reasoning']})")
            
        return result
    except Exception as e:
        logger.error(f"Router error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)

"""
AI Interview System — FastAPI Backend
Serves:
  • REST function endpoints called by Elastic agents
  • WebSocket endpoint for the candidate chat UI
  • Static files (index.html)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import httpx
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, NotFoundError
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(
    title="AI Interview System",
    description="Backend functions + WebSocket relay for the Elastic Agent-powered interview pipeline.",
    version="1.0.0",
)

# Allow the Node.js frontend (port 3000) + any localhost origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the static folder so index.html is served at /
STATIC_DIR   = Path(__file__).parent / "static"
FRONTEND_DIR = Path(__file__).parent / "frontend" / "public"
RECORDINGS_DIR = Path(__file__).parent / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)
CV_DIR = Path(__file__).parent / "cv"
CV_DIR.mkdir(exist_ok=True)

app.mount("/static",   StaticFiles(directory=str(STATIC_DIR)),   name="static")
app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")

# ──────────────────────────────────────────────────────────────────────────────
# Singletons (initialised once at startup)
# ──────────────────────────────────────────────────────────────────────────────
ES_URL          = os.getenv("ELASTICSEARCH_URL")
ES_USER         = os.getenv("ELASTICSEARCH_USER", "elastic")
ES_PASS         = os.getenv("ELASTICSEARCH_PASSWORD")

ELASTIC_AGENT_URL   = os.getenv("ELASTIC_AGENT_URL")
ELASTIC_API_KEY     = os.getenv("ELASTIC_API_KEY", "")
KIBANA_API_KEY      = os.getenv("KIBANA_API_KEY", "")
KIBANA_USER         = os.getenv("ELASTICSEARCH_USER", "elastic")
KIBANA_PASS         = os.getenv("ELASTICSEARCH_PASSWORD", "")

# Agent IDs — set by setup_agents.py and stored in .env
SUPERVISOR_AGENT_ID  = os.getenv("SUPERVISOR_AGENT_ID",  "supervisor-agent")
ANALYSIS_AGENT_ID    = os.getenv("ANALYSIS_AGENT_ID",    "analysis-agent")
EVALUATION_AGENT_ID  = os.getenv("EVALUATION_AGENT_ID",  "evaluation-agent")
BENCHMARK_AGENT_ID   = os.getenv("BENCHMARK_AGENT_ID",   "benchmark-agent")

# In-memory conversation history keyed by session_id
_session_history: dict[str, list[dict]] = {}

if not ES_URL:
    raise RuntimeError("ELASTICSEARCH_URL env var is required")
if not ELASTIC_AGENT_URL:
    raise RuntimeError("ELASTIC_AGENT_URL env var is required")

es = Elasticsearch(ES_URL, basic_auth=(ES_USER, ES_PASS))
embedding_model = SentenceTransformer("all-MiniLM-L6-v2")


# ──────────────────────────────────────────────────────────────────────────────
# Agent helpers
# ──────────────────────────────────────────────────────────────────────────────

def _build_agent_auth() -> tuple:
    """Return (headers, auth) for Kibana Agent Builder API calls."""
    hdrs = {"kbn-xsrf": "true", "Content-Type": "application/json"}
    return hdrs, (KIBANA_USER, KIBANA_PASS)


async def _call_agent(agent_id: str, prompt: str, timeout: float = 90.0) -> str:
    """
    Call any Elastic Agent Builder agent via the /converse endpoint.
    Returns the raw text of the agent’s response.
    Raises httpx.HTTPError on non-2xx so the caller can handle it.
    """
    hdrs, auth = _build_agent_auth()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{ELASTIC_AGENT_URL}/api/agent_builder/converse",
            headers=hdrs,
            auth=auth,
            json={"agent_id": agent_id, "input": prompt},
        )
        resp.raise_for_status()
        data = resp.json()
        nested = data.get("response", {})
        return nested.get("message", "") if isinstance(nested, dict) else str(nested)


def _extract_json(text: str):
    """
    Parse the first JSON object or array found in agent output text.
    Returns the parsed object/list, or None if nothing valid is found.
    """
    stripped = text.strip()
    # Direct parse (agent returned pure JSON)
    if stripped and stripped[0] in ("{"  , "["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # Strip markdown code fences
    fenced = re.sub(r"```(?:json)?\s*", "", stripped, flags=re.IGNORECASE).strip()
    if fenced and fenced[0] in ("{", "["):
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            pass
    # Last-resort: find the largest {...} or [...] block
    for pattern in (r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", r"\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]"):
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health_check():
    """Quick liveness probe."""
    try:
        info = es.cluster.health()
        return {"status": "ok", "elasticsearch": info["status"]}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Elasticsearch unavailable: {exc}")


@app.get("/")
def root():
    """Serve the portal picker (choose candidate or recruiter)."""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/candidate")
@app.get("/candidate.html")
def candidate_portal():
    """Serve the candidate webcam interview portal."""
    return FileResponse(str(FRONTEND_DIR / "candidate.html"))


@app.get("/recruiter.html")
def recruiter_portal_new():
    """Serve the recruiter dashboard (new frontend)."""
    return FileResponse(str(FRONTEND_DIR / "recruiter.html"))


@app.get("/results")
@app.get("/results.html")
def results_page():
    """Serve the interview results page."""
    return FileResponse(str(FRONTEND_DIR / "results.html"))


@app.get("/livecoding")
@app.get("/livecoding.html")
def livecoding_page():
    """Serve the live coding candidate portal."""
    return FileResponse(str(FRONTEND_DIR / "livecoding.html"))


@app.get("/recruiter")
def recruiter_portal():
    """Serve the recruiter portal (legacy path)."""
    return FileResponse(str(FRONTEND_DIR / "recruiter.html"))


@app.get("/interviewer")
def interviewer_portal():
    """Serve the live interview monitor portal."""
    return FileResponse(str(STATIC_DIR / "interviewer.html"))


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────────────────────────────────────
class TranscriptEntry(BaseModel):
    session_id: str
    role: str        # "assistant" | "candidate"
    content: str


class Skill(BaseModel):
    skill_name: str
    proficiency: str  # beginner | intermediate | advanced | expert
    evidence: str
    confidence: float


class SkillsPayload(BaseModel):
    session_id: str
    candidate_id: str
    skills: List[Skill]


class Evaluation(BaseModel):
    session_id: str
    candidate_id: str
    technical_score: float
    communication_score: float
    problem_solving_score: float
    cultural_fit_score: float
    total_score: float
    recommendation: str
    bias_risk: Optional[str] = "LOW"


class BenchmarkRequest(BaseModel):
    skills: List[str]
    job_id: str


class StageUpdate(BaseModel):
    session_id: str
    stage: str


class BiasFlag(BaseModel):
    session_id: str
    risk_level: str   # LOW | MEDIUM | HIGH
    concerns: str
    recommendations: Optional[str] = ""


class CandidateProfile(BaseModel):
    candidate_id: str
    name: str
    email: str
    job_id: str


class JobRequirement(BaseModel):
    job_id: str
    title: str
    required_skills: List[str]
    description: str
    department: Optional[str] = ""
    location: Optional[str] = ""
    active: Optional[bool] = True


class InterviewerNote(BaseModel):
    session_id: str
    note: str
    action: Optional[str] = "note"   # note | pause | end | escalate


class RoomCreate(BaseModel):
    title: str
    description: str
    evaluation_criteria: Optional[str] = ""
    department: Optional[str] = ""
    location: Optional[str] = ""
    interviewer_id: Optional[str] = "default_interviewer"  # Track who created the room


class SkillExtractionRequest(BaseModel):
    job_title: str
    job_description: str


class CandidateRegister(BaseModel):
    room_code: str
    name: str
    email: str
    resume_text: Optional[str] = ""


# ──────────────────────────────────────────────────────────────────────────────
# Function endpoints — called by Elastic Agents
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# AI Skill Extraction — auto-extract skills from job descriptions 
# ──────────────────────────────────────────────────────────────────────────────

_SKILL_KEYWORDS: list[tuple[str, str]] = [
    # Programming languages
    (r"\bpython\b", "Python"), (r"\bjava\b(?!script)", "Java"), (r"\bjavascript\b|\bjs\b", "JavaScript"),
    (r"\btypescript\b|\bts\b", "TypeScript"), (r"\bc\+\+\b|\bcpp\b", "C++"), (r"\bc#\b|\bcsharp\b", "C#"),
    (r"\bgo\b(?:lang)?\b", "Go"), (r"\brust\b", "Rust"), (r"\bscala\b", "Scala"),
    (r"\br\b(?:\s+programming)?", "R"), (r"\bkotlin\b", "Kotlin"), (r"\bswift\b", "Swift"),
    (r"\bphp\b", "PHP"), (r"\bruby\b", "Ruby"), (r"\bperl\b", "Perl"), (r"\bshell\b|\bbash\b", "Shell/Bash"),
    # ML / AI
    (r"\bmachine\s+learning\b|\bml\b", "Machine Learning"), (r"\bdeep\s+learning\b", "Deep Learning"),
    (r"\bneural\s+network[s]?\b", "Neural Networks"), (r"\bnatural\s+language\s+processing\b|\bnlp\b", "NLP"),
    (r"\bcomputer\s+vision\b", "Computer Vision"), (r"\breinforcement\s+learning\b", "Reinforcement Learning"),
    (r"\bgenerative\s+ai\b|\bgen\s*ai\b", "Generative AI"), (r"\bllm[s]?\b", "LLMs"),
    (r"\btransformer[s]?\b", "Transformers"), (r"\bfine.?tun", "Fine-tuning"),
    (r"\bmodel\s+evaluation\b|\bmodel\s+assessment\b", "Model Evaluation"),
    (r"\bfailure\s+analysis\b", "Failure Analysis"), (r"\bdata\s+quality\b", "Data Quality"),
    (r"\bfeature\s+engineering\b", "Feature Engineering"), (r"\bmodel\s+deployment\b", "Model Deployment"),
    (r"\bmlops\b", "MLOps"), (r"\bexperiment\s+tracking\b", "Experiment Tracking"),
    (r"\banomaly\s+detection\b", "Anomaly Detection"),
    # ML Frameworks
    (r"\btensorflow\b|\btf\b", "TensorFlow"), (r"\bpytorch\b", "PyTorch"), (r"\bkeras\b", "Keras"),
    (r"\bscikit.?learn\b|\bsklearn\b", "scikit-learn"), (r"\bhugging\s*face\b", "HuggingFace"),
    (r"\bxgboost\b", "XGBoost"), (r"\blightgbm\b", "LightGBM"), (r"\bspark\s*ml\b|\bpyspark\b", "PySpark"),
    (r"\bmlflow\b", "MLflow"), (r"\bwandb\b|\bweights\s*&\s*biases\b", "Weights & Biases"),
    (r"\bray\b", "Ray"), (r"\bonnx\b", "ONNX"),
    # Data
    (r"\bsql\b", "SQL"), (r"\bnosql\b", "NoSQL"), (r"\bpandas\b", "Pandas"), (r"\bnumpy\b", "NumPy"),
    (r"\bspark\b", "Apache Spark"), (r"\bhadoop\b", "Hadoop"), (r"\bkafka\b", "Kafka"),
    (r"\bairflow\b", "Airflow"), (r"\bdbt\b", "dbt"), (r"\bdata\s+pipeline[s]?\b", "Data Pipelines"),
    (r"\bdata\s+warehouse\b", "Data Warehouse"), (r"\betl\b|\belt\b", "ETL/ELT"),
    (r"\bdata\s+engineering\b", "Data Engineering"),
    # Cloud & Infra
    (r"\baws\b|amazon\s+web\s+services", "AWS"), (r"\bazure\b", "Azure"), (r"\bgcp\b|google\s+cloud", "GCP"),
    (r"\bkubernetes\b|\bk8s\b", "Kubernetes"), (r"\bdocker\b", "Docker"), (r"\bterraform\b", "Terraform"),
    (r"\bci/cd\b|\bcicd\b", "CI/CD"), (r"\bgitops\b", "GitOps"),
    # Web / Backend
    (r"\bfastapi\b", "FastAPI"), (r"\bdjango\b", "Django"), (r"\bflask\b", "Flask"),
    (r"\bnode\.?js\b", "Node.js"), (r"\breact\b", "React"), (r"\bvue\b", "Vue"),
    (r"\bangular\b", "Angular"), (r"\brest\s*api[s]?\b|\brestful\b", "REST APIs"),
    (r"\bgraphql\b", "GraphQL"), (r"\bmicroservice[s]?\b", "Microservices"),
    # Databases
    (r"\bpostgresql\b|\bpostgres\b", "PostgreSQL"), (r"\bmysql\b", "MySQL"),
    (r"\bmongodb\b", "MongoDB"), (r"\bredis\b", "Redis"), (r"\belasticsearch\b", "Elasticsearch"),
    (r"\bsnowflake\b", "Snowflake"), (r"\bbigquery\b", "BigQuery"),
    # Soft skills
    (r"\bteam\s*work\b|\bcollaboration\b", "Collaboration"), (r"\bcommunication\b", "Communication"),
    (r"\bproblem.?solv", "Problem Solving"), (r"\bcritical\s+thinking\b", "Critical Thinking"),
    (r"\bproject\s+management\b", "Project Management"), (r"\bmentoring\b|\bmentorship\b", "Mentoring"),
    (r"\bstakeholder\b", "Stakeholder Management"), (r"\banalytical\b", "Analytical Skills"),
    (r"\bdata.?driven\b", "Data-Driven Decision Making"),
]

def _local_extract_skills(title: str, description: str) -> list[str]:
    """Keyword-based local fallback when the Elastic agent is unavailable."""
    text = f"{title} {description}".lower()
    found = []
    seen = set()
    for pattern, label in _SKILL_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE) and label not in seen:
            found.append(label)
            seen.add(label)
    return found[:15] if found else ["Python", "Problem Solving", "Communication"]


# ── Fast local interviewer (used when Elastic Agent is unavailable/slow) ──────
_TECHNICAL_QS = [
    "Walk me through a technically challenging project you built end-to-end. What was the hardest part?",
    "Tell me about a time you had to optimise something for performance. What did you measure and how?",
    "How do you decide between building something yourself versus using an existing library?",
    "Describe your debugging process when you hit a problem you've never seen before.",
    "What's a technology decision you've made that you'd change with hindsight, and why?",
    "How do you keep your systems observable in production? What do you monitor?",
    "Talk me through how you'd design a system to handle 10× your current traffic.",
]
_BEHAVIOURAL_QS = [
    "Tell me about a time you pushed back on a requirement. How did that conversation go?",
    "Describe a situation where you had to deliver bad news to a stakeholder. What happened?",
    "Tell me about a project that failed or fell short. What did you learn?",
    "Give me an example of when you had to prioritise ruthlessly. What did you cut and why?",
    "Tell me about a time you had a strong disagreement with a colleague. How was it resolved?",
    "Describe a time you had to learn something quickly under pressure. How did you approach it?",
]
_SITUATIONAL_QS = [
    "Imagine it's Friday 5 pm and a critical bug hits production. Your team lead is unreachable. What do you do?",
    "You inherit a codebase with no tests and no documentation. Where do you start?",
    "Your manager asks you to ship a feature you believe has a serious security risk. How do you handle it?",
    "A junior engineer on your team is consistently missing deadlines but doesn't raise blockers. What's your move?",
    "Halfway through a project the requirements change significantly. Walk me through how you'd adapt.",
]
_CLOSING_QS = [
    "What aspect of this role excites you most, and what worries you slightly?",
    "Where do you see yourself in two years, and how does this role fit that path?",
    "What's the most important thing you'd want to achieve in your first 90 days here?",
]
_AFFIRMATIONS = ["Got it.", "Interesting.", "Makes sense.", "Noted.", "That's helpful context."]

# Count of non-greeting turns to determine stage
_STAGE_THRESHOLDS = {"GREETING": 0, "TECHNICAL": 1, "BEHAVIOURAL": 4, "SITUATIONAL": 7, "CLOSING": 10, "COMPLETE": 13}

def _local_interview_reply(history: list, _job_id: str = "") -> dict:
    """
    Generate an instant interview response without the Elastic Agent.
    Uses turn count to progress through stages, alternating question types.
    """
    import random as _rnd
    # Count assistant + candidate turns (exclude system)
    turns = sum(1 for m in history if m["role"] in ("candidate", "assistant"))

    # Determine stage by turn count
    if turns < 2:
        stage = "GREETING"
    elif turns < 5:
        stage = "TECHNICAL"
    elif turns < 8:
        stage = "BEHAVIOURAL"
    elif turns < 11:
        stage = "SITUATIONAL"
    elif turns < 13:
        stage = "CLOSING"
    else:
        stage = "COMPLETE"

    if stage == "COMPLETE":
        return {
            "role": "assistant",
            "message": "Thank you so much for your time today. It was great learning about your experience — we'll review everything and be in touch with next steps soon.",
            "stage": "COMPLETE",
        }

    # Pick affirmation based on last candidate message
    affirmation = _rnd.choice(_AFFIRMATIONS)

    # Pick question pool by stage
    if stage == "TECHNICAL":
        pool = _TECHNICAL_QS
    elif stage == "BEHAVIOURAL":
        pool = _BEHAVIOURAL_QS
    elif stage == "SITUATIONAL":
        pool = _SITUATIONAL_QS
    else:  # CLOSING
        pool = _CLOSING_QS

    # Avoid repeating questions
    asked = {m["content"] for m in history if m["role"] == "assistant"}
    available = [q for q in pool if q not in asked]
    question = _rnd.choice(available) if available else _rnd.choice(pool)

    message = f"{affirmation} {question}"
    return {"role": "assistant", "message": message, "stage": stage}


@app.post("/extract-skills")
async def extract_skills(request: SkillExtractionRequest):
    """
    Agent-driven skill extraction with local NLP fallback.
    Tries the Supervisor agent first; on failure falls back to keyword extraction.
    """
    prompt = (
        f"Extract the required skills for this job role and return them as a JSON array of strings.\n\n"
        f"Job Title: {request.job_title}\n"
        f"Job Description:\n{request.job_description}\n\n"
        f"Think carefully about:\n"
        f"  - Technical skills explicitly named in the description\n"
        f"  - Tools, frameworks, and platforms implied by the role\n"
        f"  - Soft skills and competencies required at this seniority level\n"
        f"  - Normalise skill names (e.g. Postgres→PostgreSQL, k8s→Kubernetes)\n\n"
        f"Return ONLY a valid JSON array. No explanation, no markdown, no other text:\n"
        f'["Skill1", "Skill2", ...]'
    )
    try:
        raw = await _call_agent(SUPERVISOR_AGENT_ID, prompt, timeout=30.0)
        log.info("Skill extraction agent response (first 200): %s", raw[:200])
        parsed = _extract_json(raw)
        if isinstance(parsed, list) and parsed:
            skills = [s for s in parsed if isinstance(s, str) and s.strip()]
            if skills:
                return {"skills": skills[:15]}
        raise ValueError(f"Unexpected agent output: {raw[:200]}")
    except Exception as exc:
        log.warning("Agent skill extraction failed (%s); using local keyword fallback.", exc)
        skills = _local_extract_skills(request.job_title, request.job_description)
        return {"skills": skills}
# ──────────────────────────────────────────────────────────────────────────────
# Room Management — code-based interview rooms
# ──────────────────────────────────────────────────────────────────────────────

def _gen_room_code() -> str:
    """Generate a short memorable room code like INT-A3K7."""
    chars = random.choices(string.ascii_uppercase + string.digits, k=4)
    return "INT-" + "".join(chars)


@app.post("/rooms", status_code=201)
async def create_room(body: RoomCreate):
    """Recruiter creates an interview room; returns the room code."""
    # Collision-safe code generation (try up to 5 times)
    for _ in range(5):
        code = _gen_room_code()
        existing = es.search(
            index="job_requirements_index",
            size=1,
            query={"term": {"room_code": code}},
        )
        if existing["hits"]["total"]["value"] == 0:
            break

    job_id = f"job_{uuid.uuid4().hex[:8]}"

    # Skill extraction — try agent first, fall back to local keyword extraction
    prompt = (
        f"Extract the required skills for this job role and return them as a JSON array of strings.\n\n"
        f"Job Title: {body.title}\n"
        f"Job Description:\n{body.description}\n\n"
        f"Think carefully about:\n"
        f"  - Technical skills explicitly named in the description\n"
        f"  - Tools, frameworks, and platforms implied by the role\n"
        f"  - Soft skills and competencies required at this seniority level\n"
        f"  - Normalise names (e.g. Postgres→PostgreSQL, k8s→Kubernetes)\n\n"
        f"Return ONLY a valid JSON array. No explanation, no markdown, no other text:\n"
        f'["Skill1", "Skill2", ...]'
    )
    required_skills: list[str] = []
    try:
        raw = await _call_agent(SUPERVISOR_AGENT_ID, prompt, timeout=30.0)
        parsed = _extract_json(raw)
        if isinstance(parsed, list):
            required_skills = [s for s in parsed if isinstance(s, str) and s.strip()][:12]
        log.info("Agent extracted %d skills for room %s", len(required_skills), code)
    except Exception as exc:
        log.warning("Skill extraction agent unavailable for room creation (%s); using local fallback.", exc)

    if not required_skills:
        required_skills = _local_extract_skills(body.title, body.description)
    
    doc = {
        "job_id":              job_id,
        "title":               body.title,
        "description":         body.description,
        "required_skills":     required_skills,  # auto-extracted
        "evaluation_criteria": body.evaluation_criteria,
        "department":          body.department,
        "location":            body.location,
        "interviewer_id":      body.interviewer_id,
        "active":              True,
        "room_code":           code,
        "created_at":          datetime.now(timezone.utc).isoformat(),
        "updated_at":          datetime.now(timezone.utc).isoformat(),
    }
    doc["description_embedding"] = embedding_model.encode(
        f"{body.title} {body.description} {' '.join(required_skills)}"
    ).tolist()
    es.index(index="job_requirements_index", id=job_id, document=doc, refresh=True)
    return {"room_code": code, "job_id": job_id, "extracted_skills": required_skills}


@app.get("/rooms/{room_code}")
def get_room(room_code: str):
    """Look up a job by room code (used by candidate portal on code entry)."""
    result = es.search(
        index="job_requirements_index",
        size=1,
        query={"match": {"room_code": room_code.upper()}},
    )
    hits = result["hits"]["hits"]
    if not hits:
        raise HTTPException(status_code=404, detail="Room code not found")
    src = hits[0]["_source"]
    # Return only safe fields (no embeddings)
    return {
        k: v for k, v in src.items()
        if k not in ("description_embedding",)
    }


@app.get("/rooms/{room_code}/candidates")
def get_room_candidates(room_code: str):
    """Recruiter views all candidates + evaluations for a room."""
    # 1. Get job_id from room code
    jobs = es.search(
        index="job_requirements_index",
        size=1,
        query={"term": {"room_code": room_code.upper()}},
    )
    if jobs["hits"]["total"]["value"] == 0:
        raise HTTPException(status_code=404, detail="Room code not found")
    job = jobs["hits"]["hits"][0]["_source"]
    job_id = job["job_id"]

    # 2. Get all sessions for this job
    sessions_res = es.search(
        index="interview_session_index",
        size=200,
        query={"term": {"job_id": job_id}},
        sort=[{"started_at": {"order": "desc"}}],
    )
    sessions = [h["_source"] for h in sessions_res["hits"]["hits"]]

    # 3. Enrich each session with candidate profile + evaluation
    results = []
    for session in sessions:
        cand_id = session.get("candidate_id", "")
        # Candidate profile
        try:
            profile = es.get(index="candidate_profile_index", id=cand_id)["_source"]
        except NotFoundError:
            profile = {"candidate_id": cand_id, "name": "Unknown", "email": ""}
        # Evaluation
        try:
            evaluation = es.get(index="evaluation_index", id=session["session_id"])["_source"]
        except NotFoundError:
            evaluation = None
        results.append({
            "session":    session,
            "candidate":  profile,
            "evaluation": evaluation,
        })

    return {"job": {k: v for k, v in job.items() if k != "description_embedding"}, "candidates": results}


# ──────────────────────────────────────────────────────────────────────────────
# Room Management & Results Viewing — permanent room system with full results
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/interviewer/{interviewer_id}/rooms")
def get_interviewer_rooms(interviewer_id: str):
    """Get all rooms created by an interviewer."""
    result = es.search(
        index="job_requirements_index",
        size=100,
        query={"term": {"interviewer_id": interviewer_id}},
        sort=[{"created_at": {"order": "desc"}}],
    )
    
    rooms = []
    for hit in result["hits"]["hits"]:
        room_data = hit["_source"]
        
        # Get candidate count for this room
        candidate_count_res = es.search(
            index="candidate_profile_index",
            size=0,
            query={"term": {"job_id": room_data["job_id"]}},
        )
        candidate_count = candidate_count_res["hits"]["total"]["value"]
        
        rooms.append({
            "job_id": room_data["job_id"],
            "room_code": room_data["room_code"],
            "title": room_data["title"],
            "description": room_data["description"],
            "department": room_data.get("department", ""),
            "location": room_data.get("location", ""),
            "created_at": room_data["created_at"],
            "candidate_count": candidate_count,
            "active": room_data.get("active", True)
        })
    
    return {"rooms": rooms}


@app.get("/rooms/{room_code}/results")
def get_room_results(room_code: str):
    """Get comprehensive results for a specific room including all candidates and evaluations."""
    # Get room/job info
    job_res = es.search(
        index="job_requirements_index", 
        size=1,
        query={"match": {"room_code": room_code.upper()}},
    )
    if job_res["hits"]["total"]["value"] == 0:
        raise HTTPException(status_code=404, detail="Room not found")
        
    job = job_res["hits"]["hits"][0]["_source"]
    job_id = job["job_id"]
    
    # Get all candidates for this room 
    candidates_res = es.search(
        index="candidate_profile_index",
        size=100,
        query={"term": {"job_id": job_id}},
        sort=[{"created_at": {"order": "desc"}}],
    )
    
    candidates_with_results = []
    for hit in candidates_res["hits"]["hits"]:
        candidate = hit["_source"]
        candidate_id = candidate["candidate_id"]
        
        # Get interview sessions for this candidate
        sessions_res = es.search(
            index="interview_session_index",
            size=10,
            query={"bool": {"must": [
                {"term": {"candidate_id": candidate_id}},
                {"term": {"job_id": job_id}}
            ]}},
            sort=[{"started_at": {"order": "desc"}}],
        )
        
        sessions_with_eval = []
        for session_hit in sessions_res["hits"]["hits"]:
            session = session_hit["_source"]
            
            # Get evaluation for this session
            try:
                eval_res = es.get(index="evaluation_index", id=session["session_id"])
                evaluation = eval_res["_source"]
            except NotFoundError:
                evaluation = None
                
            sessions_with_eval.append({
                "session": session,
                "evaluation": evaluation
            })
        
        candidates_with_results.append({
            "candidate": candidate,
            "sessions": sessions_with_eval,
            "session_count": len(sessions_with_eval)
        })
    
    return {
        "room": {k: v for k, v in job.items() if k != "description_embedding"},
        "candidates": candidates_with_results,
        "total_candidates": len(candidates_with_results)
    }


@app.get("/candidates/{candidate_id}/details")
def get_candidate_details(candidate_id: str):
    """Get detailed results for a specific candidate including all sessions and evaluations."""
    try:
        # Get candidate profile
        candidate_res = es.get(index="candidate_profile_index", id=candidate_id)
        candidate = candidate_res["_source"]
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Candidate not found")
    
    # Get all sessions for this candidate
    sessions_res = es.search(
        index="interview_session_index",
        size=50,
        query={"term": {"candidate_id": candidate_id}},
        sort=[{"started_at": {"order": "desc"}}],
    )
    
    detailed_sessions = []
    for hit in sessions_res["hits"]["hits"]:
        session = hit["_source"]
        session_id = session["session_id"]
        
        # Get evaluation
        try:
            eval_res = es.get(index="evaluation_index", id=session_id)
            evaluation = eval_res["_source"]
        except NotFoundError:
            evaluation = None
        
        # Get interview transcript
        try:
            transcript_res = es.search(
                index="transcript_index",
                size=1000,
                query={"term": {"session_id": session_id}},
                sort=[{"timestamp": {"order": "asc"}}],
            )
            transcript = [t["_source"] for t in transcript_res["hits"]["hits"]]
        except Exception:
            transcript = []
            
        detailed_sessions.append({
            "session": session,
            "evaluation": evaluation,
            "transcript": transcript,
            "transcript_length": len(transcript)
        })
    
    # Get job information
    job_id = candidate.get("job_id", "")
    job_info = {}
    if job_id:
        try:
            job_res = es.search(
                index="job_requirements_index",
                size=1,
                query={"term": {"job_id": job_id}},
            )
            if job_res["hits"]["hits"]:
                job_info = {k: v for k, v in job_res["hits"]["hits"][0]["_source"].items() 
                           if k != "description_embedding"}
        except Exception:
            pass
    
    return {
        "candidate": candidate,
        "job": job_info,
        "sessions": detailed_sessions,
        "total_sessions": len(detailed_sessions)
    }


@app.delete("/rooms/{room_code}")
def delete_room(room_code: str):
    """Interviewer deletes a room and all associated data."""
    # Get room info
    job_res = es.search(
        index="job_requirements_index",
        size=1,
        query={"match": {"room_code": room_code.upper()}},
    )
    if job_res["hits"]["total"]["value"] == 0:
        raise HTTPException(status_code=404, detail="Room not found")
    
    job_id = job_res["hits"]["hits"][0]["_source"]["job_id"]
    
    # Delete job requirement
    es.delete_by_query(
        index="job_requirements_index",
        query={"term": {"job_id": job_id}}
    )
    
    # Get all candidates for this job
    candidates_res = es.search(
        index="candidate_profile_index",
        size=1000,
        query={"term": {"job_id": job_id}},
    )
    
    candidate_ids = [hit["_source"]["candidate_id"] for hit in candidates_res["hits"]["hits"]]
    
    # Delete candidate profiles
    if candidate_ids:
        es.delete_by_query(
            index="candidate_profile_index",
            query={"terms": {"candidate_id": candidate_ids}}
        )
    
    # Delete interview sessions
    if candidate_ids:
        es.delete_by_query(
            index="interview_session_index",
            query={"terms": {"candidate_id": candidate_ids}}
        )
    
    # Delete evaluations and transcripts by scanning sessions first
    session_res = es.search(
        index="interview_session_index",
        size=1000,
        query={"term": {"job_id": job_id}},
    )
    session_ids = [hit["_source"]["session_id"] for hit in session_res["hits"]["hits"]]
    
    if session_ids:
        # Delete evaluations
        es.delete_by_query(
            index="evaluation_index",
            query={"terms": {"session_id": session_ids}}
        )
        
        # Delete transcripts  
        es.delete_by_query(
            index="transcript_index",
            query={"terms": {"session_id": session_ids}}
        )
    
    return {"message": f"Room {room_code} and all associated data deleted successfully"}


# ──────────────────────────────────────────────────────────────────────────────
# Candidate Registration — join interview rooms
# ──────────────────────────────────────────────────────────────────────────────


@app.post("/candidates/register", status_code=201)
def register_candidate(body: CandidateRegister):
    """Candidate registers with a room code; returns candidate_id + job info."""
    # Look up room — use match (not term) as room_code is text-analysed by default
    jobs = es.search(
        index="job_requirements_index",
        size=1,
        query={"match": {"room_code": body.room_code.upper()}},
    )
    if jobs["hits"]["total"]["value"] == 0:
        raise HTTPException(status_code=404, detail="Room code not found")
    job = jobs["hits"]["hits"][0]["_source"]

    # Create candidate profile
    candidate_id = f"cand_{uuid.uuid4().hex[:10]}"
    profile_doc = {
        "candidate_id": candidate_id,
        "name":         body.name,
        "email":        body.email,
        "job_id":       job["job_id"],
        "resume_text":  body.resume_text,
        "created_at":   datetime.now(timezone.utc).isoformat(),
    }
    es.index(index="candidate_profile_index", id=candidate_id, document=profile_doc)

    return {
        "candidate_id": candidate_id,
        "job_id":       job["job_id"],
        "job_title":    job["title"],
        "session_id":   f"sess_{uuid.uuid4().hex[:10]}",
    }


# ── Job Requirements ──────────────────────────────────────────────────────────
@app.get("/functions/get_job_requirements/{job_id}")
def get_job_requirements(job_id: str):
    """Retrieve job spec from Elasticsearch."""
    try:
        result = es.get(index="job_requirements_index", id=job_id)
        return result["_source"]
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")


@app.get("/functions/list_jobs")
def list_jobs():
    """Return all job postings (up to 100)."""
    result = es.search(index="job_requirements_index", size=100, query={"match_all": {}})
    return [h["_source"] for h in result["hits"]["hits"]]


# ── Candidate Profiles ────────────────────────────────────────────────────────
@app.post("/functions/create_candidate_profile")
def create_candidate_profile(profile: CandidateProfile):
    """Register a new candidate."""
    doc = {
        **profile.dict(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    es.index(index="candidate_profile_index", id=profile.candidate_id, document=doc)
    return {"status": "created", "candidate_id": profile.candidate_id}


@app.get("/functions/get_candidate_profile/{candidate_id}")
def get_candidate_profile(candidate_id: str):
    try:
        result = es.get(index="candidate_profile_index", id=candidate_id)
        return result["_source"]
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"Candidate '{candidate_id}' not found")


# ── Transcript Storage ────────────────────────────────────────────────────────
@app.post("/functions/store_transcript")
def store_transcript(entry: TranscriptEntry):
    """Persist one conversation turn with a semantic embedding."""
    embedding = embedding_model.encode(entry.content).tolist()
    es.index(
        index="transcript_index",
        document={
            **entry.dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "content_embedding": embedding,
        },
    )
    return {"status": "stored"}


@app.get("/functions/get_transcript/{session_id}")
def get_transcript(session_id: str):
    """Retrieve all transcript entries for a session, ordered by time."""
    result = es.search(
        index="transcript_index",
        size=200,
        query={"term": {"session_id": session_id}},
        sort=[{"timestamp": {"order": "asc"}}],
    )
    return [h["_source"] for h in result["hits"]["hits"]]


# ── Skill Storage ─────────────────────────────────────────────────────────────
@app.post("/functions/store_skills")
def store_skills(payload: SkillsPayload):
    """Bulk-store extracted skills from the Skill Extraction Agent."""
    for skill in payload.skills:
        es.index(
            index="candidate_skill_index",
            document={
                "session_id": payload.session_id,
                "candidate_id": payload.candidate_id,
                **skill.dict(),
            },
        )
    return {"status": "stored", "count": len(payload.skills)}


@app.get("/functions/get_skills/{session_id}")
def get_skills(session_id: str):
    result = es.search(
        index="candidate_skill_index",
        size=100,
        query={"term": {"session_id": session_id}},
    )
    return [h["_source"] for h in result["hits"]["hits"]]


# ── Evaluation Storage ────────────────────────────────────────────────────────
@app.post("/functions/store_evaluation")
def store_evaluation(eval_data: Evaluation):
    """Persist the Scoring Agent's evaluation output."""
    es.index(
        index="evaluation_index",
        id=eval_data.session_id,
        document=eval_data.dict(),
    )
    return {"status": "stored"}


@app.get("/functions/get_evaluation/{session_id}")
def get_evaluation(session_id: str):
    try:
        result = es.get(index="evaluation_index", id=session_id)
        return result["_source"]
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Evaluation not found")


# ── Vector Benchmarking ───────────────────────────────────────────────────────
@app.post("/functions/vector_search_similar_hires")
def vector_search_similar_hires(req: BenchmarkRequest):
    """kNN search against historical top hires to rank the current candidate."""
    if not req.skills:
        return {"percentile": 50, "recommendation": "No skills provided"}

    profile_text = f"Skills: {', '.join(req.skills)}"
    embedding = embedding_model.encode(profile_text).tolist()

    results = es.search(
        index="historical_top_hires",
        knn={
            "field": "profile_embedding",
            "query_vector": embedding,
            "k": 10,
            "num_candidates": 100,
            "filter": {"term": {"job_id": req.job_id}},
        },
    )

    hits = results["hits"]["hits"]
    if not hits:
        return {
            "percentile": 50,
            "recommendation": "No benchmark data available yet",
            "similar_hires": 0,
        }

    scores = [h["_source"]["performance_score"] for h in hits]
    avg = sum(scores) / len(scores)

    if avg >= 75:
        recommendation = "Strong Hire"
    elif avg >= 50:
        recommendation = "Consider"
    else:
        recommendation = "Pass"

    return {
        "percentile": round(avg, 1),
        "recommendation": recommendation,
        "similar_hires": len(hits),
        "avg_performance_of_similar": round(avg, 1),
    }


# ── Session Management ────────────────────────────────────────────────────────
@app.post("/functions/update_session_stage")
def update_session_stage(update: StageUpdate):
    """Advance the interview to the next pipeline stage."""
    es.update(
        index="interview_session_index",
        id=update.session_id,
        doc={"stage": update.stage, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return {"status": "updated", "stage": update.stage}


@app.get("/functions/get_session/{session_id}")
def get_session(session_id: str):
    try:
        result = es.get(index="interview_session_index", id=session_id)
        return result["_source"]
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


# ── Bias Flagging ─────────────────────────────────────────────────────────────
@app.post("/functions/flag_evaluation")
def flag_evaluation(flag: BiasFlag):
    """Store a bias alert raised by the Reflection Agent."""
    es.index(
        index="bias_alerts_index",
        document={
            **flag.dict(),
            "flagged_at": datetime.now(timezone.utc).isoformat(),
            "reviewed": False,
        },
    )
    return {"status": "flagged", "risk_level": flag.risk_level}


@app.get("/functions/get_bias_alerts/{session_id}")
def get_bias_alerts(session_id: str):
    result = es.search(
        index="bias_alerts_index",
        size=20,
        query={"term": {"session_id": session_id}},
        sort=[{"flagged_at": {"order": "desc"}}],
    )
    return [h["_source"] for h in result["hits"]["hits"]]


# ── Final Report ──────────────────────────────────────────────────────────────
@app.get("/functions/get_final_report/{session_id}")
def get_final_report(session_id: str):
    """Aggregate all pipeline outputs into one report object."""
    try:
        session = es.get(index="interview_session_index", id=session_id)["_source"]
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    # Evaluation
    try:
        evaluation = es.get(index="evaluation_index", id=session_id)["_source"]
    except NotFoundError:
        evaluation = None

    # Extracted skills
    skills_res = es.search(
        index="candidate_skill_index",
        size=100,
        query={"term": {"session_id": session_id}},
    )
    skills = [h["_source"] for h in skills_res["hits"]["hits"]]

    # Bias alerts
    bias_res = es.search(
        index="bias_alerts_index",
        size=10,
        query={"term": {"session_id": session_id}},
    )
    bias_alerts = [h["_source"] for h in bias_res["hits"]["hits"]]

    return {
        "session": session,
        "evaluation": evaluation,
        "skills": skills,
        "bias_alerts": bias_alerts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Admin helpers — recruiter dashboard data
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/admin/sessions")
def list_sessions(status: Optional[str] = None, size: int = 50):
    query = {"match_all": {}} if not status else {"term": {"status": status}}
    result = es.search(
        index="interview_session_index",
        size=size,
        query=query,
        sort=[{"started_at": {"order": "desc"}}],
    )
    return [h["_source"] for h in result["hits"]["hits"]]


@app.get("/admin/high_bias_alerts")
def high_bias_alerts(reviewed: Optional[bool] = False):
    """Return all HIGH-risk bias alerts that haven't been reviewed."""
    result = es.search(
        index="bias_alerts_index",
        size=50,
        query={
            "bool": {
                "must": [{"term": {"risk_level": "HIGH"}}],
                "filter": [{"term": {"reviewed": reviewed}}],
            }
        },
        sort=[{"flagged_at": {"order": "desc"}}],
    )
    return [h["_source"] for h in result["hits"]["hits"]]


@app.post("/admin/mark_bias_reviewed/{session_id}")
def mark_bias_reviewed(session_id: str):
    """Mark all bias alerts for a session as reviewed."""
    es.update_by_query(
        index="bias_alerts_index",
        query={"term": {"session_id": session_id}},
        script={"source": "ctx._source.reviewed = true"},
    )
    return {"status": "updated"}


# ──────────────────────────────────────────────────────────────────────────────
# Video Recording Upload
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/recordings/upload")
async def upload_recording(
    session_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Receive a .webm recording blob from the candidate and persist it to disk."""
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", session_id)
    dest = RECORDINGS_DIR / f"{safe_id}.webm"
    content = await file.read()
    dest.write_bytes(content)
    log.info("Recording saved  session=%s  bytes=%d  path=%s", session_id, len(content), dest)
    return {"saved": str(dest), "bytes": len(content)}


@app.get("/recordings/{session_id}")
async def serve_recording(session_id: str):
    """Stream a candidate's interview recording to the recruiter."""
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", session_id)
    path = RECORDINGS_DIR / f"{safe_id}.webm"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Recording not found")
    return FileResponse(str(path), media_type="video/webm",
                        headers={"Content-Disposition": f'inline; filename="{safe_id}.webm"'})


@app.post("/cv/upload")
async def upload_cv(
    session_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Receive a PDF/doc CV from the candidate and persist it to disk."""
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", session_id)
    ext = (file.filename or "cv.pdf").rsplit(".", 1)[-1].lower()
    dest = CV_DIR / f"{safe_id}.{ext}"
    content = await file.read()
    dest.write_bytes(content)
    log.info("CV saved  session=%s  bytes=%d  path=%s", session_id, len(content), dest)
    return {"saved": str(dest), "bytes": len(content)}


@app.get("/cv/{session_id}")
async def serve_cv(session_id: str):
    """Stream a candidate's CV to the recruiter."""
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", session_id)
    for ext in ("pdf", "txt", "doc", "docx"):
        path = CV_DIR / f"{safe_id}.{ext}"
        if path.exists():
            mime = "application/pdf" if ext == "pdf" else "application/octet-stream"
            return FileResponse(str(path), media_type=mime,
                                headers={"Content-Disposition": f'inline; filename="{safe_id}.{ext}"'})
    raise HTTPException(status_code=404, detail="CV not found")


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket — real-time interview chat relay
# ──────────────────────────────────────────────────────────────────────────────
@app.websocket("/ws/interview/{session_id}/{job_id}/{candidate_id}")
async def interview_ws(
    websocket: WebSocket,
    session_id: str,
    job_id: str,
    candidate_id: str,
    mic: str = Query("0"),   # "1" = candidate using voice (mic bonus eligible)
):
    """
    Relay messages between the candidate browser and the Elastic Supervisor Agent.

    Protocol:
      → browser sends plain text (candidate's message)
      ← server sends JSON  { "role": "assistant", "message": "..." }
    """
    await websocket.accept()
    mic_mode = (mic == "1")
    log.info("WS connected  session=%s job=%s candidate=%s mic=%s", session_id, job_id, candidate_id, mic_mode)

    # Persist the new session record
    es.index(
        index="interview_session_index",
        id=session_id,
        document={
            "session_id": session_id,
            "candidate_id": candidate_id,
            "job_id": job_id,
            "stage": "GREETING",
            "status": "active",
            "mic_mode": mic_mode,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    # ── Load candidate resume + job requirements for context injection ────────
    _candidate_resume = ""
    _candidate_name = ""
    _job_title = ""
    _job_required_skills: list[str] = []
    _job_description = ""
    try:
        cp = es.get(index="candidate_profile_index", id=candidate_id)["_source"]
        _candidate_resume = cp.get("resume_text", "") or ""
        _candidate_name   = cp.get("name", "") or ""
    except Exception:
        pass
    try:
        jr = es.search(
            index="job_requirements_index", size=1,
            query={"term": {"job_id": job_id}},
        )
        if jr["hits"]["hits"]:
            jd = jr["hits"]["hits"][0]["_source"]
            _job_title           = jd.get("title", "")
            _job_required_skills = jd.get("required_skills", [])
            _job_description     = jd.get("description", "")
    except Exception:
        pass

    # ── Kibana Agent Builder auth ─────────────────────────────────────────────
    # The /api/agent_builder/converse endpoint requires manage_onechat privilege.
    # A scoped Kibana API key typically only has read_onechat, so we always use
    # the elastic superuser via basic auth which has full Kibana privileges.
    agent_headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
    agent_auth = (KIBANA_USER, KIBANA_PASS)

    # Initialise per-session conversation history
    history = _session_history.setdefault(session_id, [])
    # Track every question the agent has already asked (persists for full session)
    _asked_questions: list[str] = []

    # ── Live mode — Elastic Agent Builder /api/agent_builder/converse ─────────

    async def call_agent(user_input: str) -> dict:
        """
        POST to /api/agent_builder/converse.
        We include the last 10 turns as a formatted transcript so the agent
        has conversational context (the API is stateless per call).
        """
        # Build a condensed history string (last 10 turns)
        history_text = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in history[-10:]
        )
        # Resume snippet: first 1200 chars is usually enough to ground questions
        resume_snippet = _candidate_resume[:1200].strip()
        skills_line = ", ".join(_job_required_skills[:12]) if _job_required_skills else "not specified"
        # Build the already-asked block so the agent can never repeat
        asked_block = ""
        if _asked_questions:
            bullets = "\n".join(f"  - {q}" for q in _asked_questions)
            asked_block = f"\n[QUESTIONS YOU HAVE ALREADY ASKED — DO NOT REPEAT OR PARAPHRASE ANY OF THESE]\n{bullets}\n"
        context_prompt = (
            f"[CONTEXT]\n"
            f"session_id: {session_id}\n"
            f"job_id: {job_id}\n"
            f"candidate_id: {candidate_id}\n"
            f"candidate_name: {_candidate_name}\n"
            f"job_title: {_job_title}\n"
            f"required_skills: {skills_line}\n"
            + (f"\n[CANDIDATE RESUME]\n{resume_snippet}\n" if resume_snippet else "")
            + asked_block
            + (f"\n[PRIOR CONVERSATION]\n{history_text}\n" if history else "")
            + f"\n[INSTRUCTION]\nYour next question MUST be different from every question listed above."
              f" For technical questions, reference the candidate's actual resume (companies, tools, projects)."
              f" Behavioural questions may be open and generic to reveal character."
              f"\n\n[CANDIDATE MESSAGE]\n{user_input}"
        )

        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{ELASTIC_AGENT_URL}/api/agent_builder/converse",
                headers=agent_headers,
                auth=agent_auth,
                json={"agent_id": SUPERVISOR_AGENT_ID, "input": context_prompt},
            )
            resp.raise_for_status()
            data = resp.json()
            # Elastic converse API nests the reply under data["response"]["message"]
            nested = data.get("response", {})
            return (
                nested.get("message", "")
                if isinstance(nested, dict)
                else str(nested)
            )

    # Send opening greeting — try agent first, fall back to local instantly
    try:
        reply_text = await call_agent("Start the interview with a professional greeting.")
        try:
            parsed = json.loads(reply_text) if reply_text.strip().startswith("{") else None
        except Exception:
            parsed = None
        greeting_payload = parsed or {"role": "assistant", "message": reply_text, "stage": "GREETING"}
        greeting_text = greeting_payload.get("message", reply_text)
    except Exception as exc:
        log.warning("Agent greeting error (using local)  session=%s  %s", session_id, exc)
        greeting_text = (
            "Welcome! I'm your AI interviewer today. "
            "Could you start by introducing yourself and giving me a quick overview of your background?"
        )
        greeting_payload = {"role": "assistant", "message": greeting_text, "stage": "GREETING"}

    await websocket.send_json(greeting_payload)
    history.append({"role": "assistant", "content": greeting_text})
    # Record greeting as first asked item so the intro isn't repeated
    _asked_questions.append(greeting_text[:120])

    # Persist greeting in background
    async def _persist_greeting(msg: str):
        try:
            emb = await asyncio.get_event_loop().run_in_executor(
                None, lambda: embedding_model.encode(msg).tolist()
            )
            es.index(
                index="transcript_index",
                document={
                    "session_id": session_id, "role": "assistant",
                    "content": msg,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "content_embedding": emb,
                },
            )
        except Exception as _e:
            log.debug("Greeting persist error: %s", _e)
    asyncio.create_task(_persist_greeting(greeting_text))

    try:
        while True:
            user_msg = await websocket.receive_text()

            # ── Special: silence nudge ────────────────────────────────────────
            if user_msg.strip() == '[SILENCE_DETECTED]':
                log.info("Silence nudge triggered  session=%s", session_id)
                try:
                    nudge_prompt = (
                        "The candidate has been completely silent for 60 seconds. "
                        "Gently engage them — offer encouragement, ask if they need "
                        "the question rephrased, suggest they think out loud, or "
                        "ask a simpler follow-up question to get them talking. "
                        "Be warm and supportive, not judgmental."
                    )
                    nudge_text = await call_agent(nudge_prompt)
                    if not nudge_text:
                        nudge_text = (
                            "Take your time — there's no rush. Would you like me to "
                            "rephrase that question, or would you like to start with "
                            "a different aspect of it?"
                        )
                    history.append({"role": "assistant", "content": nudge_text})
                    await websocket.send_json({
                        "role": "assistant",
                        "message": nudge_text,
                        "stage": "NUDGE",
                    })
                except Exception as exc:
                    log.warning("Silence nudge agent error: %s", exc)
                    fallback_nudge = (
                        "I notice you've been quiet for a moment. Take your time — "
                        "feel free to think out loud, or let me know if you'd like "
                        "the question rephrased."
                    )
                    await websocket.send_json({"role": "assistant", "message": fallback_nudge, "stage": "NUDGE"})
                continue  # don't persist or embed the nudge trigger

            history.append({"role": "candidate", "content": user_msg})

            # Persist candidate turn in background (never block the reply)
            async def _persist_candidate(msg: str):
                try:
                    emb = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: embedding_model.encode(msg).tolist()
                    )
                    es.index(
                        index="transcript_index",
                        document={
                            "session_id": session_id, "role": "candidate",
                            "content": msg,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "content_embedding": emb,
                        },
                    )
                except Exception as _e:
                    log.debug("Candidate persist error: %s", _e)
            asyncio.create_task(_persist_candidate(user_msg))

            # Call agent — fall back to local interviewer immediately on any error
            try:
                reply_text = await call_agent(user_msg)
            except Exception as _agent_exc:
                log.warning("Agent call failed (%s); using local interviewer.", _agent_exc)
                reply_text = ""

            # Try to parse structured JSON from agent reply
            try:
                parsed = json.loads(reply_text) if reply_text.strip().startswith("{") else None
            except Exception:
                parsed = None

            if parsed and isinstance(parsed, dict) and parsed.get("message"):
                agent_reply = parsed
            elif reply_text.strip():
                agent_reply = {"role": "assistant", "message": reply_text}
            else:
                # Agent unavailable — generate instant local reply
                agent_reply = _local_interview_reply(history, job_id)

            agent_text = agent_reply.get("message", "")  # clean text for storage

            # ── Server-side dedup: if reply is too similar to a past question, re-ask ──
            def _is_repeat(new_msg: str, past: list[str], threshold: float = 0.72) -> bool:
                """Simple n-gram overlap check — no ML needed."""
                def _tokens(s: str) -> set[str]:
                    return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())
                new_tok = _tokens(new_msg)
                if not new_tok:
                    return False
                for prev in past:
                    prev_tok = _tokens(prev)
                    if not prev_tok:
                        continue
                    overlap = len(new_tok & prev_tok) / max(len(new_tok), len(prev_tok))
                    if overlap >= threshold:
                        log.info("Dedup triggered: new=%r overlaps prev=%r (%.0f%%)", new_msg[:60], prev[:60], overlap*100)
                        return True
                return False

            if agent_text and _is_repeat(agent_text, _asked_questions):
                asked_bullets = "\n".join(f"  - {q}" for q in _asked_questions)
                rewrite_prompt = (
                    f"Your last response was too similar to a question you already asked.\n\n"
                    f"ALREADY ASKED:\n{asked_bullets}\n\n"
                    f"You MUST ask a completely different question on a NEW topic.\n"
                    f"Do not rephrase anything from the list above.\n\n"
                    f"[CANDIDATE MESSAGE]\n{user_msg}"
                )
                try:
                    retry_text = await call_agent(rewrite_prompt)
                    retry_parsed = json.loads(retry_text) if retry_text.strip().startswith("{") else None
                    retry_msg = (retry_parsed or {}).get("message", retry_text) if retry_parsed else retry_text
                    # Only use retry if it's genuinely different
                    if retry_msg and not _is_repeat(retry_msg, _asked_questions, threshold=0.6):
                        agent_reply = retry_parsed if retry_parsed else {"role": "assistant", "message": retry_msg}
                        agent_text = retry_msg
                    else:
                        # Final fallback: use local interviewer with explicit exclusion list
                        agent_reply = _local_interview_reply(history, job_id)
                        agent_text = agent_reply.get("message", "")
                except Exception as _retry_exc:
                    log.warning("Dedup retry failed (%s); using local fallback.", _retry_exc)
                    agent_reply = _local_interview_reply(history, job_id)
                    agent_text = agent_reply.get("message", "")

            # Persist agent turn in background
            if agent_text:
                history.append({"role": "assistant", "content": agent_text})
                # Track every question asked so the agent never repeats one
                _asked_questions.append(agent_text[:160])
            async def _persist_agent(msg: str):
                try:
                    emb = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: embedding_model.encode(msg).tolist()
                    )
                    es.index(
                        index="transcript_index",
                        document={
                            "session_id": session_id, "role": "assistant",
                            "content": msg,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "content_embedding": emb,
                        },
                    )
                except Exception as _e:
                    log.debug("Agent persist error: %s", _e)
            if agent_text:
                asyncio.create_task(_persist_agent(agent_text))

            # Push to interviewer monitor
            for monitor_ws in _monitor_connections.get(session_id, []):
                try:
                    await monitor_ws.send_json({"type": "transcript", "turn": agent_reply})
                except Exception:
                    pass

            await websocket.send_json(agent_reply)

            # If interview complete, trigger background evaluation pipeline
            if agent_reply.get("stage") == "COMPLETE":
                # ── 1. Extract inline evaluation if agent returned it ──────────────
                inline_eval = agent_reply.get("evaluation")
                if inline_eval and isinstance(inline_eval, dict):
                    try:
                        # Apply mic bonus (+10%) to communication_score
                        _icomm = float(inline_eval.get("communication_score", 0))
                        _itech = float(inline_eval.get("technical_score", 0))
                        _ips   = float(inline_eval.get("problem_solving_score", 0))
                        _icf   = float(inline_eval.get("cultural_fit_score", 0))
                        if mic_mode:
                            _icomm = min(100.0, round(_icomm * 1.10, 1))
                            log.info("Mic bonus applied (inline)  session=%s  comm=%.1f", session_id, _icomm)
                        _ioverall = float(inline_eval.get("overall_score") or
                                          inline_eval.get("total_score") or
                                          round(_itech*0.4 + _icomm*0.2 + _ips*0.2 + _icf*0.2, 1))
                        eval_doc = {
                            "session_id":             session_id,
                            "candidate_id":           candidate_id,
                            "job_id":                 job_id,
                            "technical_score":        _itech,
                            "communication_score":    _icomm,
                            "problem_solving_score":  _ips,
                            "cultural_fit_score":     _icf,
                            "overall_score":          _ioverall,
                            "mic_mode":               mic_mode,
                            "mic_bonus_applied":      mic_mode,
                            "recommendation":         inline_eval.get("recommendation", "NEUTRAL"),
                            "strengths":              inline_eval.get("strengths", []),
                            "weaknesses":             inline_eval.get("weaknesses", []),
                            "summary":                inline_eval.get("summary", ""),
                            "score_explanations":     inline_eval.get("score_explanations", []),
                            "bias_detected":          inline_eval.get("bias_detected", False),
                            "bias_notes":             inline_eval.get("bias_notes", []),
                            "source":                 "inline_agent",
                            "scored_at":              datetime.now(timezone.utc).isoformat(),
                        }
                        es.index(index="evaluation_index", id=session_id, document=eval_doc)
                        log.info("Stored inline agent evaluation  session=%s", session_id)
                    except Exception as _e:
                        log.error("Failed to store inline evaluation  session=%s  %s", session_id, _e)

                es.update(
                    index="interview_session_index", id=session_id,
                    doc={"status": "completed", "stage": "COMPLETE",
                         "completed_at": datetime.now(timezone.utc).isoformat()},
                )
                # ── 2. Always fire post-interview pipeline (fills gaps) ────────────
                asyncio.create_task(
                    _run_post_interview_pipeline(session_id, job_id, candidate_id, inline_eval)
                )
                # Clean up history
                _session_history.pop(session_id, None)

    except WebSocketDisconnect:
        log.info("WS disconnected  session=%s", session_id)
        es.update(
            index="interview_session_index", id=session_id,
            doc={"status": "disconnected", "completed_at": datetime.now(timezone.utc).isoformat()},
        )
        _session_history.pop(session_id, None)
        # Fire evaluation pipeline even on early disconnect (if enough transcript exists)
        asyncio.create_task(
            _run_post_interview_pipeline(session_id, job_id, candidate_id, None)
        )
    except httpx.HTTPError as exc:
        log.error("Agent relay error  session=%s  %s", session_id, exc)
        await websocket.send_json({"role": "system", "error": str(exc)})
        es.update(index="interview_session_index", id=session_id, doc={"status": "error"})


# ──────────────────────────────────────────────────────────────────────────────
# Job Management — Recruiter Portal CRUD
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Post-Interview Evaluation Pipeline
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Post-Interview Evaluation Pipeline  (Analysis → Evaluation → Benchmark)
# All reasoning is delegated to Elastic Agents; no keyword matching or
# heuristic scoring anywhere in this pipeline.
# ──────────────────────────────────────────────────────────────────────────────

async def _run_post_interview_pipeline(
    session_id: str,
    job_id: str,
    candidate_id: str,
    inline_evaluation: Optional[dict] = None,
):
    """
    Three-stage agent pipeline triggered after every interview.

    Stage 1 — Analysis Agent
        Reads transcript_index and job_requirements_index via its built-in ES
        search tools.  Extracts skills with evidence and stores each to
        candidate_skill_index.

    Stage 2 — Evaluation Agent
        Reads transcript_index, candidate_skill_index, and
        job_requirements_index via its built-in ES tools.  Returns a full
        structured evaluation with scores, reasoning, and recommendation.
        Skipped if an inline evaluation was already stored during the session.

    Stage 3 — Benchmark Agent
        Reads evaluation_index and historical_top_hires to rank the candidate
        against past hires and stores results to benchmark_results_index.
    """
    await asyncio.sleep(2)  # let the final ES writes settle
    log.info("Post-interview pipeline starting  session=%s  has_inline=%s",
             session_id, inline_evaluation is not None)

    try:
        # ── Stage 1: Analysis Agent ────────────────────────────────────────────
        log.info("[Stage 1] Analysis agent  session=%s", session_id)
        analysis_prompt = (
            f"You are the Analysis Agent in an AI interviewing system.\n\n"
            f"Task: Analyse the completed job interview.\n"
            f"  session_id = {session_id}\n"
            f"  job_id     = {job_id}\n"
            f"  candidate_id = {candidate_id}\n\n"
            f"Steps:\n"
            f"  1. Use your Elasticsearch search tool to retrieve all documents from "
            f"     transcript_index where session_id = '{session_id}'.\n"
            f"  2. Use your Elasticsearch search tool to retrieve the job document from "
            f"     job_requirements_index where _id = '{job_id}'.\n"
            f"  3. Read the full conversation and identify every skill the candidate "
            f"     demonstrated with supporting evidence from the transcript.\n"
            f"  4. For each skill, estimate proficiency and a confidence score 0.0-1.0.\n\n"
            f"Return ONLY valid JSON (no markdown, no explanation):\n"
            f'{{\n'
            f'  "extracted_skills": [\n'
            f'    {{"skill_name": "...", "proficiency": "beginner|intermediate|advanced|expert",'
            f' "evidence": "exact quote from transcript", "confidence": 0.0}}\n'
            f'  ],\n'
            f'  "reasoning_patterns": ["..."],\n'
            f'  "required_skills_not_evidenced": ["..."],\n'
            f'  "skill_coverage_pct": 0\n'
            f'}}'
        )
        try:
            analysis_raw = await _call_agent(ANALYSIS_AGENT_ID, analysis_prompt, timeout=120.0)
            log.info("[Stage 1] Agent response (first 300): %s", analysis_raw[:300])
            analysis_data = _extract_json(analysis_raw)
            if isinstance(analysis_data, dict) and "extracted_skills" in analysis_data:
                skills = analysis_data["extracted_skills"]
                for skill in skills:
                    if not isinstance(skill, dict):
                        continue
                    es.index(
                        index="candidate_skill_index",
                        document={
                            "session_id":   session_id,
                            "candidate_id": candidate_id,
                            "job_id":       job_id,
                            "skill_name":   skill.get("skill_name", ""),
                            "proficiency":  skill.get("proficiency", "intermediate"),
                            "evidence":     skill.get("evidence", ""),
                            "confidence":   float(skill.get("confidence", 0.7)),
                            "verified":     False,
                            "extracted_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                log.info("[Stage 1] Stored %d skills  session=%s", len(skills), session_id)
            else:
                log.warning("[Stage 1] Analysis agent returned unexpected structure: %s",
                            analysis_raw[:200])
        except Exception as exc:
            log.error("[Stage 1] Analysis agent error  session=%s  %s", session_id, exc)

        # ── Stage 2: Evaluation Agent ──────────────────────────────────────────
        eval_exists = False
        try:
            es.get(index="evaluation_index", id=session_id)
            eval_exists = True
        except Exception:
            pass

        if eval_exists:
            log.info("[Stage 2] Evaluation already stored (inline), skipping  session=%s",
                     session_id)
        else:
            log.info("[Stage 2] Evaluation agent  session=%s", session_id)
            eval_prompt = (
                f"You are the Evaluation Agent in an AI interviewing system.\n\n"
                f"Task: Produce a structured evaluation of this candidate.\n"
                f"  session_id   = {session_id}\n"
                f"  job_id       = {job_id}\n"
                f"  candidate_id = {candidate_id}\n\n"
                f"Steps:\n"
                f"  1. Retrieve all transcript turns from transcript_index "
                f"     where session_id = '{session_id}'.\n"
                f"  2. Retrieve extracted skills from candidate_skill_index "
                f"     where session_id = '{session_id}'.\n"
                f"  3. Retrieve the job spec from job_requirements_index "
                f"     where _id = '{job_id}'.\n"
                f"  4. Score the candidate 0-100 on technical, communication, "
                f"     problem_solving, and cultural_fit, with evidence-backed reasoning.\n"
                f"  5. Compute overall_score = technical*0.4 + communication*0.2 + "
                f"     problem_solving*0.2 + cultural_fit*0.2 (rounded to 1 dp).\n\n"
                f"Return ONLY valid JSON (no markdown, no explanation):\n"
                f'{{\n'
                f'  "technical_score": 0,\n'
                f'  "communication_score": 0,\n'
                f'  "problem_solving_score": 0,\n'
                f'  "cultural_fit_score": 0,\n'
                f'  "overall_score": 0.0,\n'
                f'  "recommendation": "STRONG_HIRE|HIRE|NEUTRAL|PASS|STRONG_PASS",\n'
                f'  "strengths": ["..."],\n'
                f'  "weaknesses": ["..."],\n'
                f'  "summary": "2-3 sentence assessment",\n'
                f'  "score_explanations": [\n'
                f'    {{"dimension": "technical", "score": 0, "reasoning": "...", "evidence": ["quote"]}}\n'
                f'  ],\n'
                f'  "bias_detected": false,\n'
                f'  "bias_notes": []\n'
                f'}}'
            )
            try:
                eval_raw = await _call_agent(EVALUATION_AGENT_ID, eval_prompt, timeout=120.0)
                log.info("[Stage 2] Agent response (first 300): %s", eval_raw[:300])
                eval_data = _extract_json(eval_raw)
                if isinstance(eval_data, dict):
                    t  = eval_data.get("technical_score",       0)
                    comm_score = float(eval_data.get("communication_score",   0))
                    ps = eval_data.get("problem_solving_score", 0)
                    cf = eval_data.get("cultural_fit_score",    0)
                    # Look up mic_mode from session and apply +10% bonus if set
                    try:
                        _sess2 = es.get(index="interview_session_index", id=session_id)["_source"]
                        _mic_mode2 = _sess2.get("mic_mode", False)
                    except Exception:
                        _mic_mode2 = False
                    if _mic_mode2:
                        comm_score = min(100.0, round(comm_score * 1.10, 1))
                        log.info("[Stage 2] Mic bonus applied  session=%s  comm=%.1f", session_id, comm_score)
                    overall = eval_data.get("overall_score") or \
                              round(t * 0.4 + comm_score * 0.2 + ps * 0.2 + cf * 0.2, 1)
                    es.index(
                        index="evaluation_index",
                        id=session_id,
                        document={
                            "session_id":    session_id,
                            "candidate_id":  candidate_id,
                            "job_id":        job_id,
                            "overall_score": overall,
                            "mic_mode":      _mic_mode2,
                            "mic_bonus_applied": _mic_mode2,
                            "source":        "evaluation_agent",
                            "scored_at":     datetime.now(timezone.utc).isoformat(),
                            **{**eval_data, "communication_score": comm_score},
                        },
                    )
                    log.info("[Stage 2] Stored evaluation  session=%s  score=%s  rec=%s",
                             session_id, overall, eval_data.get("recommendation"))
                else:
                    log.warning("[Stage 2] Evaluation agent returned unexpected structure: %s",
                                eval_raw[:200])
            except Exception as exc:
                log.error("[Stage 2] Evaluation agent error  session=%s  %s", session_id, exc)

        # ── Stage 3: Benchmark Agent ───────────────────────────────────────────
        log.info("[Stage 3] Benchmark agent  session=%s", session_id)
        bench_prompt = (
            f"You are the Benchmark Agent in an AI interviewing system.\n\n"
            f"Task: Benchmark this candidate against historical top hires.\n"
            f"  session_id   = {session_id}\n"
            f"  job_id       = {job_id}\n"
            f"  candidate_id = {candidate_id}\n\n"
            f"Steps:\n"
            f"  1. Retrieve the evaluation from evaluation_index "
            f"     where _id = '{session_id}'.\n"
            f"  2. Search historical_top_hires for records matching job_id = '{job_id}' "
            f"     or similar roles to obtain benchmark scores.\n"
            f"  3. Compute the candidate\'s percentile rank and overall benchmark score.\n\n"
            f"Return ONLY valid JSON (no markdown, no explanation):\n"
            f'{{\n'
            f'  "benchmark_score": 0.0,\n'
            f'  "percentile": 0,\n'
            f'  "ranking": "top_10|top_25|top_50|bottom_50",\n'
            f'  "comparison_notes": "...",\n'
            f'  "similar_hire_count": 0,\n'
            f'  "reasoning": "..."\n'
            f'}}'
        )
        try:
            bench_raw = await _call_agent(BENCHMARK_AGENT_ID, bench_prompt, timeout=120.0)
            log.info("[Stage 3] Agent response (first 300): %s", bench_raw[:300])
            bench_data = _extract_json(bench_raw)
            if isinstance(bench_data, dict):
                es.index(
                    index="benchmark_results_index",
                    id=f"{job_id}_{session_id}",
                    document={
                        "session_id":     session_id,
                        "candidate_id":   candidate_id,
                        "job_id":         job_id,
                        "benchmarked_at": datetime.now(timezone.utc).isoformat(),
                        **bench_data,
                    },
                )
                log.info("[Stage 3] Stored benchmark  session=%s  percentile=%s",
                         session_id, bench_data.get("percentile"))
            else:
                log.warning("[Stage 3] Benchmark agent returned unexpected structure: %s",
                            bench_raw[:200])
        except Exception as exc:
            log.error("[Stage 3] Benchmark agent error  session=%s  %s", session_id, exc)

        # ── Mark session as fully processed ───────────────────────────────────
        es.update(
            index="interview_session_index",
            id=session_id,
            doc={
                "pipeline_done": True,
                "stage":         "COMPLETE",
                "updated_at":    datetime.now(timezone.utc).isoformat(),
            },
        )
        log.info("Post-interview pipeline complete  session=%s", session_id)

    except Exception as exc:
        log.error("Post-interview pipeline FATAL  session=%s  %s", session_id, exc,
                  exc_info=True)


# ──────────────────────────────────────────────────────────────────────────────
# Manual Re-Evaluate endpoint
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/evaluate/{session_id}")
async def trigger_evaluation(session_id: str):
    """
    Recruiter can manually trigger (or re-trigger) the post-interview
    evaluation pipeline for any session.
    """
    try:
        session = es.get(index="interview_session_index", id=session_id)["_source"]
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    # Delete any existing evaluation so the pipeline re-runs from scratch
    try:
        es.delete(index="evaluation_index", id=session_id)
    except Exception:
        pass

    asyncio.create_task(
        _run_post_interview_pipeline(
            session_id=session_id,
            job_id=session.get("job_id", ""),
            candidate_id=session.get("candidate_id", ""),
            inline_evaluation=None,
        )
    )
    return {"status": "evaluation_triggered", "session_id": session_id}


@app.get("/evaluate/{session_id}")
def get_evaluation_status(session_id: str):
    """Poll evaluation status for a session."""
    try:
        eval_doc = es.get(index="evaluation_index", id=session_id)["_source"]
        return {"status": "complete", "evaluation": eval_doc}
    except NotFoundError:
        # Check if session even exists
        try:
            sess = es.get(index="interview_session_index", id=session_id)["_source"]
            return {"status": "pending", "session_stage": sess.get("stage", "UNKNOWN")}
        except NotFoundError:
            raise HTTPException(status_code=404, detail="Session not found")


# ──────────────────────────────────────────────────────────────────────────────
# Job Management — Recruiter Portal CRUD
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/jobs", status_code=201)
def create_job(job: JobRequirement):
    """Create or replace a job posting."""
    doc = {
        **job.dict(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Generate description embedding for semantic search
    doc["description_embedding"] = embedding_model.encode(
        f"{job.title} {job.description} {' '.join(job.required_skills)}"
    ).tolist()
    es.index(index="job_requirements_index", id=job.job_id, document=doc)
    return {"status": "created", "job_id": job.job_id}


@app.put("/jobs/{job_id}")
def update_job(job_id: str, job: JobRequirement):
    """Update an existing job posting."""
    try:
        es.get(index="job_requirements_index", id=job_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    update_doc = {
        **job.dict(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "description_embedding": embedding_model.encode(
            f"{job.title} {job.description} {' '.join(job.required_skills)}"
        ).tolist(),
    }
    es.index(index="job_requirements_index", id=job_id, document=update_doc)
    return {"status": "updated", "job_id": job_id}


@app.patch("/jobs/{job_id}/toggle")
def toggle_job(job_id: str):
    """Toggle a job posting between active and inactive."""
    try:
        result = es.get(index="job_requirements_index", id=job_id)
        current = result["_source"].get("active", True)
        es.update(
            index="job_requirements_index",
            id=job_id,
            doc={"active": not current, "updated_at": datetime.now(timezone.utc).isoformat()},
        )
        return {"status": "updated", "active": not current}
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """Delete a job posting."""
    try:
        es.delete(index="job_requirements_index", id=job_id)
        return {"status": "deleted"}
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")


# ──────────────────────────────────────────────────────────────────────────────
# Analytics — Recruiter Dashboard
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/admin/analytics")
def get_analytics():
    """Aggregate stats for the recruiter dashboard."""
    # Total sessions
    total_sessions = es.count(index="interview_session_index")["count"]

    # Sessions by status
    status_agg = es.search(
        index="interview_session_index",
        size=0,
        aggs={"by_status": {"terms": {"field": "status"}}},
    )
    by_status = {
        b["key"]: b["doc_count"]
        for b in status_agg["aggregations"]["by_status"]["buckets"]
    }

    # Sessions by stage
    stage_agg = es.search(
        index="interview_session_index",
        size=0,
        aggs={"by_stage": {"terms": {"field": "stage"}}},
    )
    by_stage = {
        b["key"]: b["doc_count"]
        for b in stage_agg["aggregations"]["by_stage"]["buckets"]
    }

    # Average scores
    score_agg = es.search(
        index="evaluation_index",
        size=0,
        aggs={
            "avg_total":   {"avg": {"field": "total_score"}},
            "avg_tech":    {"avg": {"field": "technical_score"}},
            "avg_comm":    {"avg": {"field": "communication_score"}},
            "avg_ps":      {"avg": {"field": "problem_solving_score"}},
            "avg_cf":      {"avg": {"field": "cultural_fit_score"}},
            "by_rec":      {"terms": {"field": "recommendation"}},
        },
    )
    agg = score_agg["aggregations"]

    recommendations = {
        b["key"]: b["doc_count"]
        for b in agg["by_rec"]["buckets"]
    }

    # Bias alerts breakdown
    bias_agg = es.search(
        index="bias_alerts_index",
        size=0,
        aggs={"by_risk": {"terms": {"field": "risk_level"}}},
    )
    by_risk = {
        b["key"]: b["doc_count"]
        for b in bias_agg["aggregations"]["by_risk"]["buckets"]
    }

    total_evals = es.count(index="evaluation_index")["count"]

    return {
        "total_sessions":    total_sessions,
        "total_evaluations": total_evals,
        "by_status":         by_status,
        "by_stage":          by_stage,
        "recommendations":   recommendations,
        "bias_risk_counts":  by_risk,
        "avg_scores": {
            "total":         round(agg["avg_total"]["value"] or 0, 1),
            "technical":     round(agg["avg_tech"]["value"] or 0, 1),
            "communication": round(agg["avg_comm"]["value"] or 0, 1),
            "problem_solving": round(agg["avg_ps"]["value"] or 0, 1),
            "cultural_fit":  round(agg["avg_cf"]["value"] or 0, 1),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/admin/evaluations")
def list_evaluations(
    recommendation: Optional[str] = None,
    min_score: Optional[float] = None,
    job_id: Optional[str] = None,
    size: int = 50,
):
    """List evaluations with optional filters, newest first via session lookup."""
    must: list = []
    if recommendation:
        must.append({"term": {"recommendation": recommendation}})
    if job_id:
        must.append({"term": {"job_id": job_id}})
    if min_score is not None:
        must.append({"range": {"total_score": {"gte": min_score}}})

    query = {"bool": {"must": must}} if must else {"match_all": {}}
    result = es.search(index="evaluation_index", size=size, query=query)
    return [h["_source"] for h in result["hits"]["hits"]]


@app.get("/admin/candidates")
def list_candidates(job_id: Optional[str] = None, size: int = 100):
    """List all candidate profiles, optionally filtered by job."""
    query = {"term": {"job_id": job_id}} if job_id else {"match_all": {}}
    result = es.search(
        index="candidate_profile_index",
        size=size,
        query=query,
        sort=[{"created_at": {"order": "desc"}}],
    )
    return [h["_source"] for h in result["hits"]["hits"]]


# ──────────────────────────────────────────────────────────────────────────────
# Interviewer Portal — live override + monitoring
# ──────────────────────────────────────────────────────────────────────────────

# In-memory store for active WebSocket monitor connections
_monitor_connections: dict[str, list[WebSocket]] = {}


@app.post("/admin/override/{session_id}")
def post_interviewer_note(session_id: str, body: InterviewerNote):
    """
    Interviewer adds a note or issues an action (pause / end / escalate) for
    a live session.  The note is persisted and broadcast to any open monitor
    WebSocket connections watching this session.
    """
    doc = {
        "session_id": session_id,
        "note":       body.note,
        "action":     body.action,
        "authored_by": "interviewer",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    es.index(index="transcript_index", document={
        "session_id": session_id,
        "role":       "interviewer_note",
        "content":    f"[{body.action.upper()}] {body.note}",
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "content_embedding": embedding_model.encode(body.note).tolist(),
    })

    if body.action in ("pause", "end", "escalate"):
        new_status = {"pause": "paused", "end": "ended_by_interviewer",
                      "escalate": "escalated"}.get(body.action, "active")
        es.update(
            index="interview_session_index",
            id=session_id,
            doc={"status": new_status,
                 "updated_at": datetime.now(timezone.utc).isoformat()},
        )

    return {"status": "ok", "action": body.action}


@app.get("/admin/notes/{session_id}")
def get_notes(session_id: str):
    """Retrieve interviewer notes for a session."""
    result = es.search(
        index="transcript_index",
        size=50,
        query={"bool": {"must": [
            {"term": {"session_id": session_id}},
            {"term": {"role": "interviewer_note"}},
        ]}},
        sort=[{"timestamp": {"order": "asc"}}],
    )
    return [h["_source"] for h in result["hits"]["hits"]]


@app.websocket("/ws/monitor/{session_id}")
async def monitor_ws(websocket: WebSocket, session_id: str):
    """
    Interviewer monitor WebSocket.
    Sends the current transcript on connect, then pushes real-time updates.
    The interviewer portal can also send JSON commands:
      { "action": "note",      "note": "..." }
      { "action": "pause" }
      { "action": "end" }
      { "action": "escalate",  "note": "..." }
    """
    await websocket.accept()
    _monitor_connections.setdefault(session_id, []).append(websocket)
    log.info("Monitor WS connected  session=%s  total_monitors=%d",
             session_id, len(_monitor_connections[session_id]))

    # Push current session state immediately
    try:
        session = es.get(index="interview_session_index", id=session_id)["_source"]
    except NotFoundError:
        session = {"session_id": session_id, "stage": "UNKNOWN", "status": "unknown"}

    transcript_res = es.search(
        index="transcript_index",
        size=200,
        query={"term": {"session_id": session_id}},
        sort=[{"timestamp": {"order": "asc"}}],
    )
    transcript = [h["_source"] for h in transcript_res["hits"]["hits"]]

    await websocket.send_json({
        "event":      "init",
        "session":    session,
        "transcript": transcript,
    })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"event": "error", "detail": "Invalid JSON"})
                continue

            action = cmd.get("action", "note")
            note   = cmd.get("note", "")

            # Persist the action
            content = f"[{action.upper()}] {note}" if note else f"[{action.upper()}]"
            es.index(index="transcript_index", document={
                "session_id": session_id,
                "role":       "interviewer_note",
                "content":    content,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "content_embedding": embedding_model.encode(content).tolist(),
            })

            if action in ("pause", "end", "escalate"):
                new_status = {"pause": "paused", "end": "ended_by_interviewer",
                              "escalate": "escalated"}.get(action, "active")
                es.update(
                    index="interview_session_index",
                    id=session_id,
                    doc={"status": new_status,
                         "updated_at": datetime.now(timezone.utc).isoformat()},
                )

            # Broadcast to all monitors watching this session
            event_payload = {
                "event":   "interviewer_action",
                "action":  action,
                "note":    note,
                "ts":      datetime.now(timezone.utc).isoformat(),
            }
            dead = []
            for conn in _monitor_connections.get(session_id, []):
                try:
                    await conn.send_json(event_payload)
                except Exception:
                    dead.append(conn)
            for d in dead:
                _monitor_connections[session_id].remove(d)

    except WebSocketDisconnect:
        conns = _monitor_connections.get(session_id, [])
        if websocket in conns:
            conns.remove(websocket)
        log.info("Monitor WS disconnected  session=%s", session_id)


# ══════════════════════════════════════════════════════════════════════════════
# LIVE CODING FEATURE
# ══════════════════════════════════════════════════════════════════════════════

import subprocess
import tempfile
import time

# ──────────────────────────────────────────────────────────────────────────────
# Live Coding Pydantic Models
# ──────────────────────────────────────────────────────────────────────────────

class CodingQuestion(BaseModel):
    title: str
    description: str
    difficulty: Optional[str] = "medium"   # easy | medium | hard
    tags: Optional[List[str]] = []
    examples: Optional[List[dict]] = []
    constraints: Optional[str] = ""
    starter_code_python: Optional[str] = "def solution():\n    pass\n"
    starter_code_js: Optional[str] = "function solution() {\n  \n}\n"


class CodingRoomCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    num_questions: int = 3
    time_limit_minutes: Optional[int] = 60
    interviewer_id: Optional[str] = "default_interviewer"


class LiveCodingRegister(BaseModel):
    room_code: str
    name: str
    email: str


class CodeRunRequest(BaseModel):
    code: str
    language: str          # python | javascript
    session_id: Optional[str] = ""


class EmotionSnapshot(BaseModel):
    session_id: str
    timestamp: str
    emotion: str
    confidence: float


def _gen_lc_room_code() -> str:
    chars = random.choices(string.ascii_uppercase + string.digits, k=4)
    return "LC-" + "".join(chars)


# ──────────────────────────────────────────────────────────────────────────────
# Local Challenger (fallback when agent is unavailable)
# ──────────────────────────────────────────────────────────────────────────────

_CHALLENGER_GENERIC = [
    "What's the time complexity of your current solution? Can you do better?",
    "What happens if the input is empty or None? Does your code handle that?",
    "Your variable names are hard to follow — can you make the intent clearer?",
    "Have you considered edge cases like duplicate values or overflow?",
    "That approach works, but is there a data structure that could make this O(n) instead?",
    "What if the array has only one element? Walk me through your code with that input.",
    "Can you simplify this by breaking it into smaller helper functions?",
    "Think about the space complexity — are you using more memory than you need?",
    "Your loop could potentially run out of bounds — are you sure your indices are safe?",
    "I see you're using a nested loop. Have you considered a two-pointer or sliding-window approach?",
    "What does your code output when the target doesn't exist in the input? Is that the right behaviour?",
    "Could you write a test case that would break your current solution?",
    "There's a more Pythonic way to express this — what built-in functions might help?",
    "You're modifying the input in place — is that safe if the caller uses it again?",
    "Think about recursion vs. iteration here. What are the trade-offs?",
]

_CHALLENGER_NESTED_LOOP = (
    "I see nested loops — your current complexity looks like O(n²). "
    "Think about what information you're repeatedly looking up, and whether a hash map could eliminate the inner loop."
)
_CHALLENGER_NO_EDGE_CASE = (
    "Your code looks like it assumes the input is always valid. "
    "What happens with an empty list, a single element, or negative numbers?"
)
_CHALLENGER_LONG_FUNCTION = (
    "This function is doing a lot of things at once. "
    "Can you refactor it into smaller, focused functions with descriptive names?"
)


def _local_challenger_response(code: str, question: str, user_msg: str = "") -> str:
    """Generate a contextual challenge based on simple code heuristics."""
    if user_msg:
        # Respond to a direct question without solving it
        return (
            "That's an interesting line of thinking. Instead of answering directly, "
            "I'll ask: what would happen to your approach if the input size was 10 million? "
            "Think about the implications and try again."
        )
    lines = code.splitlines()
    # Detect nested loops
    indents = [len(l) - len(l.lstrip()) for l in lines if l.strip().startswith(("for ", "while "))]
    if len(indents) >= 2 and max(indents) > min(indents):
        return _CHALLENGER_NESTED_LOOP
    # Detect very long function
    if len(lines) > 30:
        return _CHALLENGER_LONG_FUNCTION
    # Detect no None/empty checks
    if "None" not in code and "len(" not in code and "empty" not in code.lower():
        return _CHALLENGER_NO_EDGE_CASE
    return random.choice(_CHALLENGER_GENERIC)


# ──────────────────────────────────────────────────────────────────────────────
# Live Coding Room Management
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/coding-rooms", status_code=201)
async def create_coding_room(body: CodingRoomCreate):
    """Recruiter creates a live coding room."""
    for _ in range(5):
        code = _gen_lc_room_code()
        existing = es.search(index="live_coding_room_index", size=1, query={"term": {"room_code": code}})
        if existing["hits"]["total"]["value"] == 0:
            break
    room_id = f"lcroom_{uuid.uuid4().hex[:8]}"
    doc = {
        "room_id":            room_id,
        "room_code":          code,
        "title":              body.title,
        "description":        body.description,
        "num_questions":      body.num_questions,
        "time_limit_minutes": body.time_limit_minutes,
        "interviewer_id":     body.interviewer_id,
        "active":             True,
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }
    es.index(index="live_coding_room_index", id=room_id, document=doc, refresh=True)
    return {"room_code": code, "room_id": room_id}


@app.get("/coding-rooms/{room_code}")
def get_coding_room(room_code: str):
    """Get live coding room details."""
    result = es.search(
        index="live_coding_room_index", size=1,
        query={"term": {"room_code": room_code.upper()}},
    )
    hits = result["hits"]["hits"]
    if not hits:
        raise HTTPException(status_code=404, detail="Coding room not found")
    return hits[0]["_source"]


@app.post("/coding-rooms/{room_code}/questions", status_code=201)
async def add_coding_question(room_code: str, body: CodingQuestion):
    """Recruiter uploads a coding question to a room."""
    # Verify room exists
    result = es.search(index="live_coding_room_index", size=1, query={"term": {"room_code": room_code.upper()}})
    if result["hits"]["total"]["value"] == 0:
        raise HTTPException(status_code=404, detail="Coding room not found")
    question_id = f"q_{uuid.uuid4().hex[:10]}"
    doc = {
        "question_id":         question_id,
        "room_code":           room_code.upper(),
        "title":               body.title,
        "description":         body.description,
        "difficulty":          body.difficulty,
        "tags":                body.tags,
        "examples":            body.examples,
        "constraints":         body.constraints,
        "starter_code_python": body.starter_code_python,
        "starter_code_js":     body.starter_code_js,
        "created_at":          datetime.now(timezone.utc).isoformat(),
    }
    es.index(index="coding_questions_index", id=question_id, document=doc, refresh=True)
    return {"question_id": question_id}


@app.get("/coding-rooms/{room_code}/questions")
def list_coding_questions(room_code: str):
    """List all questions for a coding room (recruiter view)."""
    result = es.search(
        index="coding_questions_index", size=50,
        query={"term": {"room_code": room_code.upper()}},
    )
    questions = [h["_source"] for h in result["hits"]["hits"]]
    return {"questions": questions, "total": len(questions)}


@app.delete("/coding-rooms/{room_code}/questions/{question_id}", status_code=204)
def delete_coding_question(room_code: str, question_id: str):
    """Delete a question from a coding room."""
    try:
        es.delete(index="coding_questions_index", id=question_id, refresh=True)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Question not found")


@app.get("/coding-rooms/{room_code}/sessions")
def get_coding_sessions(room_code: str):
    """Recruiter views all candidate sessions for a coding room."""
    room_res = es.search(index="live_coding_room_index", size=1, query={"term": {"room_code": room_code.upper()}})
    if room_res["hits"]["total"]["value"] == 0:
        raise HTTPException(status_code=404, detail="Coding room not found")
    room = room_res["hits"]["hits"][0]["_source"]
    sessions_res = es.search(
        index="live_coding_session_index", size=200,
        query={"term": {"room_code": room_code.upper()}},
        sort=[{"started_at": {"order": "desc"}}],
    )
    sessions = [h["_source"] for h in sessions_res["hits"]["hits"]]
    return {"room": room, "sessions": sessions}


# ──────────────────────────────────────────────────────────────────────────────
# Live Coding Candidate Registration
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/live-coding/register", status_code=201)
async def register_live_coding(body: LiveCodingRegister):
    """Candidate registers for a live coding session. Returns session + questions."""
    room_code = body.room_code.upper()
    # Get room
    room_res = es.search(index="live_coding_room_index", size=1, query={"term": {"room_code": room_code}})
    if room_res["hits"]["total"]["value"] == 0:
        raise HTTPException(status_code=404, detail="Coding room code not found")
    room = room_res["hits"]["hits"][0]["_source"]

    # Fetch all questions for this room
    qs_res = es.search(index="coding_questions_index", size=50, query={"term": {"room_code": room_code}})
    all_questions = [h["_source"] for h in qs_res["hits"]["hits"]]
    if not all_questions:
        raise HTTPException(status_code=422, detail="This room has no questions yet. Ask the recruiter to add questions first.")

    # Randomly pick num_questions (or all if fewer available)
    n = min(room.get("num_questions", 3), len(all_questions))
    selected = random.sample(all_questions, n)

    candidate_id = f"lc_cand_{uuid.uuid4().hex[:8]}"
    session_id   = f"lc_sess_{uuid.uuid4().hex[:10]}"

    session_doc = {
        "session_id":      session_id,
        "room_code":       room_code,
        "candidate_id":    candidate_id,
        "candidate_name":  body.name,
        "candidate_email": body.email,
        "question_ids":    [q["question_id"] for q in selected],
        "status":          "active",
        "language":        "python",
        "code_snapshots":  [],
        "emotion_timeline":[],
        "challenger_log":  [],
        "started_at":      datetime.now(timezone.utc).isoformat(),
    }
    es.index(index="live_coding_session_index", id=session_id, document=session_doc, refresh=True)

    return {
        "session_id":   session_id,
        "candidate_id": candidate_id,
        "room":         room,
        "questions":    selected,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Code Runner
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/code/run")
async def run_code(body: CodeRunRequest):
    """Execute candidate code in a subprocess sandbox (10-second timeout)."""
    start = time.time()
    lang = body.language.lower()

    if lang not in ("python", "javascript", "js"):
        raise HTTPException(status_code=400, detail="Supported languages: python, javascript")

    suffix   = ".py" if lang == "python" else ".js"
    cmd      = ["python3"] if lang == "python" else ["node"]

    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
        f.write(body.code)
        tmppath = f.name

    try:
        result = subprocess.run(
            cmd + [tmppath],
            capture_output=True,
            text=True,
            timeout=10,
            cwd="/tmp",
        )
        elapsed_ms = int((time.time() - start) * 1000)
        return {
            "stdout":      result.stdout[:8000],
            "stderr":      result.stderr[:2000],
            "returncode":  result.returncode,
            "runtime_ms":  elapsed_ms,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "⏱ Time limit exceeded (10 s)", "returncode": -1, "runtime_ms": 10000}
    except FileNotFoundError:
        return {"stdout": "", "stderr": f"Runtime not found: {cmd[0]} is not installed on this server.", "returncode": -1, "runtime_ms": 0}
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Emotion Logging
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/emotions")
async def save_emotion(body: EmotionSnapshot):
    """Save a single emotion snapshot from the frontend webcam analysis."""
    try:
        es.update(
            index="live_coding_session_index",
            id=body.session_id,
            script={
                "source": "if (ctx._source.emotion_timeline == null) { ctx._source.emotion_timeline = []; } ctx._source.emotion_timeline.add(params.snap)",
                "lang": "painless",
                "params": {"snap": {"ts": body.timestamp, "emotion": body.emotion, "confidence": body.confidence}},
            },
        )
    except Exception as e:
        log.debug("Emotion save error: %s", e)
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# Live Coding WebSocket — Challenger Agent
# ──────────────────────────────────────────────────────────────────────────────

CHALLENGER_AGENT_ID = os.getenv("CHALLENGER_AGENT_ID", "")

# Active live coding connections keyed by session_id
_lc_connections: dict[str, WebSocket] = {}


@app.websocket("/ws/challenger/{session_id}")
async def ws_challenger(websocket: WebSocket, session_id: str):
    """
    WebSocket relay for the Code Challenger agent.
    Messages from client:
      {"type": "code",   "code": "...", "language": "...", "question": "..."}
      {"type": "ask",    "message": "..."}
      {"type": "submit", "code": "...", "question_index": 0}
    Messages to client:
      {"type": "challenge", "message": "..."}
      {"type": "ack"}
    """
    await websocket.accept()
    _lc_connections[session_id] = websocket
    log.info("Challenger WS connected  session=%s", session_id)

    # Send welcome challenge
    await websocket.send_json({
        "type": "challenge",
        "message": (
            "Welcome to the Live Coding Challenge. I'll be watching your code as you work. "
            "I won't give you answers — but I will push you to think harder. Ready? Start coding."
        ),
    })

    async def _call_challenger(prompt: str) -> str:
        if CHALLENGER_AGENT_ID:
            try:
                return await _call_agent(CHALLENGER_AGENT_ID, prompt, timeout=30.0)
            except Exception as exc:
                log.warning("Challenger agent error (%s); using local fallback.", exc)
        # Local fallback is derived from the prompt context
        code = ""
        question = ""
        user_msg = ""
        if "CODE SUBMISSION" in prompt:
            # extract code block
            parts = prompt.split("```")
            if len(parts) >= 2:
                code = parts[1].strip()
        if "CANDIDATE QUESTION:" in prompt:
            user_msg = prompt.split("CANDIDATE QUESTION:")[-1].strip()
        if "QUESTION:" in prompt:
            question = prompt.split("QUESTION:")[-1].split("\n")[0].strip()
        return _local_challenger_response(code, question, user_msg)

    try:
        while True:
            raw = await websocket.receive_json()
            msg_type = raw.get("type", "")

            if msg_type == "code":
                # Candidate's code changed or they explicitly requested a review
                code     = raw.get("code", "").strip()
                language = raw.get("language", "python")
                question = raw.get("question", "")

                if not code:
                    continue

                prompt = (
                    f"You are a Code Challenger — an adversarial coach whose job is to push candidate thinking.\n\n"
                    f"QUESTION: {question}\n\n"
                    f"CODE SUBMISSION ({language}):\n```\n{code[:3000]}\n```\n\n"
                    f"Your rules:\n"
                    f"- NEVER provide the solution or working code\n"
                    f"- DO challenge: time complexity, space complexity, edge cases, correctness, style\n"
                    f"- DO ask probing questions that force deeper thinking\n"
                    f"- Keep your response to 2–3 sharp sentences only\n"
                    f"- Be direct and Socratic, not friendly\n"
                    f"- If the code is correct and efficient, find a new angle to challenge\n"
                )
                await websocket.send_json({"type": "typing"})
                message = await _call_challenger(prompt)
                message = message.strip()

                # Persist to challenger log
                try:
                    es.update(
                        index="live_coding_session_index", id=session_id,
                        script={
                            "source": "if (ctx._source.challenger_log == null) { ctx._source.challenger_log = []; } ctx._source.challenger_log.add(params.entry)",
                            "lang": "painless",
                            "params": {"entry": {"ts": datetime.now(timezone.utc).isoformat(), "role": "challenger", "message": message}},
                        },
                    )
                except Exception:
                    pass

                await websocket.send_json({"type": "challenge", "message": message})

            elif msg_type == "ask":
                # Candidate asks the challenger a direct question
                user_message = raw.get("message", "").strip()
                if not user_message:
                    continue
                question = raw.get("question", "")
                prompt = (
                    f"You are a Code Challenger — adversarial and Socratic, never giving away answers.\n\n"
                    f"QUESTION CONTEXT: {question}\n\n"
                    f"CANDIDATE QUESTION: {user_message}\n\n"
                    f"Rules:\n"
                    f"- Do NOT answer the question directly or give a solution\n"
                    f"- Redirect with a probing question or hint toward a concept, not the answer\n"
                    f"- 2 sentences max. Be challenging but fair.\n"
                )
                await websocket.send_json({"type": "typing"})
                message = await _call_challenger(prompt)

                # Persist candidate message too
                try:
                    for role, msg in [("candidate", user_message), ("challenger", message.strip())]:
                        es.update(
                            index="live_coding_session_index", id=session_id,
                            script={
                                "source": "if (ctx._source.challenger_log == null) { ctx._source.challenger_log = []; } ctx._source.challenger_log.add(params.entry)",
                                "lang": "painless",
                                "params": {"entry": {"ts": datetime.now(timezone.utc).isoformat(), "role": role, "message": msg}},
                            },
                        )
                except Exception:
                    pass

                await websocket.send_json({"type": "challenge", "message": message.strip()})

            elif msg_type == "submit":
                # Candidate submits their final code for a question
                code  = raw.get("code", "")
                q_idx = raw.get("question_index", 0)
                try:
                    es.update(
                        index="live_coding_session_index", id=session_id,
                        script={
                            "source": "if (ctx._source.code_snapshots == null) { ctx._source.code_snapshots = []; } ctx._source.code_snapshots.add(params.snap)",
                            "lang": "painless",
                            "params": {"snap": {"ts": datetime.now(timezone.utc).isoformat(), "question_index": q_idx, "code": code[:5000], "type": "final_submit"}},
                        },
                    )
                except Exception:
                    pass
                await websocket.send_json({
                    "type": "ack",
                    "message": "Code submitted. Moving on — don't get comfortable, the next question is waiting.",
                })

            elif msg_type == "complete":
                # Candidate finished all questions
                try:
                    es.update(
                        index="live_coding_session_index", id=session_id,
                        doc={"status": "completed", "completed_at": datetime.now(timezone.utc).isoformat()},
                    )
                except Exception:
                    pass
                await websocket.send_json({
                    "type": "complete",
                    "message": "Session complete. Your submissions have been recorded.",
                })

    except WebSocketDisconnect:
        _lc_connections.pop(session_id, None)
        log.info("Challenger WS disconnected  session=%s", session_id)

# ────────────────────────────────────────────────────────────────
# PDF / Text → structured questions parser
# ────────────────────────────────────────────────────────────────
class ParseQuestionsRequest(BaseModel):
    text: str
    room_code: str = ""

def _heuristic_parse(text: str) -> list[dict]:
    """Simple rule-based parser: split on numbered items or '---' dividers."""
    import re
    # Normalise line endings
    text = text.replace("\r\n", "\n").strip()

    # Try splitting on: 1. / Q1. / Question 1: / ### 1
    block_pattern = re.compile(
        r"(?:^|\n)\s*(?:Q(?:uestion)?\s*|#+ *)?(?P<num>\d+)[\.\):][ \t]*",
        re.MULTILINE | re.IGNORECASE,
    )
    splits = [m.start() for m in block_pattern.finditer(text)]

    # If fewer than 2 numbered blocks, fall back to '---' or double-newline splits
    if len(splits) < 2:
        blocks = [b.strip() for b in re.split(r"(?:-{3,}|={3,}|\n{2,})", text) if b.strip()]
    else:
        blocks = []
        for i, start in enumerate(splits):
            end = splits[i + 1] if i + 1 < len(splits) else len(text)
            chunk = text[start:end].strip()
            # Strip leading number
            chunk = re.sub(r"^[^\n]*?[\d]+[\.\):][ \t]*", "", chunk, count=1).strip()
            if chunk:
                blocks.append(chunk)

    questions = []
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        title = lines[0].strip()[:120]
        body  = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        # Difficulty heuristic
        diff_match = re.search(r"\b(easy|medium|hard)\b", block, re.IGNORECASE)
        difficulty  = diff_match.group(1).lower() if diff_match else "medium"
        # Tags heuristic
        tags = []
        for kw in ["array", "string", "hash", "tree", "graph", "dp", "dynamic programming",
                   "binary search", "sliding window", "stack", "queue", "linked list",
                   "recursion", "math", "sorting", "two pointers"]:
            if kw.lower() in block.lower():
                tags.append(kw.replace(" ", "-"))
        # Constraints / Examples
        constraints = ""
        ex_in = ex_out = ""
        cm = re.search(r"(?:Constraint[s]?:?)([\s\S]+?)(?:Example|Input|Output|$)", block, re.IGNORECASE)
        if cm:
            constraints = cm.group(1).strip()[:400]
        em = re.search(r"Input:?\s*(.+?)\nOutput:?\s*(.+?)(?:\n|$)", block, re.IGNORECASE)
        if em:
            ex_in, ex_out = em.group(1).strip(), em.group(2).strip()
        questions.append({
            "title": title,
            "description": body or title,
            "difficulty": difficulty,
            "tags": tags[:5],
            "constraints": constraints,
            "examples": [{"input": ex_in, "output": ex_out}] if ex_in or ex_out else [],
            "starter_code_python": "def solution():\n    pass\n",
            "starter_code_js": "function solution() {\n  \n}\n",
        })
    return questions


@app.post("/parse-questions-text")
async def parse_questions_text(body: ParseQuestionsRequest):
    """Parse raw text (from PDF or paste) into structured coding questions."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="No text provided.")

    # Fast heuristic parse runs immediately
    questions = _heuristic_parse(text)

    # Optional: try AI enhancement with a short timeout (non-blocking feel)
    if CHALLENGER_AGENT_ID and questions:
        try:
            prompt = (
                "You are a coding question extractor. The user pasted text from a PDF. "
                "Extract every distinct coding problem and return ONLY a valid JSON array — no markdown fences. "
                "Each element: title, description, difficulty (easy|medium|hard), tags (array, max 5), "
                "constraints (string), examples (array of {input,output}, max 2), "
                "starter_code_python, starter_code_js. Use sensible defaults for unknown fields.\n\n"
                f"TEXT:\n{text[:4000]}"
            )
            raw_ai = await asyncio.wait_for(_call_agent(CHALLENGER_AGENT_ID, prompt), timeout=12.0)
            raw_ai = raw_ai.strip()
            if raw_ai.startswith("```"):
                raw_ai = re.sub(r"^```[a-z]*\n?", "", raw_ai)
                raw_ai = re.sub(r"```$", "", raw_ai).strip()
            parsed = json.loads(raw_ai)
            if isinstance(parsed, list) and parsed:
                return {"questions": parsed, "source": "ai"}
        except Exception as exc:
            log.debug("AI question parsing skipped (%s); using heuristic result.", exc)

    if not questions:
        raise HTTPException(status_code=422, detail="Could not extract any questions from the provided text.")
    return {"questions": questions, "source": "heuristic"}

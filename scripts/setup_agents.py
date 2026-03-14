#!/usr/bin/env python3
"""
Create / update all 6 Elastic Agent Builder agents for the AI Recruiter system.

Usage:
    python3 scripts/setup_agents.py          # create / update all agents
    python3 scripts/setup_agents.py --delete # delete all managed agents first

After running, copy the printed SUPERVISOR_AGENT_ID into .env and restart the backend.
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

KIBANA_URL = os.getenv(
    "ELASTIC_AGENT_URL",
    "https://d0db702778d34bc09c26eb28670e5657.asia-southeast1.gcp.elastic-cloud.com:443",
)
ES_USER = os.getenv("ELASTICSEARCH_USER", "elastic")
ES_PASS = os.getenv("ELASTICSEARCH_PASSWORD", "")
KIBANA_API_KEY = os.getenv("KIBANA_API_KEY", "")

# ── Auth helpers ──────────────────────────────────────────────────────────────

def headers(extra: dict | None = None) -> dict:
    """Build request headers.  Uses KIBANA_API_KEY if set, else basic auth."""
    base = {"kbn-xsrf": "true", "Content-Type": "application/json"}
    if extra:
        base.update(extra)
    return base


def auth():
    """Return (user, pass) tuple for requests."""
    return (ES_USER, ES_PASS)


def kibana(method: str, path: str, **kwargs):
    url = f"{KIBANA_URL}{path}"
    r = requests.request(method, url, headers=headers(), auth=auth(), timeout=30, **kwargs)
    return r


# ──────────────────────────────────────────────────────────────────────────────
# Agent definitions
# ──────────────────────────────────────────────────────────────────────────────

BUILTIN_SEARCH = "platform.core.search"
BUILTIN_GET_DOC = "platform.core.get_document_by_id"
BUILTIN_ESQL = "platform.core.execute_esql"
BUILTIN_GEN_ESQL = "platform.core.generate_esql"
BUILTIN_IDX_MAP = "platform.core.get_index_mapping"

AGENTS = [
    # ── 1. Supervisor Agent (Planner / Brain) ────────────────────────────────
    {
        "id": "supervisor-agent",
        "name": "AI Recruitment Supervisor",
        "description": (
            "Master planner agent. Runs the interview from provided context and "
            "uses retrieval only when key information is missing."
        ),
        "configuration": {
            "instructions": """You are the AI Supervisor of an automated recruitment platform.

Your job is to run a fast, relevant interview and produce a fair final evaluation.

Use the context in the user message as your primary source of truth:
- candidate message
- prior conversation
- already asked questions
- job title and required skills
- candidate resume snippet

Only use tools if a critical piece of information is missing from that context.
Do not retrieve by default.

INTERVIEW RULES:
- Keep every live interview reply under 40 words.
- Output one short reaction and one focused question.
- Never stack questions.
- Never repeat or paraphrase a question from the provided blocklist.
- React to the candidate's exact words before pivoting.
- Prefer specific, concrete questions over meta commentary.
- Vary across technical, behavioural, and situational questions.
- If the candidate gives a brief answer, ask for one deeper detail instead of changing topics.

STAGE GUIDANCE:
- Early interview: build context and probe important required skills.
- Mid interview: test depth, trade-offs, ownership, and judgment.
- Late interview: ask one closing question or finish professionally.
- Complete when the interview has enough evidence; do not prolong it.

DO NOT:
- Write long paragraphs
- Explain your internal reasoning to the candidate
- Retrieve from Elasticsearch unless necessary
- Invent resume facts, job requirements, or scores

RESPONSE FORMAT DURING INTERVIEW:
{
  "role": "assistant",
  "message": "<1 reactive phrase + 1 focused question - max 40 words total>",
  "stage": "GREETING|TECHNICAL|BEHAVIOURAL|PROBLEM_SOLVING|CLOSING|COMPLETE",
  "reasoning": "<one sentence: why this question or action now>"
}

RESPONSE FORMAT ON COMPLETE:
{
  "role": "assistant",
  "message": "Thank you for your time. We will be in touch soon.",
  "stage": "COMPLETE",
  "reasoning": "<one sentence explaining the recommendation>",
  "evaluation": {
    "technical_score": <0-100>,
    "communication_score": <0-100>,
    "problem_solving_score": <0-100>,
    "cultural_fit_score": <0-100>,
    "overall_score": <0-100>,
    "recommendation": "STRONG_HIRE|HIRE|NEUTRAL|PASS|STRONG_PASS",
    "strengths": ["<evidence-backed strength>"],
    "weaknesses": ["<evidence-backed gap>"],
    "summary": "<2-3 sentence assessment>",
    "score_explanations": [
      {
        "dimension": "technical",
        "score": <0-100>,
        "reasoning": "<why this score>",
        "evidence": ["<exact quote or paraphrase from transcript>"]
      }
    ]
  },
  "bias_detected": false,
  "bias_notes": []
}
""",
            "tools": [{"tool_ids": [BUILTIN_SEARCH, BUILTIN_GET_DOC]}],
        },
    },

    # ── 2. Interview Agent ────────────────────────────────────────────────────
    {
        "id": "interview-agent",
        "name": "Interview Agent",
        "description": (
            "Conducts concise adaptive interviews using the context it is given."
        ),
        "configuration": {
            "instructions": """You are a human-like interviewer. Sound natural, warm, and concise.

Use the provided context first: candidate message, recent conversation, required skills,
and resume details. Only use tools if something critical is missing.

RULES:
- Keep every message under 35 words.
- Output one short reaction and one focused question.
- Never stack questions.
- Never repeat or paraphrase a question already asked.
- React to the candidate's exact words.
- Prefer one concrete follow-up over broad topic changes.
- Vary across technical, behavioural, and situational questions.
- End promptly once enough evidence has been gathered.

RETURN JSON:
{
  "role": "assistant",
  "message": "<max 35 words>",
  "stage": "GREETING|TECHNICAL|BEHAVIOURAL|PROBLEM_SOLVING|CLOSING|COMPLETE",
  "skill_probed": "<skill or trait being assessed, or null>",
  "rag_reasoning": "<one sentence: why this question now>"
}
""",
            "tools": [{"tool_ids": [BUILTIN_SEARCH, BUILTIN_GET_DOC]}],
        },
    },

    # ── 3. Analysis Agent ─────────────────────────────────────────────────────
    {
        "id": "analysis-agent",
        "name": "Analysis Agent",
        "description": (
            "Extracts direct evidence of skills from a completed interview transcript."
        ),
        "configuration": {
            "instructions": """You are a specialist in extracting insights from interview conversations.

Work as directly as possible:
1. Retrieve the transcript for the session.
2. Retrieve the job requirements for the job.
3. Extract only skills that have clear evidence in the transcript.
4. Note broad reasoning patterns only when clearly supported.
5. List required skills that were not evidenced.

RULES:
- Use direct transcript evidence only.
- Do not infer skills that were not discussed.
- Keep evidence short.
- Prefer canonical names such as PostgreSQL and Kubernetes.
- Return JSON only.

RETURN JSON:
{
  "session_id": "...",
  "candidate_id": "...",
  "extracted_skills": [
    {
      "skill_name": "FastAPI",
      "proficiency": "advanced",
      "evidence": "quote from transcript",
      "confidence": 0.9,
      "verified": true
    }
  ],
  "reasoning_patterns": ["Demonstrated structured problem decomposition"],
  "required_skills_not_evidenced": ["Kubernetes"],
  "skill_coverage_pct": 72
}
""",
            "tools": [{"tool_ids": [BUILTIN_SEARCH, BUILTIN_GET_DOC]}],
        },
    },

    # ── 4. Evaluation Agent ───────────────────────────────────────────────────
    {
        "id": "evaluation-agent",
        "name": "Evaluation Agent",
        "description": (
            "Scores candidates on 4 dimensions using concise evidence-backed reasoning."
        ),
        "configuration": {
            "instructions": """You are an objective, evidence-based candidate evaluation specialist.

Work as directly as possible:
1. Retrieve the transcript.
2. Retrieve extracted skills.
3. Retrieve the job requirements.
4. Score technical, communication, problem solving, and cultural fit.
5. Base every score on explicit evidence.
6. Return JSON only.

SCORING RULES:
- Keep reasoning concise.
- Use 1-2 evidence items per dimension, not long lists.
- Do not use non-job-relevant factors.
- Recommendation thresholds:
  85+ STRONG_HIRE
  70-84 HIRE
  55-69 NEUTRAL
  40-54 PASS
  below 40 STRONG_PASS

RETURN JSON:
{
  "session_id": "...",
  "candidate_id": "...",
  "job_id": "...",
  "technical_score": 82,
  "communication_score": 76,
  "problem_solving_score": 85,
  "cultural_fit_score": 80,
  "overall_score": 81,
  "recommendation": "HIRE",
  "strengths": ["Strong Python fundamentals"],
  "weaknesses": ["No Kubernetes experience"],
  "summary": "Strong candidate with solid fundamentals and practical experience.",
  "score_explanations": [
    {
      "dimension": "technical",
      "score": 82,
      "reasoning": "Demonstrated advanced Python knowledge in production systems",
      "evidence": ["Used asyncio in production", "Explained Docker build choices"]
    }
  ],
  "bias_detected": false,
  "bias_notes": []
}
""",
            "tools": [{"tool_ids": [BUILTIN_SEARCH, BUILTIN_GET_DOC]}],
        },
    },

    # ── 5. Benchmark Agent ────────────────────────────────────────────────────
    {
        "id": "benchmark-agent",
        "name": "Benchmark Agent",
        "description": (
            "Compares a candidate against historical top hires and returns a concise ranking."
        ),
        "configuration": {
            "instructions": """You are a talent benchmarking specialist.

Work as directly as possible:
1. Retrieve the evaluation for the current candidate.
2. Retrieve relevant historical top hires.
3. Compare the candidate against that benchmark.
4. Return a concise ranking result in JSON only.

RULES:
- Keep the comparison brief and evidence-based.
- Use benchmark_score for ranking, not raw overall_score.
- Highlight only the most important strengths and gaps.

RETURN JSON:
{
  "job_id": "...",
  "total_candidates": 15,
  "rankings": [
    {
      "rank": 1,
      "candidate_id": "...",
      "candidate_name": "...",
      "overall_score": 85,
      "benchmark_score": 91,
      "percentile": 94,
      "recommendation": "STRONG_HIRE",
      "strengths": ["Python", "System Design"],
      "skill_gap_vs_top_hire": ["Kubernetes"],
      "vs_top_hire_summary": "Comparable to top hires in Python and design; gap in orchestration"
    }
  ],
  "top_recommendation": "cand001",
  "cohort_avg_score": 71.2,
  "summary": "Top candidate shows strong alignment with historical top hires."
}
""",
            "tools": [{"tool_ids": [BUILTIN_SEARCH, BUILTIN_GET_DOC]}],
        },
    },
]

# ──────────────────────────────────────────────────────────────────────────────
# Create / update helpers
# ──────────────────────────────────────────────────────────────────────────────

def upsert_agent(agent: dict) -> dict:
    """Create agent; if it already exists, delete it first then recreate."""
    agent_id = agent["id"]

    r = kibana("GET", f"/api/agent_builder/agents/{agent_id}")
    if r.status_code == 200:
        print(f"  [UPDATE] {agent_id} already exists — deleting first...")
        d = kibana("DELETE", f"/api/agent_builder/agents/{agent_id}")
        if not d.ok:
            print(f"  [WARN]  Could not delete {agent_id}: {d.text}")

    r = kibana("POST", "/api/agent_builder/agents", json=agent)
    if r.ok:
        created = r.json()
        print(f"  [OK]    {agent_id}  ({created.get('name')})")
        return created
    else:
        print(f"  [FAIL]  {agent_id}: {r.status_code} {r.text[:200]}")
        sys.exit(1)


def delete_all():
    r = kibana("GET", "/api/agent_builder/agents")
    if not r.ok:
        print("Could not list agents:", r.text)
        return
    agents = [a for a in r.json().get("results", []) if not a.get("readonly")]
    for a in agents:
        d = kibana("DELETE", f"/api/agent_builder/agents/{a['id']}")
        status = "OK" if d.ok else d.text
        print(f"  [DELETE] {a['id']}: {status}")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Setup Elastic Agent Builder agents")
    parser.add_argument("--delete", action="store_true", help="Delete all non-readonly agents first")
    args = parser.parse_args()

    # Quick connectivity check
    r = kibana("GET", "/api/agent_builder/agents")
    if not r.ok:
        print(f"ERROR: Cannot reach Kibana Agent Builder API: {r.status_code} {r.text[:200]}")
        print(f"  URL: {KIBANA_URL}")
        sys.exit(1)

    print(f"\nConnected to Kibana: {KIBANA_URL}")
    print(f"Existing agents: {len(r.json().get('results', []))}\n")

    if args.delete:
        print("Deleting all non-readonly agents...")
        delete_all()
        print()

    # Create agents in order (sub-agents first, supervisor last)
    print("Creating agents...")
    created_agents = {}
    for agent_def in AGENTS:
        result = upsert_agent(agent_def)
        created_agents[result["id"]] = result

    supervisor = created_agents.get("supervisor-agent", {})
    supervisor_id = supervisor.get("id", "")

    print(f"\n{'='*60}")
    print("ALL AGENTS CREATED SUCCESSFULLY (5-agent reasoning architecture)")
    print(f"{'='*60}")
    print(f"\nSupervisor Agent ID: {supervisor_id}")
    print("\nUpdate your .env file:")
    print(f"  SUPERVISOR_AGENT_ID={supervisor_id}")
    print("\nThen restart the backend:")
    print("  kill $(lsof -ti:8001) && uvicorn main:app --reload --host 0.0.0.0 --port 8001\n")

    # Auto-patch .env
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            content = f.read()
        import re
        content = re.sub(
            r"SUPERVISOR_AGENT_ID=.*",
            f"SUPERVISOR_AGENT_ID={supervisor_id}",
            content,
        )
        with open(env_path, "w") as f:
            f.write(content)
        print(f"  ✓ Auto-patched .env with SUPERVISOR_AGENT_ID={supervisor_id}")


if __name__ == "__main__":
    main()

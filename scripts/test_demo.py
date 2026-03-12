#!/usr/bin/env python3
"""
End-to-end demo test — agent-only pipeline, no hardcodes.

Flow:
  1. POST /rooms           → create interview room (agent extracts skills)
  2. POST /candidates      → register candidate
  3. WebSocket             → 10 realistic interview turns
  4. Wait for COMPLETE stage
  5. Poll GET /api/evaluate/{session_id} → print full evaluation
  6. Check benchmark_results_index → print percentile ranking
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import websockets
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, NotFoundError

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8001")
WS_BASE  = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")

ES_URL   = os.getenv("ELASTICSEARCH_URL")
ES_USER  = os.getenv("ELASTICSEARCH_USER", "elastic")
ES_PASS  = os.getenv("ELASTICSEARCH_PASSWORD")

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
GREY   = "\033[90m"
MAGENTA = "\033[95m"

# ── 10 realistic turns for a Senior Python Engineer role ─────────────────────
CANDIDATE_TURNS = [
    # Turn 1 — introduction
    "Hi, I'm Jordan Lee. I have 7 years of professional Python experience across "
    "fintech and SaaS companies. I'm excited to discuss the Senior Python Engineer role.",

    # Turn 2 — backend depth
    "I've built high-throughput event-driven microservices using FastAPI and asyncio, "
    "processing roughly 80,000 requests per second at peak. I use SQLAlchemy with "
    "async drivers for PostgreSQL, and I've tuned queries with EXPLAIN ANALYZE and "
    "partial indexes to keep p99 latency under 10ms.",

    # Turn 3 — distributed systems
    "For distributed systems I lean on the outbox pattern with Kafka for reliable "
    "event publishing. I've designed idempotent consumers so we never double-process "
    "a payment event, using Redis sorted sets for deduplication windows. "
    "Handling at-least-once delivery was a key constraint in that system.",

    # Turn 4 — infrastructure / DevOps
    "In my last role I owned the Kubernetes migration from bare-metal. I wrote Helm "
    "charts for 12 services, set up horizontal pod autoscaling tied to Kafka consumer "
    "lag metrics, and ran zero-downtime deployments with a blue-green approach. "
    "CI/CD runs on GitHub Actions using Docker BuildKit caching to keep build times "
    "under 3 minutes.",

    # Turn 5 — debugging a hard problem
    "One memorable debugging session: a memory leak in a long-lived FastAPI process. "
    "I used tracemalloc and objgraph to identify that a third-party client library "
    "was caching SSL sessions indefinitely. I patched it upstream and added a "
    "pytest fixture that fails the build if RSS growth exceeds 50 MB over 10k requests.",

    # Turn 6 — AWS experience
    "On AWS I'm comfortable with EKS, RDS Aurora PostgreSQL, ElastiCache, SQS, "
    "and Lambda for event fan-out. I use Terraform for all infrastructure — checked "
    "into git, reviewed like code. I've implemented least-privilege IAM with "
    "instance profiles and never hardcode credentials.",

    # Turn 7 — code quality and leadership
    "I've led a team of four engineers for two years. I introduced ADR documents "
    "for architectural decisions, set up a weekly tech-debt rotation, and run "
    "structured code reviews focused on readability and test coverage over style "
    "preferences. Test coverage went from 40% to 90% in six months.",

    # Turn 8 — handling tight deadlines / trade-offs
    "When we had a hard launch deadline for a compliance feature, I suggested cutting "
    "the real-time analytics to a daily batch job initially. We shipped on time, "
    "documented the tech debt as a P1 ticket, and delivered the streaming version "
    "in the next sprint. I prefer explicit trade-off logs over silent shortcuts.",

    # Turn 9 — what they want next
    "I'm looking for a role where I can solve genuine scale challenges and mentor "
    "junior engineers. I'm drawn to this position because the platform description "
    "mentions millions of daily events — that's the kind of problem I enjoy owning "
    "end-to-end, from schema design to on-call runbooks.",

    # Turn 10 — closing question
    "What does the on-call rotation look like, and how does the team handle "
    "post-incident reviews? Learning from failures in a blameless culture is "
    "something I consider a green flag when evaluating a new company.",
]


def hr(char: str = "─", width: int = 62) -> str:
    return char * width


def print_turn(role: str, text: str, extras: dict):
    if role == "candidate":
        print(f"\n{CYAN}{BOLD}► CANDIDATE:{RESET} {text}")
    elif role in ("assistant", "agent"):
        stage = extras.get("stage", "")
        tag   = f" [{YELLOW}{stage}{RESET}]" if stage else ""
        print(f"\n{GREEN}{BOLD}◆ AGENT{tag}:{RESET} {text[:600]}")
        if len(text) > 600:
            print(f"  {GREY}… (truncated, {len(text)} chars total){RESET}")
        if extras.get("evaluation"):
            ev = extras["evaluation"]
            print(f"\n  {BOLD}── INLINE EVALUATION ──{RESET}")
            for k, v in ev.items():
                print(f"    {MAGENTA}{k}{RESET}: {v}")
    elif role == "system":
        print(f"\n{RED}✗ SYSTEM:{RESET} {extras.get('error', text)}")
    else:
        print(f"\n{GREY}[{role}] {text[:300]}{RESET}")


def print_evaluation(ev: dict):
    print(f"\n{BOLD}{hr()}")
    print(f"  EVALUATION RESULT  (source: {ev.get('source','?')})")
    print(hr())
    scores = [
        ("Technical",       ev.get("technical_score")),
        ("Communication",   ev.get("communication_score")),
        ("Problem Solving", ev.get("problem_solving_score")),
        ("Cultural Fit",    ev.get("cultural_fit_score")),
    ]
    for label, val in scores:
        if val is not None:
            bar = "█" * int(float(val) / 5)
            print(f"  {label:<18} {CYAN}{bar:<20}{RESET} {val}")
    overall = ev.get("overall_score") or ev.get("total_score")
    rec = ev.get("recommendation", "N/A")
    rec_col = GREEN if "HIRE" in str(rec) else (YELLOW if rec == "NEUTRAL" else RED)
    print(f"\n  {BOLD}Overall Score  : {overall}{RESET}")
    print(f"  {BOLD}Recommendation : {rec_col}{rec}{RESET}")
    if ev.get("summary"):
        print(f"\n  Summary: {ev['summary']}")
    if ev.get("strengths"):
        print(f"\n  {GREEN}Strengths:{RESET}")
        for s in (ev["strengths"] if isinstance(ev["strengths"], list) else [ev["strengths"]]):
            print(f"    + {s}")
    if ev.get("weaknesses"):
        print(f"\n  {YELLOW}Gaps:{RESET}")
        for w in (ev["weaknesses"] if isinstance(ev["weaknesses"], list) else [ev["weaknesses"]]):
            print(f"    - {w}")
    print(hr() + RESET)


def print_benchmark(bm: dict):
    print(f"\n{BOLD}{hr()}")
    print("  BENCHMARK RESULT")
    print(hr())
    print(f"  Percentile       : {bm.get('percentile', 'N/A')}")
    print(f"  Ranking          : {bm.get('ranking', 'N/A')}")
    print(f"  Benchmark Score  : {bm.get('benchmark_score', 'N/A')}")
    if bm.get("comparison_notes"):
        print(f"  Notes            : {bm['comparison_notes']}")
    print(hr() + RESET)


async def run_demo():
    print(f"\n{BOLD}{hr('═')}")
    print("  AI INTERVIEWER — FULL AGENT PIPELINE END-TO-END DEMO")
    print(hr('═') + RESET)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as http:

        # ── Step 1: Health check ───────────────────────────────────────────
        print(f"\n{BOLD}[1/6] Health check{RESET}")
        try:
            r = await http.get("/health")
            r.raise_for_status()
            print(f"  {GREEN}✓ App is running{RESET}  {r.json()}")
        except Exception as exc:
            print(f"  {RED}✗ App not reachable at {BASE_URL} — start with: uvicorn main:app --port 8001{RESET}")
            sys.exit(1)

        # ── Step 2: Create room ────────────────────────────────────────────
        print(f"\n{BOLD}[2/6] Create interview room (agent extracts skills){RESET}")
        t0 = time.time()
        r = await http.post("/rooms", json={
            "title":       "Senior Python Engineer",
            "description": (
                "We are hiring a Senior Python Engineer to design and own backend "
                "services processing millions of events per day. You will work with "
                "FastAPI, asyncio, PostgreSQL, Kafka, Kubernetes, and AWS. Strong "
                "knowledge of distributed systems, observability, CI/CD pipelines, "
                "and mentoring junior engineers is essential. The role requires "
                "7+ years of Python, experience with Docker and Terraform, and "
                "expertise in performance tuning, on-call ownership, and code review."
            ),
            "company":     "Demo Corp",
            "location":    "Remote",
            "job_type":    "full_time",
        })
        if r.status_code not in (200, 201):
            print(f"  {RED}✗ Room creation failed: {r.status_code} {r.text[:300]}{RESET}")
            sys.exit(1)
        room = r.json()
        room_code = room.get("room_code") or room.get("code")
        job_id    = room.get("job_id")
        elapsed   = time.time() - t0
        print(f"  {GREEN}✓ Room created{RESET}  room_code={room_code}  job_id={job_id}  ({elapsed:.1f}s)")
        print(f"  Skills extracted: {room.get('required_skills', '(see ES)')}")

        # ── Step 3: Register candidate ─────────────────────────────────────
        print(f"\n{BOLD}[3/6] Register candidate{RESET}")
        r = await http.post("/candidates/register", json={
            "name":     "Jordan Lee",
            "email":    "jordan.lee@demo.example",
            "job_id":   job_id,
            "room_code": room_code,
        })
        if r.status_code >= 400:
            print(f"  {RED}✗ Candidate registration failed: {r.status_code} {r.text[:300]}{RESET}")
            sys.exit(1)
        cand = r.json()
        candidate_id = cand.get("candidate_id") or cand.get("id")
        session_id   = cand.get("session_id")
        print(f"  {GREEN}✓ Candidate registered{RESET}  candidate_id={candidate_id}")
        print(f"  session_id={session_id}")

        # ── Step 4: WebSocket interview ────────────────────────────────────
        print(f"\n{BOLD}[4/6] Interview via WebSocket ({len(CANDIDATE_TURNS)} turns){RESET}")
        ws_url = f"{WS_BASE}/ws/interview/{session_id}/{job_id}/{candidate_id}"
        print(f"  Connecting to: {ws_url}")

        completed = False
        inline_eval = None
        try:
            async with websockets.connect(ws_url, open_timeout=15) as ws:
                print(f"  {GREEN}✓ Connected{RESET}")

                # Receive greeting
                print(f"  {GREY}Waiting for greeting (up to 120s)…{RESET}")
                t0 = time.time()
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
                msg = json.loads(raw)
                print(f"  {GREY}({time.time()-t0:.1f}s){RESET}")
                print_turn(msg.get("role","assistant"), msg.get("message",""), msg)

                for i, turn_text in enumerate(CANDIDATE_TURNS, 1):
                    print_turn("candidate", turn_text, {})
                    await ws.send(turn_text)
                    print(f"  {GREY}  ↳ awaiting agent response…{RESET}")
                    t0 = time.time()
                    raw = await asyncio.wait_for(ws.recv(), timeout=120)
                    msg = json.loads(raw)
                    print(f"  {GREY}  ({time.time()-t0:.1f}s){RESET}")
                    print_turn(msg.get("role","assistant"), msg.get("message",""), msg)
                    if msg.get("evaluation"):
                        inline_eval = msg["evaluation"]
                    if msg.get("stage") == "COMPLETE":
                        print(f"\n  {GREEN}{BOLD}✓ Interview COMPLETE after {i} turns{RESET}")
                        completed = True
                        break

                if not completed:
                    print(f"\n  {YELLOW}⚠  All {len(CANDIDATE_TURNS)} turns sent — session may still be running{RESET}")

        except asyncio.TimeoutError:
            print(f"\n{RED}✗ Timeout waiting for agent — check server logs{RESET}")
            sys.exit(1)
        except Exception as exc:
            print(f"\n{RED}✗ WebSocket error: {exc}{RESET}")
            sys.exit(1)

        # ── Step 5: Poll evaluation pipeline ──────────────────────────────
        print(f"\n{BOLD}[5/6] Polling post-interview pipeline{RESET}")
        print(f"  Waiting for Analysis → Evaluation → Benchmark agents…")
        eval_doc = None
        for attempt in range(1, 25):
            await asyncio.sleep(6)
            try:
                r = await http.get(f"/api/evaluate/{session_id}")
                data = r.json()
                if data.get("status") == "complete":
                    eval_doc = data["evaluation"]
                    print(f"  {GREEN}✓ Evaluation stored  (attempt {attempt}){RESET}")
                    break
                print(f"  {GREY}  attempt {attempt}: status={data.get('status')} stage={data.get('session_stage','?')}{RESET}")
            except Exception as exc:
                print(f"  {GREY}  attempt {attempt}: {exc}{RESET}")
        else:
            print(f"  {YELLOW}⚠  Evaluation not ready after 2.5 min — check agent logs{RESET}")

        if eval_doc:
            print_evaluation(eval_doc)
        elif inline_eval:
            print(f"\n  {YELLOW}Showing inline evaluation captured during session:{RESET}")
            print_evaluation(inline_eval)

        # ── Step 6: Check benchmark result ────────────────────────────────
        print(f"\n{BOLD}[6/6] Benchmark result{RESET}")
        es = Elasticsearch(ES_URL, basic_auth=(ES_USER, ES_PASS))
        try:
            bm_id = f"{job_id}_{session_id}"
            bm = es.get(index="benchmark_results_index", id=bm_id)["_source"]
            print_benchmark(bm)
        except NotFoundError:
            print(f"  {YELLOW}⚠  Benchmark not yet stored — benchmark agent may still be running{RESET}")
        except Exception as exc:
            print(f"  {RED}✗ ES error: {exc}{RESET}")

        # Transcript summary
        print(f"\n{BOLD}Transcript summary:{RESET}")
        try:
            tx = es.search(
                index="transcript_index",
                query={"term": {"session_id": session_id}},
                size=50,
                sort=[{"timestamp": {"order": "asc"}}],
            )
            turns_stored = tx["hits"]["total"]["value"]
            print(f"  {GREEN}✓ {turns_stored} transcript turns in Elasticsearch{RESET}")
        except Exception as exc:
            print(f"  {RED}✗ {exc}{RESET}")

    print(f"\n{BOLD}{hr('═')}")
    print(f"  DEMO COMPLETE")
    print(hr('═') + RESET)
    print(f"\n  Candidate portal : {BASE_URL}")
    print(f"  Recruiter portal : {BASE_URL}/recruiter")
    print(f"  Results page     : {BASE_URL}/results.html")
    print()


if __name__ == "__main__":
    asyncio.run(run_demo())


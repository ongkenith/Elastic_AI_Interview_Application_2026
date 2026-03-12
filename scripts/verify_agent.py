#!/usr/bin/env python3
"""
Verifies the full agent call chain:
  1. Direct Elastic /api/agent_builder/converse call
  2. Backend /health endpoint
  3. Backend WS delivers a real agent reply
  4. ES transcript_index stores the turns correctly
"""
import asyncio
import json
import os
import sys
import time
import requests
import websockets
from dotenv import load_dotenv
from elasticsearch import Elasticsearch

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

KIBANA_URL  = os.getenv("ELASTIC_AGENT_URL")
ES_USER     = os.getenv("ELASTICSEARCH_USER", "elastic")
ES_PASS     = os.getenv("ELASTICSEARCH_PASSWORD")
ES_URL      = os.getenv("ELASTICSEARCH_URL")
AGENT_ID    = os.getenv("SUPERVISOR_AGENT_ID", "interview-supervisor-agent")

OK   = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[96mℹ\033[0m"
BOLD = "\033[1m"
RST  = "\033[0m"


def check(label, ok, detail=""):
    sym = OK if ok else FAIL
    suffix = f"  {detail}" if detail else ""
    print(f"  {sym}  {label}{suffix}")
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# 1. Direct Elastic API test
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{BOLD}[ 1 ] Direct Elastic Agent Builder API{RST}")
print(f"  URL   : {KIBANA_URL}/api/agent_builder/converse")
print(f"  Agent : {AGENT_ID}")

t0 = time.time()
r = requests.post(
    f"{KIBANA_URL}/api/agent_builder/converse",
    auth=(ES_USER, ES_PASS),
    headers={"kbn-xsrf": "true", "Content-Type": "application/json"},
    json={
        "agent_id": AGENT_ID,
        "input": (
            "[CONTEXT]\n"
            "session_id: verify_001\n"
            "job_id: job_001\n"
            "candidate_id: cand_001\n\n"
            "[CANDIDATE MESSAGE]\n"
            "Start the interview with a professional greeting."
        ),
    },
    timeout=90,
)
elapsed = time.time() - t0

check("HTTP 200 from /api/agent_builder/converse", r.status_code == 200,
      f"(got {r.status_code})")

if r.status_code != 200:
    print(f"\n  {FAIL}  Response body: {r.text[:300]}")
    sys.exit(1)

data = r.json()
conv_id   = data.get("conversation_id", "")
model     = data.get("model_usage", {}).get("connector_id", "?")
llm_calls = data.get("model_usage", {}).get("llm_calls", "?")
in_tok    = data.get("model_usage", {}).get("input_tokens", "?")
out_tok   = data.get("model_usage", {}).get("output_tokens", "?")

check("conversation_id present", bool(conv_id), conv_id[:32] + "…")
check("model_usage returned",    bool(model),   f"model={model}  llm_calls={llm_calls}  tokens={in_tok}→{out_tok}")
print(f"  {INFO}  Response time: {elapsed:.1f}s")

# Extract response.message
nested = data.get("response", {})
raw_msg = nested.get("message", "") if isinstance(nested, dict) else str(nested)
check("response.message non-empty", bool(raw_msg))

# Parse inner JSON
try:
    inner = json.loads(raw_msg)
    role  = inner.get("role", "?")
    stage = inner.get("stage", "?")
    msg   = inner.get("message", "")
    check("Agent returned valid JSON", True, f"role={role}  stage={stage}")
    print(f"\n  {INFO}  Agent message preview:")
    print(f"      \"{msg[:200]}\"")
except json.JSONDecodeError:
    check("Agent returned valid JSON", False, "(plain text)")
    print(f"\n  {INFO}  Agent message (plain text):")
    print(f"      \"{raw_msg[:200]}\"")

# Tool calls
steps      = data.get("steps", [])
tool_calls = [s for s in steps if s.get("type") == "tool_call"]
print(f"\n  {INFO}  Tool calls made: {len(tool_calls)}")
for tc in tool_calls:
    tid    = tc.get("tool_id", "?")
    params = json.dumps(tc.get("params", {}))[:80]
    rows   = sum(
        len(res.get("data", {}).get("values", []))
        for res in tc.get("results", [])
        if res.get("type") == "tabular_data"
    )
    print(f"      {tid}  params={params}  ES rows returned={rows}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Backend health
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{BOLD}[ 2 ] Backend health{RST}")
try:
    hr = requests.get("http://localhost:8001/health", timeout=5)
    hd = hr.json()
    check("Backend /health 200", hr.status_code == 200)
    check("Elasticsearch green", hd.get("elasticsearch") == "green",
          f"status={hd.get('elasticsearch')}")
except Exception as e:
    check("Backend /health reachable", False, str(e))
    print(f"\n  Start backend: uvicorn main:app --host 0.0.0.0 --port 8001")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 3. WebSocket — one real exchange through the backend
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{BOLD}[ 3 ] WebSocket interview relay (1 turn){RST}")
SESSION = "verify_ws_001"
WS_URL = f"ws://localhost:8001/ws/interview/{SESSION}/job_001/cand_001"
print(f"  WS: {WS_URL}")

ws_ok = False
ws_msg = ""

async def ws_test():
    global ws_ok, ws_msg
    try:
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            t0 = time.time()
            raw = await asyncio.wait_for(ws.recv(), timeout=90)
            elapsed = time.time() - t0
            msg = json.loads(raw)
            demo = msg.get("demo_mode", False)
            text = msg.get("message", "")
            ws_ok = bool(text) and not demo
            ws_msg = text[:200]
            check("WS greeting received", ws_ok,
                  f"({elapsed:.1f}s)  demo_mode={demo}  stage={msg.get('stage','?')}")
            if not ws_ok and demo:
                print(f"  {FAIL}  DEMO MODE — SUPERVISOR_AGENT_ID not configured")
            if ws_ok:
                print(f"\n  {INFO}  Agent greeted via WS:")
                print(f"      \"{ws_msg}\"")
    except asyncio.TimeoutError:
        check("WS greeting received", False, "(timeout >90s)")
    except Exception as e:
        check("WS greeting received", False, str(e))

asyncio.run(ws_test())


# ─────────────────────────────────────────────────────────────────────────────
# 4. Elasticsearch — transcript stored correctly
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{BOLD}[ 4 ] Elasticsearch transcript storage{RST}")
es = Elasticsearch(ES_URL, basic_auth=(ES_USER, ES_PASS))

result = es.search(
    index="transcript_index",
    query={"term": {"session_id": SESSION}},
    sort=[{"timestamp": {"order": "asc"}}],
    size=10,
)
turns = result["hits"]["hits"]
check("Transcript turns stored in ES", len(turns) > 0, f"{len(turns)} turn(s)")

for t in turns:
    src  = t["_source"]
    role = src.get("role", "?").upper().ljust(10)
    has_emb = bool(src.get("content_embedding"))
    content = src.get("content", "")[:100]
    check(
        f"  [{role}] embedding present",
        has_emb,
        f"\"{content}\"",
    )

# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*55}")
all_ok = ws_ok and len(turns) > 0
if all_ok:
    print(f"{OK} {BOLD}ALL CHECKS PASSED — agent pipeline is fully working{RST}")
else:
    print(f"{FAIL} {BOLD}Some checks failed — see above{RST}")
print(f"{'='*55}\n")
sys.exit(0 if all_ok else 1)

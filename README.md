# 🐴 AI Interview System

An end-to-end AI-powered interview platform.  
Recruiters create a room, share a code, and the AI agent conducts, scores, and ranks every candidate automatically.

**Stack:** FastAPI · Elasticsearch · Elastic Agent Builder · Node.js · Chart.js

---

## What You Need Before Starting

Install these tools if you don't have them already:

| Tool | Version | Download |
|------|---------|----------|
| Python | 3.10+ | https://www.python.org/downloads/ |
| Node.js | 18+ | https://nodejs.org/ |
| Docker Desktop | latest | https://www.docker.com/products/docker-desktop/ (only needed for local Elasticsearch) |

You also need an **Elasticsearch instance** — either:
- **Option A (recommended):** Elastic Cloud — sign up free at https://cloud.elastic.co — no Docker required
- **Option B:** Run Elasticsearch locally with Docker (requires Docker Desktop to be running)

---

## Running the App

There are two paths depending on how you run Elasticsearch. **Pick one.**

---

### Option A — Elastic Cloud (no Docker needed)

> Use this if you have an Elastic Cloud account or want the easiest setup.

**Step 1 — Clone and go into the project folder**

```bash
git clone <repo-url>
cd Elastic_AI_Interview_Application
```

**Step 2 — Set up your environment file**

```bash
cp .env.example .env
```

Open `.env` and fill in your Elastic Cloud details:

```env
ELASTICSEARCH_URL=https://<your-deployment>.elastic-cloud.com:443
ELASTICSEARCH_USER=elastic
ELASTICSEARCH_PASSWORD=<your-password>

# After creating agents in Kibana (see "Elastic Agents Setup" below):
ELASTIC_AGENT_URL=https://<your-deployment>.elastic-cloud.com
ELASTIC_API_KEY=<your-api-key>
SUPERVISOR_AGENT_ID=<your-supervisor-agent-id>
```

> **Where to find these values:** Go to your Elastic Cloud console → your deployment → "Manage" → "Copy endpoint" for the URL, and "Reset password" for the password.

**Step 3 — Set up Python**

```bash
python -m venv venv

# Mac / Linux:
source venv/bin/activate

# Windows:
venv\Scripts\activate

pip install -r requirements.txt
```

**Step 3.5 â€” Set up offline interviewer voice (Piper)**

> `requirements.txt` installs the Python packages only. The current interviewer voice also requires the Piper runtime and model files on disk.

If these files already exist, skip this step:

- [`tools/piper/piper/piper.exe`](tools/piper/piper/piper.exe)
- [`models/piper/en_US-hfc_female-medium.onnx`](models/piper/en_US-hfc_female-medium.onnx)
- [`models/piper/en_US-hfc_female-medium.onnx.json`](models/piper/en_US-hfc_female-medium.onnx.json)

Optional environment overrides:

```env
PIPER_EXE=tools/piper/piper/piper.exe
PIPER_MODEL=models/piper/en_US-hfc_female-medium.onnx
```

**Step 4 — Create indices and seed data**

> Make sure your venv is still active (you should see `(venv)` in your terminal prompt).

```bash
python scripts/create_indices.py     # creates all Elasticsearch indices
python scripts/seed_data.py          # seeds historical benchmark data
```

**Step 5 — Start the backend** (keep this terminal open)

```bash
uvicorn main:app --reload --port 8001
```

You should see: `Application startup complete.`  
API docs available at: **http://localhost:8001/docs**
Piper health check available at: **http://localhost:8001/tts/health**

**Step 6 — Start the frontend** (open a new terminal)

```bash
cd frontend
npm install
npm start
```

App is live at: **http://localhost:3000** 🎉

---

### Option B — Local Docker (Elasticsearch runs in a container)

> Use this if you prefer to run everything locally without a cloud account.  
> Requires **Docker Desktop** to be open and running first.

**Step 1 — Clone and go into the project folder**

```bash
git clone <repo-url>
cd Elastic_AI_Interview_Application
```

**Step 2 — Set up your environment file**

```bash
cp .env.example .env
```

Open `.env` and update the Elasticsearch values to point to localhost:

```env
ELASTICSEARCH_URL=http://localhost:9200
ELASTICSEARCH_USER=elastic
ELASTICSEARCH_PASSWORD=yourpassword   # must match ELASTIC_PASSWORD in docker-compose.yml

# After creating agents in Kibana (see "Elastic Agents Setup" below):
ELASTIC_AGENT_URL=http://localhost:5601
ELASTIC_API_KEY=<your-api-key>
SUPERVISOR_AGENT_ID=<your-supervisor-agent-id>
```

**Step 3 — Start Elasticsearch and Kibana**

```bash
docker-compose up elasticsearch kibana -d
```

Wait about 30 seconds, then verify Elasticsearch is healthy:

```bash
curl -u elastic:yourpassword http://localhost:9200/_cluster/health
```

You should see `"status":"green"` or `"status":"yellow"` in the response.  
Kibana is available at: **http://localhost:5601**

**Step 4 — Set up Python**

```bash
python -m venv venv

# Mac / Linux:
source venv/bin/activate

# Windows:
venv\Scripts\activate

pip install -r requirements.txt
```

**Step 4.5 â€” Set up offline interviewer voice (Piper)**

> `requirements.txt` does not install the Piper executable or voice model.

Make sure these files exist:

- [`tools/piper/piper/piper.exe`](tools/piper/piper/piper.exe)
- [`models/piper/en_US-hfc_female-medium.onnx`](models/piper/en_US-hfc_female-medium.onnx)
- [`models/piper/en_US-hfc_female-medium.onnx.json`](models/piper/en_US-hfc_female-medium.onnx.json)

Optional environment overrides:

```env
PIPER_EXE=tools/piper/piper/piper.exe
PIPER_MODEL=models/piper/en_US-hfc_female-medium.onnx
```

**Step 5 — Create indices and seed data**

```bash
python scripts/create_indices.py
python scripts/seed_data.py
```

**Step 6 — Start the backend** (keep this terminal open)

```bash
uvicorn main:app --reload --port 8001
```

You should see: `Application startup complete.`  
API docs available at: **http://localhost:8001/docs**
Piper health check available at: **http://localhost:8001/tts/health**

**Step 7 — Start the frontend** (open a new terminal)

```bash
cd frontend
npm install
npm start
```

App is live at: **http://localhost:3000** 🎉

---

## App Pages

| URL | Description |
|-----|-------------|
| http://localhost:3000/ | Landing page — choose Recruiter or Candidate |
| http://localhost:3000/recruiter | Create interview rooms |
| http://localhost:3000/results | Results dashboard with score charts |
| http://localhost:3000/candidate | Candidate interview portal |

---

## Elastic Agents Setup

The AI interview flow relies on 6 agents built in **Kibana → Search → AI Search → Agent Builder**.  
Their system prompts are in [`agents/prompts.yaml`](agents/prompts.yaml).

Create the agents in this order:

| # | Agent Name | What It Does |
|---|-----------|--------------|
| 1 | Interview Interaction Agent | Conducts the multi-stage Q&A interview |
| 2 | Skill Extraction Agent | Parses transcript and extracts skills |
| 3 | Scoring Agent | Scores 4 dimensions: technical, communication, problem-solving, culture |
| 4 | Reflection Agent | Audits scores for bias, flags MEDIUM/HIGH risk |
| 5 | Benchmarking Agent | Compares candidate vs. historical top hires using kNN |
| 6 | Supervisor Agent | Orchestrates all 5 agents above |

After creating the **Supervisor Agent**, copy its ID and API key into your `.env`:

```env
ELASTIC_AGENT_URL=https://your-cloud.elastic.co
ELASTIC_API_KEY=your_api_key
SUPERVISOR_AGENT_ID=your_supervisor_agent_id
```

Then restart the backend (`Ctrl+C` then `uvicorn main:app --reload --port 8001`).

---

## Troubleshooting

**`ModuleNotFoundError` when running Python scripts**  
→ Your virtual environment is not active. Run `source venv/bin/activate` (Mac/Linux) or `venv\Scripts\activate` (Windows) first.

**`Connection refused` on port 9200**  
→ Elasticsearch isn't running. Start it with `docker-compose up elasticsearch -d` (Option B) or check your cloud URL (Option A).

**`curl` to `/health` returns `"elasticsearch":"red"`**  
→ Elasticsearch is starting up. Wait 30 seconds and try again.

**Frontend shows a blank page or 502 error**  
→ The backend isn't running. Make sure `uvicorn main:app --reload --port 8001` is running in a separate terminal.

**`npm install` fails**  
→ Make sure you're inside the `frontend/` folder: `cd frontend && npm install`.

---

## Resetting Data

```bash
python scripts/create_indices.py --recreate
```

> ⚠️ This **deletes all data** in every index. Only use this during development.

---

## Architecture Overview

```
Browser (port 3000)
    │   REST /api/*   →  proxy
    │   WebSocket /ws/*  →  proxy
    ▼
Node.js Frontend  (frontend/server.js, port 3000)
    │
    ▼ proxied
FastAPI Backend  (main.py, port 8001)
    │
    ├── Elastic Supervisor Agent
    │       ├── Interaction Agent    ──► transcript_index
    │       ├── Skill Extraction     ──► candidate_skill_index
    │       ├── Scoring Agent        ──► evaluation_index
    │       ├── Reflection Agent     ──► bias_alerts_index
    │       └── Benchmarking Agent   ──► historical_top_hires (kNN)
    │
    └── Elasticsearch
```

---

## Interview Pipeline Stages

| Stage | What Happens |
|-------|-------------|
| `GREETING` | Welcome candidate, confirm role |
| `INTERACTION` | 6–8 adaptive questions via Interaction Agent |
| `SKILL_EXTRACTION` | Parse transcript → extract skills |
| `SCORING` | Score 4 dimensions, compute weighted total |
| `REFLECTION` | Audit score for bias; flag if MEDIUM or HIGH risk |
| `BENCHMARKING` | kNN vs. historical top hires → percentile rank |
| `COMPLETE` | Final report stored; recruiter can view results |

---

## Results Dashboard Features

- **Score chart** — bar chart of each candidate's score
- **Fullscreen view** — click the ⤢ button (press Escape to close)
- **Click a bar** — opens the candidate detail modal
- **Sort & filter** — by score, name, or recommendation (Hire / Neutral / Pass / Pending)
- **Session detail** — evaluation scores, transcript, recording, and CV links per candidate

---

## Key API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/tts/health` | Offline Piper TTS health check |
| `POST` | `/tts` | Generate interviewer speech audio |
| `POST` | `/rooms` | Create interview room |
| `GET` | `/rooms/{code}` | Get room by code |
| `GET` | `/rooms/{code}/candidates` | All candidates + evaluations |
| `GET` | `/rooms/{code}/results` | Full results for dashboard |
| `DELETE` | `/rooms/{code}` | Delete a room |
| `GET` | `/interviewer/{id}/rooms` | All rooms for a recruiter |
| `POST` | `/extract-skills` | Auto-extract skills from JD |
| `POST` | `/candidates/register` | Candidate joins a room |
| `GET` | `/candidates/{id}/details` | Full candidate detail + sessions |
| `POST` | `/evaluate/{session_id}` | Trigger / re-run evaluation |
| `GET` | `/recordings/{session_id}` | Interview recording |
| `GET` | `/cv/{session_id}` | Candidate CV |
| `WS` | `/ws/interview/{session_id}/{job_id}/{candidate_id}` | Live interview WebSocket |

---

## Elasticsearch Indices

| Index | Purpose |
|-------|---------|
| `job_requirements_index` | Job rooms + extracted required skills |
| `candidate_profile_index` | Candidate profiles + resume text |
| `interview_session_index` | Session state + stage tracking |
| `transcript_index` | Full conversation history |
| `candidate_skill_index` | AI-extracted candidate skills |
| `evaluation_index` | Scores + recommendation |
| `bias_alerts_index` | Bias risk flags |
| `historical_top_hires` | Benchmark data for kNN comparison |

---

## Project Structure

```
ai_interviewer/
├── main.py                     # FastAPI backend — all API + WebSocket relay
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Backend container
├── docker-compose.yml          # Elasticsearch + Kibana + backend
├── .env.example                # Environment variable template
│
├── frontend/
│   ├── server.js               # Node.js Express server + proxy (port 3000)
│   ├── package.json
│   └── public/
│       ├── index.html          # Landing page
│       ├── recruiter.html      # Create + manage interview rooms
│       ├── candidate.html      # Candidate interview portal (WebSocket)
│       └── results.html        # Results dashboard with score chart
│
├── agents/
│   └── prompts.yaml            # System prompts for all Elastic agents
│
├── scripts/
│   ├── create_indices.py       # Create all Elasticsearch indices
│   ├── seed_data.py            # Seed historical benchmark data
│   ├── setup_agents.py         # Create Elastic agents via API
│   ├── check_es.py             # Verify Elasticsearch connection
│   ├── test_demo.py            # End-to-end test runner
│   └── verify_agent.py         # Verify agent connectivity
│
├── config/
│   └── kibana_setup.md         # Kibana dashboard setup guide
│
├── recordings/                 # Interview recordings (auto-created)
└── cv/                         # Uploaded CVs (auto-created)
```

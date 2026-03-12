# 🐴 AI Interview System

An end-to-end AI-powered interview platform.  
Recruiters create a room, share a code, and the AI agent conducts, scores, and ranks every candidate automatically.

**Stack:** FastAPI · Elasticsearch · Elastic Agent Builder · Node.js · Chart.js

---

## Architecture

```
Browser (port 3000)
    │   REST /api/*  →  proxy
    │   WebSocket /ws/*  →  proxy
    ▼
Node.js Frontend Server  (frontend/server.js)
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
    └── Elasticsearch  (indices below)
```

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
│   ├── server.js               # Node.js Express server + API proxy (port 3000)
│   ├── package.json
│   └── public/
│       ├── index.html          # Landing page with animated horse mascot
│       ├── recruiter.html      # Create interview rooms + manage rooms
│       ├── candidate.html      # Candidate interview portal (WebSocket chat)
│       └── results.html        # Results dashboard with interactive score chart
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
├── static/                     # Legacy static files (served at /static)
├── recordings/                 # Interview recordings (auto-created)
└── cv/                         # Uploaded CVs (auto-created)
```

---

## Prerequisites

| Tool            | Version   |
|-----------------|-----------|
| Python          | 3.10+     |
| Node.js         | 18+       |
| Docker Compose  | latest    |
| Elasticsearch   | 8.13+     |

---

## Quick Start

### 1. Configure environment

```bash
cd ai_interviewer
cp .env.example .env
# Fill in ELASTICSEARCH_URL, ELASTICSEARCH_PASSWORD, ELASTIC_AGENT_URL, ELASTIC_API_KEY
```

### 2. Start Elasticsearch + Kibana

```bash
docker-compose up elasticsearch kibana -d
```

Wait ~30 s, then check health:

```bash
curl -u elastic:yourpassword http://localhost:9200/_cluster/health
```

Kibana: **http://localhost:5601**

### 3. Set up Python + create indices

```bash
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt

python scripts/create_indices.py     # create all indices
python scripts/seed_data.py          # seed benchmark data
```

### 4. Start the backend

```bash
uvicorn main:app --reload --port 8001
```

API docs: **http://localhost:8001/docs**

### 5. Start the frontend

```bash
cd frontend
npm install
npm start
```

App: **http://localhost:3000**

---

## Pages

| URL                          | Description                              |
|------------------------------|------------------------------------------|
| `http://localhost:3000/`     | Landing page — choose recruiter or candidate |
| `/recruiter`                 | Create interview rooms                   |
| `/results`                   | Results dashboard with score chart       |
| `/candidate`                 | Candidate interview portal               |

---

## Results Dashboard Features

- **Score chart** — bar chart showing each candidate's score (Y) vs. name (X)
- **Expand chart** — click the ⤢ button (or press Escape to close) for a fullscreen view
- **Click to view candidate** — click any bar or name label to open the candidate detail modal
- **Sort & filter** — sort by score or name; filter by recommendation (Hire / Neutral / Pass / Pending)
- **Session detail** — evaluation scores, transcript, recording, and CV links per candidate

---

## Elasticsearch Indices

| Index                      | Purpose                              |
|----------------------------|--------------------------------------|
| `job_requirements_index`   | Job rooms + extracted required skills |
| `candidate_profile_index`  | Candidate profiles + resume text     |
| `interview_session_index`  | Session state + stage tracking       |
| `transcript_index`         | Full conversation history            |
| `candidate_skill_index`    | AI-extracted candidate skills        |
| `evaluation_index`         | Scores + recommendation              |
| `bias_alerts_index`        | Bias risk flags                      |
| `historical_top_hires`     | Benchmark data for kNN comparison    |

---

## Key API Endpoints

| Method | Path                                    | Description                        |
|--------|-----------------------------------------|------------------------------------|
| `GET`  | `/health`                               | Health check                       |
| `POST` | `/rooms`                                | Create interview room              |
| `GET`  | `/rooms/{code}`                         | Get room by code                   |
| `GET`  | `/rooms/{code}/candidates`              | All candidates + evaluations       |
| `GET`  | `/rooms/{code}/results`                 | Full results for dashboard         |
| `DELETE` | `/rooms/{code}`                       | Delete a room                      |
| `GET`  | `/interviewer/{id}/rooms`               | All rooms for a recruiter          |
| `POST` | `/extract-skills`                       | Auto-extract skills from JD        |
| `POST` | `/register`                             | Candidate joins a room             |
| `GET`  | `/candidates/{id}/details`              | Full candidate detail + sessions   |
| `POST` | `/evaluate/{session_id}`               | Trigger/re-run evaluation          |
| `GET`  | `/recordings/{session_id}`              | Interview recording                |
| `GET`  | `/cv/{session_id}`                      | Candidate CV                       |
| `WS`   | `/ws/interview/{session_id}/{job_id}/{candidate_id}` | Live interview WebSocket |

---

## Elastic Agents Setup

In **Kibana → Search → AI Search → Agent Builder**, create 6 agents (see `agents/prompts.yaml` for prompts):

1. **Interview Interaction Agent** — conducts multi-stage Q&A
2. **Skill Extraction Agent** — parses transcript → skills
3. **Scoring Agent** — scores 4 dimensions (technical, communication, problem-solving, culture)
4. **Reflection Agent** — audits for bias, flags MEDIUM/HIGH risk
5. **Benchmarking Agent** — kNN similarity vs. historical top hires
6. **Supervisor Agent** — orchestrates all the above

After creating the Supervisor Agent, copy its ID into `.env`:

```env
ELASTIC_AGENT_URL=https://your-cloud.elastic.co
ELASTIC_API_KEY=your_api_key
SUPERVISOR_AGENT_ID=your_supervisor_agent_id
```

---

## Docker (Full Stack)

```bash
docker-compose up --build
```

This starts Elasticsearch, Kibana, and the FastAPI backend together.  
Run the Node.js frontend separately: `cd frontend && npm start`

---

## Reset Indices

```bash
python scripts/create_indices.py --recreate
```

⚠️ This deletes all data. Use only in development.

---

## Pipeline Stages

| Stage           | What Happens                                           |
|-----------------|--------------------------------------------------------|
| `GREETING`      | Welcome candidate, confirm role                        |
| `INTERACTION`   | 6–8 adaptive questions via Interaction Agent           |
| `SKILL_EXTRACTION` | Parse transcript → extract skills                 |
| `SCORING`       | Score 4 dimensions, compute weighted total             |
| `REFLECTION`    | Audit score for bias; flag if MEDIUM or HIGH risk      |
| `BENCHMARKING`  | kNN vs. historical top hires → percentile rank         |
| `COMPLETE`      | Final report stored; recruiter can view results        |

"""
scripts/seed_data.py
────────────────────
Populates the `historical_top_hires` index with realistic benchmark data
so the Benchmarking Agent has enough signal to assign meaningful percentiles.

Run after create_indices.py:
    python scripts/seed_data.py
    python scripts/seed_data.py --clear   # wipe and re-seed
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

import os
from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer

ES_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
ES_USER = os.getenv("ELASTICSEARCH_USER", "elastic")
ES_PASS = os.getenv("ELASTICSEARCH_PASSWORD", "yourpassword")

es = Elasticsearch(ES_URL, basic_auth=(ES_USER, ES_PASS))
model = SentenceTransformer("all-MiniLM-L6-v2")

# ──────────────────────────────────────────────────────────────────────────────
# Historical hire records
# ──────────────────────────────────────────────────────────────────────────────
TOP_HIRES = [
    # ── Senior Python Engineer (job_001) — strong hires ──────────────────────
    {"candidate_id": "hist_001", "job_id": "job_001",
     "skills": ["Python", "FastAPI", "AWS", "Docker", "PostgreSQL", "Redis"],
     "performance_score": 95.0},
    {"candidate_id": "hist_002", "job_id": "job_001",
     "skills": ["Python", "Django", "PostgreSQL", "Kubernetes", "CI/CD"],
     "performance_score": 91.0},
    {"candidate_id": "hist_003", "job_id": "job_001",
     "skills": ["Python", "FastAPI", "Celery", "RabbitMQ", "Docker", "AWS"],
     "performance_score": 89.0},
    {"candidate_id": "hist_004", "job_id": "job_001",
     "skills": ["Python", "SQLAlchemy", "PostgreSQL", "FastAPI", "Pytest"],
     "performance_score": 88.0},
    {"candidate_id": "hist_005", "job_id": "job_001",
     "skills": ["Python", "Flask", "AWS Lambda", "DynamoDB", "Terraform"],
     "performance_score": 85.0},
    # ── Mid-tier hires ────────────────────────────────────────────────────────
    {"candidate_id": "hist_006", "job_id": "job_001",
     "skills": ["Python", "FastAPI", "MySQL", "Docker"],
     "performance_score": 78.0},
    {"candidate_id": "hist_007", "job_id": "job_001",
     "skills": ["Python", "Django", "REST API", "Git"],
     "performance_score": 74.0},
    {"candidate_id": "hist_008", "job_id": "job_001",
     "skills": ["Python", "Pandas", "FastAPI", "PostgreSQL"],
     "performance_score": 72.0},
    {"candidate_id": "hist_009", "job_id": "job_001",
     "skills": ["Python", "SQLite", "Flask", "HTML"],
     "performance_score": 65.0},
    {"candidate_id": "hist_010", "job_id": "job_001",
     "skills": ["Python", "NumPy", "scripting", "bash"],
     "performance_score": 60.0},
    # ── Lower-tier hires ──────────────────────────────────────────────────────
    {"candidate_id": "hist_011", "job_id": "job_001",
     "skills": ["Python", "basic REST"],
     "performance_score": 52.0},
    {"candidate_id": "hist_012", "job_id": "job_001",
     "skills": ["Python", "Jupyter", "data analysis"],
     "performance_score": 48.0},
    # ── More strong hires to enrich the distribution ──────────────────────────
    {"candidate_id": "hist_013", "job_id": "job_001",
     "skills": ["Python", "FastAPI", "AWS ECS", "Prometheus", "Grafana"],
     "performance_score": 93.0},
    {"candidate_id": "hist_014", "job_id": "job_001",
     "skills": ["Python", "aiohttp", "asyncio", "PostgreSQL", "Redis"],
     "performance_score": 90.0},
    {"candidate_id": "hist_015", "job_id": "job_001",
     "skills": ["Python", "FastAPI", "Pydantic", "SQLModel", "Docker", "AWS"],
     "performance_score": 87.0},
    {"candidate_id": "hist_016", "job_id": "job_001",
     "skills": ["Python", "FastAPI", "GraphQL", "PostgreSQL", "Kubernetes"],
     "performance_score": 84.0},
    {"candidate_id": "hist_017", "job_id": "job_001",
     "skills": ["Python", "Django REST Framework", "PostgreSQL", "Celery", "Docker"],
     "performance_score": 82.0},
    {"candidate_id": "hist_018", "job_id": "job_001",
     "skills": ["Python", "microservices", "gRPC", "Docker", "AWS"],
     "performance_score": 80.0},
    {"candidate_id": "hist_019", "job_id": "job_001",
     "skills": ["Python", "event-driven", "Kafka", "PostgreSQL", "FastAPI"],
     "performance_score": 83.0},
    {"candidate_id": "hist_020", "job_id": "job_001",
     "skills": ["Python", "FastAPI", "Elasticsearch", "Redis", "Docker"],
     "performance_score": 86.0},
]


def seed(clear: bool = False) -> None:
    print(f"\nConnecting to Elasticsearch at {ES_URL} …")
    try:
        es.cluster.health()
    except Exception as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)

    if clear:
        es.delete_by_query(
            index="historical_top_hires",
            query={"match_all": {}},
        )
        print("  Cleared existing records in historical_top_hires\n")
        time.sleep(1)   # let ES refresh

    print(f"  Encoding and indexing {len(TOP_HIRES)} historical hires …\n")
    for i, hire in enumerate(TOP_HIRES, 1):
        text = f"Skills: {', '.join(hire['skills'])}"
        hire_doc = {**hire, "profile_embedding": model.encode(text).tolist()}
        es.index(index="historical_top_hires", document=hire_doc)
        print(f"  [{i:02d}/{len(TOP_HIRES)}]  {hire['candidate_id']}  "
              f"score={hire['performance_score']}")

    print(f"\nSeeded {len(TOP_HIRES)} records into historical_top_hires.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed historical hire data.")
    parser.add_argument("--clear", action="store_true", help="Delete existing records before seeding.")
    args = parser.parse_args()
    seed(clear=args.clear)

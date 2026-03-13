"""
scripts/create_indices.py
─────────────────────────
Creates (or re-creates) all Elasticsearch indices required by the
AI Interview System.  Run once before starting the application.

Usage:
    python scripts/create_indices.py
    python scripts/create_indices.py --recreate   # drops existing first
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

import os
from elasticsearch import Elasticsearch

ES_URL  = os.getenv("ELASTICSEARCH_URL")
ES_USER = os.getenv("ELASTICSEARCH_USER", "elastic")
ES_PASS = os.getenv("ELASTICSEARCH_PASSWORD")

if not ES_URL:
    print("ERROR: ELASTICSEARCH_URL env var is required")
    sys.exit(1)

es = Elasticsearch(ES_URL, basic_auth=(ES_USER, ES_PASS))

INDICES: dict[str, dict] = {
    # ── Job postings ──────────────────────────────────────────────────────────
    "job_requirements_index": {
        "mappings": {
            "properties": {
                "job_id":          {"type": "keyword"},
                "room_code":       {"type": "keyword"},
                "title":           {"type": "text"},
                "required_skills": {"type": "keyword"},
                "description":     {"type": "text"},
                "description_embedding": {
                    "type": "dense_vector",
                    "dims": 384,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        }
    },
    # ── Candidate profiles ────────────────────────────────────────────────────
    "candidate_profile_index": {
        "mappings": {
            "properties": {
                "candidate_id": {"type": "keyword"},
                "name":         {"type": "text"},
                "email":        {"type": "keyword"},
                "job_id":       {"type": "keyword"},
                "created_at":   {"type": "date"},
            }
        }
    },
    # ── Interview sessions ────────────────────────────────────────────────────
    "interview_session_index": {
        "mappings": {
            "properties": {
                "session_id":    {"type": "keyword"},
                "candidate_id":  {"type": "keyword"},
                "job_id":        {"type": "keyword"},
                "stage":         {"type": "keyword"},
                "status":        {"type": "keyword"},
                "started_at":    {"type": "date"},
                "completed_at":  {"type": "date"},
                "updated_at":    {"type": "date"},
            }
        }
    },
    # ── Conversation transcripts ──────────────────────────────────────────────
    "transcript_index": {
        "mappings": {
            "properties": {
                "session_id":  {"type": "keyword"},
                "role":        {"type": "keyword"},
                "content":     {"type": "text"},
                "timestamp":   {"type": "date"},
                "content_embedding": {
                    "type": "dense_vector",
                    "dims": 384,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        }
    },
    # ── Extracted skills ──────────────────────────────────────────────────────
    "candidate_skill_index": {
        "mappings": {
            "properties": {
                "session_id":   {"type": "keyword"},
                "candidate_id": {"type": "keyword"},
                "skill_name":   {"type": "keyword"},
                "proficiency":  {"type": "keyword"},
                "evidence":     {"type": "text"},
                "confidence":   {"type": "float"},
            }
        }
    },
    # ── Scoring evaluations ───────────────────────────────────────────────────
    "evaluation_index": {
        "mappings": {
            "properties": {
                "session_id":              {"type": "keyword"},
                "candidate_id":            {"type": "keyword"},
                "job_id":                  {"type": "keyword"},
                "technical_score":         {"type": "float"},
                "communication_score":     {"type": "float"},
                "problem_solving_score":   {"type": "float"},
                "cultural_fit_score":      {"type": "float"},
                "overall_score":           {"type": "float"},
                "recommendation":          {"type": "keyword"},
                "strengths":               {"type": "text"},
                "weaknesses":              {"type": "text"},
                "summary":                 {"type": "text"},
                "score_explanations":      {"type": "object", "enabled": True},
                "bias_detected":           {"type": "boolean"},
                "bias_notes":              {"type": "text"},
                "source":                  {"type": "keyword"},
                "scored_at":               {"type": "date"},
            }
        }
    },
    # ── Historical top hires (benchmarking) ───────────────────────────────────
    "historical_top_hires": {
        "mappings": {
            "properties": {
                "candidate_id":      {"type": "keyword"},
                "job_id":            {"type": "keyword"},
                "skills":            {"type": "keyword"},
                "performance_score": {"type": "float"},
                "profile_embedding": {
                    "type": "dense_vector",
                    "dims": 384,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        }
    },
    # ── Bias / fairness alerts ────────────────────────────────────────────────
    "bias_alerts_index": {
        "mappings": {
            "properties": {
                "session_id":      {"type": "keyword"},
                "risk_level":      {"type": "keyword"},
                "concerns":        {"type": "text"},
                "recommendations": {"type": "text"},
                "flagged_at":      {"type": "date"},
                "reviewed":        {"type": "boolean"},
            }
        }
    },    # ── Benchmark results ────────────────────────────────────────────────
    "benchmark_results_index": {
        "mappings": {
            "properties": {
                "session_id":       {"type": "keyword"},
                "candidate_id":     {"type": "keyword"},
                "job_id":           {"type": "keyword"},
                "benchmark_score":  {"type": "float"},
                "percentile":       {"type": "integer"},
                "ranking":          {"type": "keyword"},
                "comparison_notes": {"type": "text"},
                "similar_hire_count": {"type": "integer"},
                "reasoning":        {"type": "text"},
                "benchmarked_at":   {"type": "date"},
            }
        }
    },    # ── Live coding — question bank ───────────────────────────────────────────
    "coding_questions_index": {
        "mappings": {
            "properties": {
                "question_id":          {"type": "keyword"},
                "room_code":            {"type": "keyword"},
                "title":                {"type": "text"},
                "description":          {"type": "text"},
                "difficulty":           {"type": "keyword"},   # easy|medium|hard
                "tags":                 {"type": "keyword"},
                "examples":             {"type": "object",  "enabled": True},
                "constraints":          {"type": "text"},
                "starter_code_python":  {"type": "text"},
                "starter_code_js":      {"type": "text"},
                "created_at":           {"type": "date"},
            }
        }
    },
    # ── Live coding — candidate sessions ─────────────────────────────────────
    "live_coding_session_index": {
        "mappings": {
            "properties": {
                "session_id":       {"type": "keyword"},
                "room_code":        {"type": "keyword"},
                "candidate_id":     {"type": "keyword"},
                "candidate_name":   {"type": "text"},
                "candidate_email":  {"type": "keyword"},
                "question_ids":     {"type": "keyword"},
                "status":           {"type": "keyword"},   # active|completed
                "language":         {"type": "keyword"},
                "code_snapshots":   {"type": "object",  "enabled": True},
                "emotion_timeline": {"type": "object",  "enabled": True},
                "challenger_log":   {"type": "object",  "enabled": True},
                "started_at":       {"type": "date"},
                "completed_at":     {"type": "date"},
            }
        }
    },
    # ── Live coding rooms ─────────────────────────────────────────────────────
    "live_coding_room_index": {
        "mappings": {
            "properties": {
                "room_code":             {"type": "keyword"},
                "room_id":               {"type": "keyword"},
                "title":                 {"type": "text"},
                "description":           {"type": "text"},
                "num_questions":         {"type": "integer"},
                "time_limit_minutes":    {"type": "integer"},
                "interviewer_id":        {"type": "keyword"},
                "active":                {"type": "boolean"},
                "created_at":            {"type": "date"},
            }
        }
    },}


def create_indices(recreate: bool = False) -> None:
    print(f"\nConnecting to Elasticsearch at {ES_URL} …")
    try:
        health = es.cluster.health()
        print(f"  Cluster status: {health['status']}\n")
    except Exception as exc:
        print(f"  ERROR: Cannot reach Elasticsearch — {exc}")
        sys.exit(1)

    for name, body in INDICES.items():
        exists = es.indices.exists(index=name)

        if exists:
            if recreate:
                es.indices.delete(index=name)
                print(f"  Deleted  {name}")
            else:
                print(f"  Skipped  {name}  (already exists; use --recreate to reset)")
                continue

        es.indices.create(index=name, body=body)
        print(f"  Created  {name}")

    print("\nAll indices ready.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create Elasticsearch indices for the AI Interview System.")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete and re-create indices if they already exist.",
    )
    args = parser.parse_args()
    create_indices(recreate=args.recreate)

# Kibana Dashboard & Alerting Setup
# ─────────────────────────────────────────────────────────────────────────────
# Run these in Kibana → Dev Tools (or via curl) after creating the indices.
# ─────────────────────────────────────────────────────────────────────────────

# ── 1. Create index patterns ──────────────────────────────────────────────────
# Kibana → Stack Management → Index Patterns → Create index pattern
# Create one pattern for each index below:
#
#   interview_session_index   (time field: started_at)
#   evaluation_index          (no time field)
#   bias_alerts_index         (time field: flagged_at)
#   candidate_skill_index     (no time field)
#   transcript_index          (time field: timestamp)

# ── 2. Saved searches ─────────────────────────────────────────────────────────

POST kbn:/api/saved_objects/search
{
  "attributes": {
    "title": "Active Interview Sessions",
    "description": "All sessions with status=active",
    "kibanaSavedObjectMeta": {
      "searchSourceJSON": "{\"index\":\"interview_session_index\",\"filter\":[{\"meta\":{\"index\":\"interview_session_index\"},\"query\":{\"term\":{\"status\":\"active\"}}}],\"query\":{\"query_string\":{\"query\":\"*\"}}}"
    }
  }
}

POST kbn:/api/saved_objects/search
{
  "attributes": {
    "title": "High Risk Bias Alerts (Unreviewed)",
    "kibanaSavedObjectMeta": {
      "searchSourceJSON": "{\"index\":\"bias_alerts_index\",\"filter\":[{\"query\":{\"term\":{\"risk_level\":\"HIGH\"}}},{\"query\":{\"term\":{\"reviewed\":false}}}]}"
    }
  }
}

# ── 3. Kibana Lens visualisations (use the UI) ────────────────────────────────
#
# Panel 1 — Interview Funnel  (Vertical bar)
#   Index:  interview_session_index
#   X-axis: terms(stage)
#   Y-axis: count()
#
# Panel 2 — Score Distribution  (Histogram)
#   Index:  evaluation_index
#   Field:  total_score
#   Bucket interval: 10
#
# Panel 3 — High Bias Alert Count  (Metric)
#   Index:  bias_alerts_index
#   Filter: risk_level:HIGH AND reviewed:false
#   Metric: count()
#
# Panel 4 — Recommendation Breakdown  (Pie)
#   Index:  evaluation_index
#   Slice by: terms(recommendation)
#
# Panel 5 — Sessions Over Time  (Area)
#   Index:  interview_session_index
#   X-axis: date_histogram(started_at, interval=1d)
#   Y-axis: count()
#
# Panel 6 — Average Scores by Job  (Data table)
#   Index:  evaluation_index
#   Columns: job_id, avg(total_score), avg(technical_score), count()

# ── 4. Alerting rule — High Bias Alert ────────────────────────────────────────
POST kbn:/api/alerting/rule
{
  "name": "High Bias Alert — Immediate Review Required",
  "rule_type_id": ".es-query",
  "schedule": { "interval": "1h" },
  "params": {
    "index": ["bias_alerts_index"],
    "timeField": "flagged_at",
    "timeWindowSize": 1,
    "timeWindowUnit": "h",
    "thresholdComparator": ">",
    "threshold": [0],
    "esQuery": "{\"query\":{\"bool\":{\"must\":[{\"term\":{\"risk_level\":\"HIGH\"}},{\"term\":{\"reviewed\":false}}]}}}"
  },
  "actions": [
    {
      "id": "<your-email-connector-id>",
      "group": "threshold met",
      "params": {
        "to": ["recruiting-team@yourcompany.com"],
        "subject": "🚨 HIGH Bias Alert — Immediate Review Required",
        "message": "One or more high-risk bias alerts have been detected in the AI Interview System.\n\nPlease review in Kibana: http://localhost:5601"
      }
    }
  ]
}

# ── 5. Watcher — Daily summary (optional) ─────────────────────────────────────
PUT _watcher/watch/daily_interview_summary
{
  "trigger": {
    "schedule": { "cron": "0 0 9 * * ?" }
  },
  "input": {
    "search": {
      "request": {
        "indices": ["interview_session_index", "evaluation_index"],
        "body": {
          "aggs": {
            "total_sessions": { "value_count": { "field": "session_id" } },
            "by_status": { "terms": { "field": "status" } },
            "avg_score": { "avg": { "field": "total_score" } },
            "recommendations": { "terms": { "field": "recommendation" } }
          },
          "size": 0
        }
      }
    }
  },
  "actions": {
    "log_summary": {
      "logging": {
        "text": "Daily interview summary: {{ctx.payload.aggregations.total_sessions.value}} sessions"
      }
    }
  }
}

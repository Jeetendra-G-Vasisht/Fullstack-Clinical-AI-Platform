"""
kBuddhi AI — review-pipeline Lambda
-------------------------------------------------------------
Function URL only, same reasoning as chat/structured-query: this can be
invoked with a large candidate pool and shouldn't be bound by API
Gateway's 29s integration timeout.

Not wired into the CDK stack yet -- see the module README for why.

Runs the multi-agent review/matching pipeline (pipeline/graph.py) against
one document record and a pool of candidate records, and returns the
final decision plus the full per-stage trace.

Request:  {
  "document": {"doc_id": "...", "name": "...", "dob": "...", "mrn": "..."},
  "candidates": [{"patient_id": "...", "name": "...", "dob": "...", "mrn": "..."}, ...],
  "gate_enabled": true   # optional, defaults to true
}
Response: { "final": {...}, "trace": [...] }
"""
import json
import os

from pipeline import run_review

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://kbuddhiai.com")


def lambda_handler(event, context):
    is_function_url = event.get("version") == "2.0"
    cors = {} if is_function_url else {
        "Access-Control-Allow-Origin":  ALLOWED_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
    }

    def resp(status, body):
        return {"statusCode": status, "headers": cors, "body": json.dumps(body)}

    try:
        body = json.loads(event.get("body") or "{}")
        document = body.get("document")
        candidates = body.get("candidates")
        gate_enabled = body.get("gate_enabled", True)

        if not isinstance(document, dict) or not isinstance(candidates, list) or not candidates:
            return resp(400, {"error": "document (object) and candidates (non-empty array) are required"})

        result = run_review(document, candidates, gate_enabled=bool(gate_enabled))
        return resp(200, {"final": result["final"], "trace": result["trace"]})

    except Exception as e:
        print("Error:", e)
        return resp(500, {"error": "Internal server error", "detail": str(e)})

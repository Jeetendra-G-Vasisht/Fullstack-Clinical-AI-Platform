"""
verify-otp Lambda
-----------------
POST /verify-otp  {email, otp_code}

Looks up the OTP in DynamoDB, validates it, and on success returns
{user_sub, email} which the frontend stores as the session.
"""

import json
import os
import time

import boto3
from botocore.exceptions import ClientError

REGION         = os.environ.get("AWS_REGION", "us-east-2")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "kbuddhiai-otp-codes")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://kbuddhiai.com")

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  ALLOWED_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}


def lambda_handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    try:
        body     = json.loads(event.get("body") or "{}")
        email    = body.get("email", "").strip().lower()
        otp_code = body.get("otp_code", "").strip()

        if not email or not otp_code:
            return _resp(400, {"error": "email and otp_code are required"})

        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table    = dynamodb.Table(DYNAMODB_TABLE)

        # ── Look up OTP record ───────────────────────────────────────────────
        result = table.get_item(Key={"email": email})
        item   = result.get("Item")

        if not item:
            return _resp(401, {"error": "Session expired. Please sign in again."})

        # ── Check expiry (belt-and-suspenders on top of DynamoDB TTL) ────────
        if int(time.time()) > int(item.get("expiry", 0)):
            _safe_delete(table, email)
            return _resp(401, {"error": "Code expired. Please sign in again."})

        # ── Check OTP ────────────────────────────────────────────────────────
        if otp_code != item.get("otp"):
            return _resp(401, {"error": "Incorrect code. Please try again."})

        # ── Valid — consume the record and return session info ────────────────
        user_sub = item.get("user_sub", "")
        _safe_delete(table, email)

        return _resp(200, {
            "success":  True,
            "user_sub": user_sub,
            "email":    email,
        })

    except Exception as e:
        print("Unexpected error:", e)
        return _resp(500, {"error": "Internal server error"})


def _safe_delete(table, email: str) -> None:
    try:
        table.delete_item(Key={"email": email})
    except Exception as e:
        print("Could not delete OTP record:", e)


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    CORS_HEADERS,
        "body":       json.dumps(body),
    }

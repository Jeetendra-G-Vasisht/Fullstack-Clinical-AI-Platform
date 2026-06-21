"""
get-upload-url Lambda  (updated for kbuddhiai.com migration)
-------------------------------------------------------------
POST /get-upload-url  {filename, user_sub}

Generates a pre-signed S3 POST URL. The S3 key follows the partition
structure required for Athena/Glue compatibility:

    uploads/user_id={cognito_sub}/year={YYYY}/month={MM}/{filename}

After a successful upload the frontend can trigger the chat Lambda
using the returned key.
"""

import json
import os
import re
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET_NAME    = os.environ.get("BUCKET_NAME", "")
BUCKET_REGION  = os.environ.get("BUCKET_REGION", "us-east-2")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://kbuddhiai.com")

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  ALLOWED_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}

# Characters allowed in S3 keys (conservative safe set)
_SAFE = re.compile(r"[^a-zA-Z0-9.\-_]")


def lambda_handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    try:
        body     = json.loads(event.get("body") or "{}")
        filename = body.get("filename", "").strip()
        user_sub = body.get("user_sub", "").strip()

        if not filename:
            return _resp(400, {"error": "filename is required"})
        if not user_sub:
            return _resp(400, {"error": "user_sub is required"})

        safe_name = _SAFE.sub("_", filename)
        now       = datetime.utcnow()
        key       = (
            f"uploads/user_id={user_sub}"
            f"/year={now.year:04d}"
            f"/month={now.month:02d}"
            f"/{safe_name}"
        )

        s3 = boto3.client(
            "s3",
            region_name=BUCKET_REGION,
            config=Config(s3={"addressing_style": "virtual"}),
        )

        presigned = s3.generate_presigned_post(
            Bucket=BUCKET_NAME,
            Key=key,
            ExpiresIn=900,  # 15 minutes
        )

        return _resp(200, {
            "upload_url": presigned["url"],
            "fields":     presigned["fields"],
            "key":        key,
        })

    except ClientError as e:
        print("ClientError:", e)
        return _resp(500, {"error": "Could not generate upload URL"})
    except Exception as e:
        print("Unexpected error:", e)
        return _resp(500, {"error": "Internal server error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    CORS_HEADERS,
        "body":       json.dumps(body),
    }

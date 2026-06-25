"""
kbuddhiai-sms-send
------------------
Reads a patient Excel file from kbuddhiai S3, sends a bill-reminder or
wellness-visit SMS to each patient, and stores the conversation in DynamoDB.

Expected Excel columns (case-insensitive):
  First Name, Last Name, Amount Due, Mobile Number

Environment variables:
  BUCKET_NAME        – kbuddhiai-uploads-699092321120
  DYNAMODB_TABLE     – kbuddhiai-sms-conversations
  ORIGINATION_NUMBER – +18557684735
  ALLOWED_ORIGIN     – https://kbuddhiai.com
"""

import io
import json
import os
import re

import boto3
import openpyxl

BUCKET_NAME        = os.environ.get("BUCKET_NAME", "")
DYNAMODB_TABLE     = os.environ.get("DYNAMODB_TABLE", "kbuddhiai-sms-conversations")
ORIGINATION_NUMBER = os.environ.get("ORIGINATION_NUMBER", "")
ALLOWED_ORIGIN     = os.environ.get("ALLOWED_ORIGIN", "https://kbuddhiai.com")

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  ALLOWED_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}

s3    = boto3.client("s3")
sms   = boto3.client("pinpoint-sms-voice-v2", region_name="us-east-1")
ddb   = boto3.resource("dynamodb")
table = ddb.Table(DYNAMODB_TABLE)


def normalize_phone(raw) -> str | None:
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def build_message(row: dict, message_type: str) -> str:
    first  = (row.get("first name") or "").strip()
    last   = (row.get("last name") or "").strip()
    name   = f"{first} {last}".strip() or "Patient"

    if message_type == "balance":
        amount = row.get("amount due") or "0.00"
        try:
            amount = f"{float(str(amount).replace(',', '')):.2f}"
        except Exception:
            amount = str(amount).strip()
        return (
            f"Hi {name}, you have an outstanding bill amount due of ${amount}. "
            f"Call 480-406-5664 at your convenience. Reply STOP to opt out."
        )
    else:
        return (
            f"Hi {name}, this is a reminder to schedule your Annual Wellness Visit. "
            f"Please call Wonderful Clinic at 480-406-5664. Reply STOP to opt out."
        )


def read_excel(bucket: str, key: str) -> list[dict]:
    obj  = s3.get_object(Bucket=bucket, Key=key)
    wb   = openpyxl.load_workbook(io.BytesIO(obj["Body"].read()), read_only=True, data_only=True)
    ws   = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h).lower().strip() if h is not None else "" for h in rows[0]]
    return [{headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))} for row in rows[1:]]


def get_conversations() -> dict:
    result = table.scan()
    items  = result.get("Items", [])
    convs  = []
    for item in items:
        try:
            history = json.loads(item.get("conversation_history") or "[]")
        except Exception:
            history = []
        last_reply = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), None
        )
        convs.append({
            "phone":        item.get("phone_number", ""),
            "patient_name": item.get("patient_name", ""),
            "amount_due":   item.get("amount_due", ""),
            "message_type": item.get("message_type", "balance"),
            "reply_count":  int(item.get("reply_count") or 0),
            "opt_out":      bool(item.get("opt_out", False)),
            "last_reply":   last_reply,
        })
    convs.sort(key=lambda x: x["reply_count"], reverse=True)
    return _resp(200, {"conversations": convs})


def lambda_handler(event, context):
    if event.get("requestContext", {}).get("http", {}).get("method") == "GET" or \
       event.get("httpMethod") == "GET":
        return get_conversations()

    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    try:
        body         = json.loads(event.get("body") or "{}")
        excel_key    = body.get("excel_key", "")
        message_type = body.get("message_type", "balance")  # "balance" or "wellness"

        if not excel_key:
            return _resp(400, {"error": "excel_key is required"})

        rows = read_excel(BUCKET_NAME, excel_key)
        sent = skipped = 0
        errors = []

        for row in rows:
            mobile_raw = row.get("mobile number") or row.get("mobilenumber") or ""
            phone = normalize_phone(mobile_raw)
            if not phone:
                skipped += 1
                continue

            message = build_message(row, message_type)

            try:
                resp = sms.send_text_message(
                    DestinationPhoneNumber=phone,
                    OriginationIdentity=ORIGINATION_NUMBER,
                    MessageBody=message,
                    MessageType="TRANSACTIONAL",
                )
                first = (row.get("first name") or "").strip()
                last  = (row.get("last name") or "").strip()
                amount = str(row.get("amount due") or "")

                table.put_item(Item={
                    "phone_number":           phone,
                    "patient_name":           f"{first} {last}".strip(),
                    "amount_due":             amount,
                    "message_type":           message_type,
                    "last_outbound_message":  message,
                    "last_message_id":        resp.get("MessageId", ""),
                    "conversation_history":   json.dumps([{"role": "assistant", "content": message}]),
                    "reply_count":            0,
                    "opt_out":                False,
                })
                sent += 1
            except Exception as e:
                errors.append({"phone": phone, "error": str(e)})

        return _resp(200, {
            "success": True,
            "message": f"SMS sent to {sent} patient(s). {skipped} skipped (no mobile number).",
            "sent": sent,
            "skipped": skipped,
            "errors": errors,
        })

    except Exception as e:
        print("Unexpected error:", e)
        return _resp(500, {"error": "Internal server error"})


def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": CORS_HEADERS, "body": json.dumps(body)}

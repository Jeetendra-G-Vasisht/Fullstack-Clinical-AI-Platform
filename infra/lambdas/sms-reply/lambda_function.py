"""
kbuddhiai-sms-reply
-------------------
Triggered by AWS End User Messaging (Pinpoint) inbound SMS via SNS.
Handles two-way AI conversation with patients using GPT-5.5.

Environment variables:
  DYNAMODB_TABLE     – kbuddhiai-sms-conversations
  ORIGINATION_NUMBER – +18557684735
  OPENROUTER_API_KEY – kept server-side only
"""

import json
import os
import urllib.request

import boto3

DYNAMODB_TABLE     = os.environ.get("DYNAMODB_TABLE", "kbuddhiai-sms-conversations")
ORIGINATION_NUMBER = os.environ.get("ORIGINATION_NUMBER", "")
OPENROUTER_KEY     = os.environ.get("OPENROUTER_API_KEY", "")

sms   = boto3.client("pinpoint-sms-voice-v2", region_name="us-east-1")
ddb   = boto3.resource("dynamodb")
table = ddb.Table(DYNAMODB_TABLE)

SYSTEM_PROMPT = """You are an SMS assistant for a medical billing office. You reply to patients via text message.

STRICT RULES:
1. ONLY answer using facts listed below or the patient's own data. Do NOT guess or make up information.
2. If you don't have the exact answer, say: "For more information, please call us at 480-406-5664."
3. Never speculate about insurance, billing codes, diagnoses, or anything medical.
4. Never confirm, cancel, or change appointments — direct to the phone number.
5. Keep replies SHORT — under 160 characters when possible.
6. Be friendly but do not go beyond what is listed here.

FACTS YOU ARE ALLOWED TO SHARE:
- Office phone: 480-406-5664
- Chandler location Saturday hours: 8 AM to 12 Noon
- For payments, scheduling, or any questions: call 480-406-5664
- The patient's own outstanding balance (provided per patient)

RESPONSE GUIDE:
- Balance question → state their balance, ask them to call to pay
- Saturday / weekend hours → "Our Chandler location is open Sat 8AM–12 Noon. Call 480-406-5664."
- How to pay → "Please call 480-406-5664 to make a payment."
- Schedule appointment → "Please call 480-406-5664 to schedule."
- Anything else → "For more information, please call us at 480-406-5664."

Never say "I believe", "I think", "probably", or anything speculative."""


def call_gpt(messages: list[dict]) -> str:
    payload = json.dumps({
        "model": "openai/gpt-5.5",
        "messages": messages,
        "max_tokens": 200,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://kbuddhiai.com",
            "X-Title": "kBuddhi AI SMS",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"].strip()


def send_sms(phone: str, body: str):
    sms.send_text_message(
        DestinationPhoneNumber=phone,
        OriginationIdentity=ORIGINATION_NUMBER,
        MessageBody=body[:1600],
        MessageType="TRANSACTIONAL",
    )


def lambda_handler(event, context):
    # Unwrap SNS envelope if present
    if "Records" in event:
        record = event["Records"][0]
        try:
            inbound = json.loads(record.get("Sns", {}).get("Message", "{}"))
        except Exception:
            inbound = {}
    else:
        inbound = event

    sender_phone = (inbound.get("originationNumber") or inbound.get("destinationIdentity") or "").strip()
    message_body = (inbound.get("messageBody") or "").strip()

    if not sender_phone or not message_body:
        return {"statusCode": 200, "body": "No phone or message"}

    upper = message_body.upper().strip()

    # Handle opt-out / opt-in commands
    if upper in ("STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"):
        table.update_item(
            Key={"phone_number": sender_phone},
            UpdateExpression="SET opt_out = :t",
            ExpressionAttributeValues={":t": True},
        )
        return {"statusCode": 200, "body": "Opt-out recorded"}

    if upper == "HELP":
        send_sms(sender_phone, "For billing help call 480-406-5664. Reply STOP to opt out.")
        return {"statusCode": 200, "body": "HELP sent"}

    if upper in ("START", "UNSTOP"):
        table.update_item(
            Key={"phone_number": sender_phone},
            UpdateExpression="SET opt_out = :f",
            ExpressionAttributeValues={":f": False},
        )
        send_sms(sender_phone, "You have been re-subscribed. Call 480-406-5664 for help.")
        return {"statusCode": 200, "body": "UNSTOP recorded"}

    # Load patient context
    result = table.get_item(Key={"phone_number": sender_phone})
    item   = result.get("Item", {})

    if item.get("opt_out"):
        return {"statusCode": 200, "body": "Patient opted out"}

    try:
        history = json.loads(item.get("conversation_history") or "[]")
    except Exception:
        history = []

    patient_name = item.get("patient_name", "Patient")
    amount_due   = item.get("amount_due", "")

    system = SYSTEM_PROMPT
    if patient_name:
        system += f"\n\nCurrent patient: {patient_name}."
    if amount_due:
        system += f" Outstanding balance: ${amount_due}."

    messages = [{"role": "system", "content": system}]
    messages += history[-10:]
    messages.append({"role": "user", "content": message_body})

    try:
        reply = call_gpt(messages)
    except Exception as e:
        reply = "Thank you for your message. Please call 480-406-5664 for assistance."
        print(f"GPT error: {e}")

    try:
        send_sms(sender_phone, reply)
    except Exception as e:
        print(f"SMS send error: {e}")
        return {"statusCode": 500, "body": f"SMS send failed: {e}"}

    history.append({"role": "user",      "content": message_body})
    history.append({"role": "assistant", "content": reply})
    history = history[-20:]

    table.update_item(
        Key={"phone_number": sender_phone},
        UpdateExpression="SET conversation_history = :h, last_inbound_message = :m, reply_count = if_not_exists(reply_count, :z) + :one",
        ExpressionAttributeValues={
            ":h":   json.dumps(history),
            ":m":   message_body,
            ":z":   0,
            ":one": 1,
        },
    )

    return {"statusCode": 200, "body": "Reply sent"}

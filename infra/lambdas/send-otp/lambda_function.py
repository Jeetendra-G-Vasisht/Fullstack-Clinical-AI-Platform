"""
send-otp Lambda
---------------
POST /send-otp  {email, password}

1. Validates email+password against Cognito (AdminInitiateAuth).
2. Generates a 6-digit OTP.
3. Stores {email, otp, user_sub, expiry} in DynamoDB with 5-minute TTL.
4. Sends the OTP to the user via SES.

Returns 200 on success, 401 on bad credentials, 403 if email unverified.
"""

import base64
import json
import os
import random
import time

import boto3
from botocore.exceptions import ClientError

REGION         = os.environ.get("AWS_REGION", "us-east-2")
USER_POOL_ID   = os.environ.get("COGNITO_USER_POOL_ID", "")
CLIENT_ID      = os.environ.get("COGNITO_CLIENT_ID", "")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "kbuddhiai-otp-codes")
SES_SENDER     = os.environ.get("SES_SENDER", "noreply@kbuddhiai.com")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://kbuddhiai.com")

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  ALLOWED_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}

OTP_TTL_SECONDS = 300  # 5 minutes


def lambda_handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    try:
        body     = json.loads(event.get("body") or "{}")
        email    = body.get("email", "").strip().lower()
        password = body.get("password", "")

        if not email or not password:
            return _resp(400, {"error": "email and password are required"})

        # ── Step 1: validate credentials with Cognito ─────────────────────────
        cognito = boto3.client("cognito-idp", region_name=REGION)
        try:
            auth_result = cognito.admin_initiate_auth(
                UserPoolId=USER_POOL_ID,
                ClientId=CLIENT_ID,
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": email, "PASSWORD": password},
            )
        except cognito.exceptions.NotAuthorizedException:
            return _resp(401, {"error": "Incorrect email or password."})
        except cognito.exceptions.UserNotFoundException:
            return _resp(401, {"error": "Incorrect email or password."})
        except cognito.exceptions.UserNotConfirmedException:
            return _resp(403, {"error": "Please verify your email before signing in. Check your inbox for the verification code."})
        except ClientError as e:
            print("Cognito ClientError:", e)
            return _resp(401, {"error": "Authentication failed. Please try again."})

        # ── Step 2: extract Cognito sub from the ID token payload ─────────────
        id_token   = auth_result["AuthenticationResult"]["IdToken"]
        payload_b64 = id_token.split(".")[1]
        # JWT base64url — pad to a multiple of 4
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload  = json.loads(base64.urlsafe_b64decode(payload_b64))
        user_sub = payload.get("sub", "")

        # ── Step 3: generate OTP and store in DynamoDB ────────────────────────
        otp    = str(random.randint(100000, 999999))
        expiry = int(time.time()) + OTP_TTL_SECONDS

        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table    = dynamodb.Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "email":    email,
            "otp":      otp,
            "user_sub": user_sub,
            "expiry":   expiry,
        })

        # ── Step 4: send OTP email via SES ────────────────────────────────────
        ses = boto3.client("ses", region_name=REGION)
        try:
            ses.send_email(
                Source=SES_SENDER,
                Destination={"ToAddresses": [email]},
                Message={
                    "Subject": {"Data": "Your kBuddhi AI verification code"},
                    "Body": {
                        "Html": {"Data": _otp_email_html(otp)},
                        "Text": {
                            "Data": (
                                f"Your kBuddhi AI verification code is: {otp}\n\n"
                                f"This code expires in 5 minutes. Do not share it with anyone."
                            ),
                        },
                    },
                },
            )
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "MessageRejected":
                print("SES sandbox rejection for:", email)
                return _resp(503, {"error": "Email delivery is temporarily restricted. Please contact support or try again later."})
            raise

        return _resp(200, {"success": True})

    except Exception as e:
        print("Unexpected error:", e)
        return _resp(500, {"error": "Internal server error"})


def _otp_email_html(otp: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:40px 0;">
    <tr><td align="center">
      <table width="480" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;padding:40px 36px;box-shadow:0 4px 24px rgba(0,0,0,.08);">
        <tr><td>
          <div style="text-align:center;margin-bottom:28px;">
            <div style="display:inline-block;width:48px;height:48px;background:#2563eb;border-radius:12px;line-height:48px;">
              <span style="color:#fff;font-size:22px;">&#128274;</span>
            </div>
          </div>
          <h2 style="color:#111827;font-size:1.4rem;font-weight:700;margin:0 0 8px;text-align:center;">Verification Code</h2>
          <p style="color:#6b7280;font-size:.875rem;text-align:center;margin:0 0 28px;">
            Use this code to complete your sign-in to kBuddhi AI.
          </p>
          <div style="background:#f3f4f6;border-radius:10px;padding:28px;text-align:center;margin-bottom:28px;">
            <span style="font-size:2.8rem;font-weight:700;letter-spacing:12px;color:#111827;">{otp}</span>
          </div>
          <p style="color:#6b7280;font-size:.8rem;text-align:center;margin:0 0 24px;">
            This code expires in <strong>5 minutes</strong>.<br>
            Do not share it with anyone.
          </p>
          <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 20px;">
          <p style="color:#9ca3af;font-size:.72rem;text-align:center;margin:0;">
            <span style="background:#f3f4f6;border-radius:4px;padding:3px 8px;font-weight:500;">&#128274; HIPAA Compliant</span><br><br>
            If you did not attempt to sign in, please ignore this email.<br>
            Your account remains secure.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    CORS_HEADERS,
        "body":       json.dumps(body),
    }

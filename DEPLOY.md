# kBuddhi AI — Deployment Guide

This guide walks through the complete process of deploying the AWS infrastructure
and pointing kbuddhiai.com (currently on GoDaddy) to AWS.

---

## Prerequisites

```bash
# 1. Install AWS CDK globally
npm install -g aws-cdk

# 2. Verify AWS credentials are configured
aws sts get-caller-identity

# 3. Bootstrap CDK in both required regions (run once per account)
cdk bootstrap aws://699092321120/us-east-2
cdk bootstrap aws://699092321120/us-east-1

# 4. Install CDK project dependencies
cd infra
npm install
```

---

## Phase 1 — Deploy the main stack

This deploys everything except the SSL certificate (which requires DNS propagation first).

```bash
cd infra
npm run deploy:main
```

**Expected outputs:**

```
KbuddhiStack.NameServers          = ns-XXX.awsdns-XX.com, ns-XXX.awsdns-XX.net, ...
KbuddhiStack.HostedZoneId         = ZXXXXXXXXXXXXXXXXX
KbuddhiStack.CloudFrontDomain     = XXXXXXXXXXXX.cloudfront.net
KbuddhiStack.ApiGatewayUrl        = https://XXXXXXXXXX.execute-api.us-east-2.amazonaws.com/prod/
KbuddhiStack.CognitoUserPoolId    = us-east-2_XXXXXXXXX
KbuddhiStack.CognitoClientId      = XXXXXXXXXXXXXXXXXXXXXXXXXX
KbuddhiStack.StaticBucketName     = kbuddhiai-static-699092321120
KbuddhiStack.UploadsBucketName    = kbuddhiai-uploads-699092321120
```

> **Save these values.** They are also baked into `config.js` in S3 automatically.

---

## Step 2 — Set the OpenRouter API key on the chat Lambda

The API key is **never** stored in source control. Set it directly on the Lambda:

```bash
aws lambda update-function-configuration \
  --function-name kbuddhiai-chat \
  --region us-east-2 \
  --environment "Variables={
    BUCKET_NAME=kbuddhiai-uploads-699092321120,
    BUCKET_REGION=us-east-2,
    ALLOWED_ORIGIN=https://kbuddhiai.com,
    OPENROUTER_API_KEY=sk-or-v1-YOUR_KEY_HERE
  }"
```

---

## Step 3 — Point Network Solutions DNS to Route 53

1. Log in to [Network Solutions Account Manager](https://www.networksolutions.com/manage-it/index.jsp)
2. In the left sidebar click **Domain Names**, then click on **kbuddhiai.com**
3. On the domain detail page, scroll to the **Name Servers** section and click **Change Where Domain Points**
4. Select **Domain Name Servers (DNS)** (not Web Forwarding or Parking)
5. Click **Continue**
6. Select **I'll use my own name servers**
7. Clear the existing nameserver boxes and enter the 4 Route 53 nameservers one per box:
   ```
   ns-1575.awsdns-04.co.uk
   ns-1043.awsdns-02.org
   ns-120.awsdns-15.com
   ns-967.awsdns-56.net
   ```
   (Add extra boxes if needed — Network Solutions usually provides 4–5 slots)
8. Click **Apply Changes** and confirm any prompt

> **Tip:** Network Solutions may show a warning that custom nameservers will
> disable their DNS management panel — that is expected and correct.

**DNS propagation takes 1–48 hours.** During this time the site is accessible
at the CloudFront domain `dmeqk1urtiqrr.cloudfront.net`.

You can check propagation with:
```bash
dig NS kbuddhiai.com +short
# Should return the four awsdns-XX nameservers when complete
```

---

## Phase 2 — Deploy the SSL certificate

Run this **after** DNS has fully propagated (dig confirms AWS nameservers).

```bash
cd infra
npm run deploy:cert -- --context hostedZoneId=ZXXXXXXXXXXXXXXXXX
```

Get the certificate ARN from the output:
```
KbuddhiCertStack.CertificateArn = arn:aws:acm:us-east-1:699092321120:certificate/XXXXXXXX
```

---

## Phase 3 — Enable kbuddhiai.com on CloudFront

Redeploy the main stack with the cert ARN to attach it to CloudFront and create
the Route 53 A records:

```bash
cd infra
npm run deploy:main -- \
  --context certArn=arn:aws:acm:us-east-1:699092321120:certificate/XXXXXXXX
```

After this deploy, `https://kbuddhiai.com` and `https://www.kbuddhiai.com` will
both serve the portal with a valid SSL certificate.

---

## Step 4 — Request SES production access

SES starts in **sandbox mode** — OTP emails only reach SES-verified addresses.

1. Open **AWS Console → SES (us-east-2) → Account dashboard**
2. Click **Request production access**
3. Fill in the form:
   - **Mail type:** Transactional
   - **Website URL:** https://kbuddhiai.com
   - **Use case description:** HIPAA-compliant healthcare portal. We send 6-digit OTP codes to users during login 2FA and account verification emails. We never send marketing email.
   - **Additional contacts:** your@email.com
4. Submit — AWS typically approves in 24 hours

---

## Updating the site (frontend changes)

After making changes to HTML, CSS, or auth.js:

```bash
cd infra

# If CDK stack outputs haven't changed (most frontend-only deploys):
npm run deploy:main

# This re-synths the CDK app, regenerates config.js with live values,
# and uploads all files to S3 via BucketDeployment (with CloudFront cache invalidation).
```

---

## Verify the deployment

```bash
# 1. Static site loads
curl -I https://kbuddhiai.com

# 2. API Gateway responds
curl -X OPTIONS https://XXXXXXXXXX.execute-api.us-east-2.amazonaws.com/prod/send-otp \
  -H "Origin: https://kbuddhiai.com"

# 3. Cognito User Pool exists
aws cognito-idp describe-user-pool \
  --user-pool-id us-east-2_XXXXXXXXX \
  --region us-east-2

# 4. DynamoDB OTP table exists
aws dynamodb describe-table \
  --table-name kbuddhiai-otp-codes \
  --region us-east-2
```

---

## Tear down (if needed)

```bash
cd infra
npm run destroy
```

> **Note:** The uploads S3 bucket has `removalPolicy: RETAIN` — it will NOT be
> deleted by `cdk destroy` to protect user data. Delete it manually only when safe.

---

## Architecture diagram

```
Browser
  │
  ├── HTTPS ──► CloudFront (kbuddhiai.com)
  │                │
  │                └── S3 (static bucket) — index.html, auth.js, config.js, styles.css
  │
  ├── /send-otp ──────► API Gateway ──► Lambda (send-otp)
  │                                        ├── Cognito (validate password)
  │                                        ├── DynamoDB (store OTP, 5-min TTL)
  │                                        └── SES (send OTP email)
  │
  ├── /verify-otp ─────► API Gateway ──► Lambda (verify-otp)
  │                                        └── DynamoDB (validate OTP)
  │
  ├── /get-upload-url ──► API Gateway ──► Lambda (get-upload-url)
  │                                        └── S3 presigned POST URL
  │                                              (path: uploads/user_id={sub}/year/month/file)
  │
  └── /chat ────────────► API Gateway ──► Lambda (chat)
                                           ├── S3 (read uploaded file)
                                           └── OpenRouter → GPT-4o mini

Route 53 (kbuddhiai.com)
  ├── A → CloudFront
  ├── DKIM CNAMEs → SES (auto-created by CDK)
  └── NS → (4 AWS nameservers — point GoDaddy here)
```

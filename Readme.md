# kBuddhi AI — Cloud-Native Clinical Intelligence Platform

A production-grade, HIPAA-conscious AI portal for healthcare document intelligence. Built entirely on AWS serverless infrastructure using Infrastructure as Code (CDK). Enables clinical staff to upload medical documents, query them with AI, and receive proactive automated alerts based on patient data — all behind multi-factor authentication.

**Live:** [kbuddhiai.com](https://kbuddhiai.com)

---

## What It Does

- Secure 2FA login (password + email OTP) for every user session
- Upload clinical documents (Excel, PDF, Word, CSV, TXT) to encrypted cloud storage
- AI-powered Q&A across one or multiple files simultaneously
- Cross-file reasoning: "Find all patients with HbA1c > 13 across these lab reports"
- Revenue trend analysis and year-end projections from financial data
- Voice-to-text input for hands-free querying
- Automated proactive email alerts (e.g. low appointment volume, critical lab values) — *Phase 2*

---

## Tech Stack

| Layer | Technology |
|---|---|
| IaC / Deployment | AWS CDK v2 (TypeScript) |
| Frontend Hosting | Amazon CloudFront + S3 (HTTPS, global CDN) |
| Authentication | Amazon Cognito User Pool (ADMIN_USER_PASSWORD_AUTH) |
| 2FA / OTP Delivery | AWS Lambda + Amazon SES |
| OTP Storage | Amazon DynamoDB (TTL = 5 minutes) |
| File Storage | Amazon S3 (server-side encryption, private) |
| File Upload | AWS Lambda + API Gateway (pre-signed POST URLs) |
| AI Chat Backend | AWS Lambda + API Gateway + OpenRouter (GPT-4o) |
| Data Processing | AWS Glue + Apache Athena (Phase 2 scaffold) |
| DNS | Amazon Route 53 |
| TLS Certificate | AWS Certificate Manager (us-east-1) |
| Frontend | HTML5, CSS3, Vanilla JavaScript |

---

## System Architecture

```
                         ┌─────────────────────────────────┐
                         │       Route 53 + ACM TLS        │
                         │       kbuddhiai.com              │
                         └────────────┬────────────────────┘
                                      │
                         ┌────────────▼────────────────────┐
                         │         CloudFront CDN          │
                         │   (HTTPS, global edge caching)  │
                         └──────┬─────────────┬────────────┘
                                │             │
               ┌────────────────▼──┐   ┌──────▼──────────────────┐
               │   S3 Static Site  │   │   API Gateway (HTTP)    │
               │  index.html       │   │                          │
               │  register.html    │   │  POST /get-upload-url   │
               │  upload.html      │   │  POST /chat             │
               │  verify.html      │   │  POST /send-otp         │
               │  confirm.html     │   │  POST /verify-otp       │
               │  styles.css       │   └──┬───────────┬──────────┘
               │  auth.js          │      │           │
               └───────────────────┘      │           │
                                          │           │
          ┌───────────────────────────────▼─┐ ┌───────▼──────────────────────────┐
          │         Auth Lambdas            │ │         App Lambdas              │
          │                                 │ │                                  │
          │  kbuddhiai-send-otp             │ │  kbuddhiai-get-upload-url        │
          │  • Cognito credential check     │ │  • Generates pre-signed POST URL │
          │  • 6-digit OTP generation       │ │                                  │
          │  • DynamoDB write (5-min TTL)   │ │  kbuddhiai-chat                  │
          │  • SES email send               │ │  • S3 file fetch + text extract  │
          │                                 │ │  • Single / combined / list mode │
          │  kbuddhiai-verify-otp           │ │  • OpenRouter AI call            │
          │  • DynamoDB OTP lookup          │ └──────────────┬───────────────────┘
          │  • Expiry + match check         │                │
          │  • Session token issue          │                │
          └───────────┬─────────────────────┘                │
                      │                          ┌───────────▼──────────────────┐
          ┌───────────▼──────────────┐           │         Amazon S3            │
          │     Amazon Cognito       │           │  kbuddhiai-uploads-{account} │
          │  User Pool (us-east-2)   │           │  • Private, SSE-S3 encrypted │
          │  • Email/password auth   │           │  • All uploaded files        │
          │  • User registration     │           └──────────────────────────────┘
          │  • Email verification    │
          └──────────────────────────┘
          ┌───────────────────────────┐
          │       Amazon SES         │
          │  noreply@kbuddhiai.com   │
          │  • OTP emails            │
          │  • Transactional only    │
          └───────────────────────────┘
          ┌───────────────────────────┐
          │      Amazon DynamoDB     │
          │  kbuddhiai-otp-codes     │
          │  • TTL-based OTP store   │
          └───────────────────────────┘
```

---

## Authentication Flow

```
1. User enters email + password at index.html
        │
        ▼
2. send-otp Lambda: Cognito AdminInitiateAuth validates credentials
        │  If invalid → 401 Incorrect password
        │  If unconfirmed → 403 → redirect to confirm.html
        ▼
3. 6-digit OTP generated, stored in DynamoDB with 5-minute TTL
        │
        ▼
4. SES sends OTP email from noreply@kbuddhiai.com
        │
        ▼
5. User enters OTP at verify.html
        │
        ▼
6. verify-otp Lambda: fetches DynamoDB record, checks expiry + match
        │  If expired → re-login required
        │  If mismatch → rejected
        ▼
7. Session token returned → user enters upload.html
```

Every API call after login is authorized by the session token. No permanent credentials ever reach the browser.

---

## AI Chat Features

### Single File Chat
Select one document. AI auto-generates a 2–3 sentence summary on open. Full multi-turn conversation with memory — switch files and back, history preserved.

### Combined Answer (All Files)
All selected files merged into one AI context. Single coherent answer across all documents. Use for cross-file questions like:
> *"Find all patients older than 65 and list their highest recorded blood pressure across all reports."*

### Ask Each File
Same question sent to every file simultaneously in parallel. Each file gets its own answer card. Use for comparison:
> *"What was the total revenue this month?"* — answered independently per file.

### Revenue Projection
Upload income data (e.g. Jan–May 2026). Ask:
> *"What would my revenue be at the end of December?"*
AI analyzes the trend and projects the full year. More historical data = more accurate prediction.

### Voice Input
Microphone button on every chat input. Click → speak → transcribed automatically. Uses the browser's built-in Web Speech API — no external service, no extra cost.

---

## File Support

| Format | Extraction |
|---|---|
| `.xlsx` (Excel 2007+) | openpyxl |
| `.xls` (Excel 97-2003) | xlrd |
| `.pdf` | pypdf (pure Python, Lambda-compatible) |
| `.docx` (Word) | python-docx |
| `.csv`, `.txt` | Built-in UTF-8 decode |

Single-file context: **60,000 characters**
Combined mode: **120,000 characters total**, split equally across all files

---

## AWS Infrastructure (CDK)

All infrastructure is defined as code in `infra/` and deployed via `cdk deploy`.

### Stacks

| Stack | Region | Purpose |
|---|---|---|
| `KbuddhiCertStack` | us-east-1 | ACM TLS certificate (CloudFront requires us-east-1) |
| `KbuddhiStack` | us-east-2 | All application resources |

### Resources Deployed

| Resource | Name | Purpose |
|---|---|---|
| S3 Bucket | `kbuddhiai-uploads-{account}-us-east-2` | Encrypted file storage |
| S3 Bucket | `kbuddhiai-site-{account}` | Static website assets |
| CloudFront Distribution | `EVJA4SLTDTVHF` | HTTPS CDN for the frontend |
| Cognito User Pool | `kbuddhiai-users` | User accounts + email verification |
| DynamoDB Table | `kbuddhiai-otp-codes` | OTP storage with 5-min TTL |
| Lambda | `kbuddhiai-send-otp` | Validates login + sends OTP via SES |
| Lambda | `kbuddhiai-verify-otp` | Validates OTP + issues session |
| Lambda | `kbuddhiai-get-upload-url` | Pre-signed S3 upload URLs |
| Lambda | `kbuddhiai-chat` | AI document Q&A |
| API Gateway | HTTP API | All Lambda endpoints |
| Route 53 | `kbuddhiai.com` | DNS with A alias to CloudFront |
| SES Identity | `kbuddhiai.com` | Verified sending domain |
| Glue Job | `kbuddhiai-convert-to-parquet` | Parquet conversion (Phase 2) |
| Glue Database | `kbuddhiai_db` | Athena data catalog (Phase 2) |
| Athena Workgroup | `kbuddhiai-workgroup` | SQL query engine (Phase 2) |
| IAM Role | `HealthLakeReadyRole` | AWS HealthLake scaffold (Phase 2) |

---

## Security Architecture

| Concern | Implementation |
|---|---|
| Password storage | Cognito — hashed, never visible |
| 2FA | 6-digit OTP, 5-minute TTL, stored only in DynamoDB |
| API key (AI) | Lambda environment variable only — never in browser |
| File access | Pre-signed POST URLs (15-minute expiry, scoped per file) |
| S3 files | Block All Public Access — private, SSE-S3 encrypted |
| HTTPS | Enforced by CloudFront — no plain HTTP |
| CORS | Lambda responses restrict requests to `kbuddhiai.com` only |
| Session | Browser sessionStorage — clears on tab close |
| CloudTrail | All S3 + Lambda + Cognito activity logged automatically |

---

## File Structure

```
/
├── index.html              → Sign-in page
├── register.html           → User registration
├── confirm.html            → Email verification (post-registration)
├── verify.html             → OTP entry (2FA step after login)
├── forgot-password.html    → Password reset (two-step)
├── success.html            → Post-login success screen
├── upload.html             → Main portal: upload, browse, AI chat
├── styles.css              → Shared styles for all pages
├── auth.js                 → Cognito auth logic (shared)
├── .env.example            → Environment variable reference
├── .gitignore              → Excludes node_modules, cdk.out, config.js
├── DEPLOY.md               → Deployment runbook
└── infra/
    ├── bin/app.ts          → CDK app entry point (two stacks)
    ├── lib/
    │   ├── kbuddhiai-stack.ts   → Main stack (us-east-2)
    │   └── cert-stack.ts        → ACM certificate stack (us-east-1)
    ├── lambdas/
    │   ├── send-otp/            → OTP generation + SES delivery
    │   ├── verify-otp/          → OTP validation
    │   ├── get-upload-url/      → Pre-signed S3 upload
    │   └── chat/                → AI document Q&A
    ├── glue/
    │   └── convert_to_parquet.py → ETL job (Phase 2)
    ├── package.json
    ├── tsconfig.json
    └── cdk.json
```

---

## Deployment

```bash
cd infra
npm install
npm run build

# Deploy certificate stack first (us-east-1 required for CloudFront)
cdk deploy KbuddhiCertStack --region us-east-1

# Deploy main stack
cdk deploy KbuddhiStack

# After every frontend change: invalidate CloudFront cache
aws cloudfront create-invalidation \
  --distribution-id EVJA4SLTDTVHF \
  --paths "/*"
```

> `config.js` is auto-generated after CDK deploy with live API Gateway URLs. It is excluded from git.

---

## Phase 2 Roadmap

### Proactive Appointment Alerting
Upload doctor schedule + historical patient averages. A scheduled Lambda (EventBridge cron) compares each doctor's upcoming appointments against their daily average. If volume is low, it automatically emails the provider:
> *"Dr. Smith — next Thursday you have 15 patients scheduled. Your daily average is 30. Just an FYI."*

### Critical Lab Value Alerts
Upload lab result files. Lambda detects values outside clinical thresholds (e.g. HbA1c > 13) and alerts the treating physician:
> *"Patient John Doe's HbA1c is 14.75. Would you like me to contact the patient to schedule an appointment?"*

### Revenue Intelligence
Upload monthly income data. AI projects full-year revenue from trend analysis. Scheduled monthly summary reports delivered to practice administrators.

### AWS HealthLake (FHIR R4)
`HealthLakeReadyRole` is already deployed as a scaffold. Full FHIR R4 patient data store with built-in Amazon ML insights — ready to activate.

---

## HIPAA Considerations

| Requirement | Status |
|---|---|
| Access controls | Email + password + 6-digit OTP on every session |
| Transmission security | HTTPS enforced end-to-end (CloudFront + API Gateway) |
| Storage encryption | S3 SSE-S3, DynamoDB encryption at rest |
| Session management | sessionStorage — auto-clears on tab close |
| Audit trail | CloudTrail logs all AWS API calls; Cognito logs all auth events |
| Email transport | Amazon SES — HIPAA-eligible AWS service |
| BAA coverage | AWS BAA available via AWS Artifact for all services used |

---

## Migration History

Migrated from a Firebase + EmailJS + GitHub Pages proof-of-concept to a fully AWS-native production stack:

| Before | After |
|---|---|
| Firebase Authentication | Amazon Cognito |
| EmailJS (browser-side OTP) | Lambda + SES (server-side OTP) |
| GitHub Pages hosting | CloudFront + S3 |
| No IaC | AWS CDK v2 (TypeScript) |
| No DNS management | Route 53 + ACM |
| No data layer | Glue + Athena (Phase 2) |

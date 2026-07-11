# kBuddhi AI — Cloud-Native Clinical Intelligence Platform

A production-grade, HIPAA-conscious AI portal for healthcare document intelligence. Built entirely on AWS serverless infrastructure using Infrastructure as Code (CDK). Enables clinical staff to upload medical documents, query them with AI, and receive proactive automated alerts based on patient data — all behind multi-factor authentication.

**Live:** [kbuddhiai.com](https://kbuddhiai.com)

---

## What It Does

- Secure 2FA login (password + email OTP) for every user session
- Upload clinical documents (Excel, PDF, Word, CSV, TXT) to encrypted cloud storage
- AI-powered Q&A across one or multiple files simultaneously
- **Fast, exact-answer queries on CSV/Excel data** — questions like "how many patients have Triglyceride > 195?" run as real SQL against the file instead of the model reading the whole document, so answers are accurate and take seconds instead of minutes
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
| AI Chat Backend | AWS Lambda (Function URL) + OpenRouter (GPT-5.5) |
| Structured Data Queries | AWS Lambda (Function URL) + DuckDB (embedded SQL engine) + GPT-5.5 |
| Bounce/Complaint Monitoring | Amazon SNS, wired to an SES Configuration Set |
| Data Processing | AWS Glue + Apache Athena (Phase 2 scaffold — provisioned, not currently in the query path) |
| DNS | Amazon Route 53 |
| TLS Certificate | AWS Certificate Manager (us-east-1) |
| Frontend | HTML5, CSS3, Vanilla JavaScript |

> **Why chat and structured queries use a Lambda Function URL instead of API Gateway:** API Gateway enforces a hard, non-configurable 29-second timeout. Large-file AI questions can legitimately take longer than that. Function URLs run to the Lambda's own timeout (5 minutes for chat) with no such ceiling. The `/chat` route still exists on API Gateway for compatibility but the frontend calls the Function URL directly.

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

> **Diagram is a simplified historical sketch.** In practice, `kbuddhiai-chat` and `kbuddhiai-structured-query` are each invoked directly via their own **Lambda Function URL** (not through the API Gateway box shown above) — see "How AI Q&A Actually Works" below for the real request path. `kbuddhiai-structured-query` is a separate Lambda that isn't pictured here.

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

## How AI Q&A Actually Works

There are two separate answer paths, chosen automatically based on file type and whether the fast path succeeds. The user never has to pick — this happens transparently on every question.

```
1. User asks a question about a file in upload.html / dashboard.html
        │
        ▼
2. Is the file CSV, TXT, XLSX, or XLS?
        │                                      │
       YES                                     NO
        │                                      │
        ▼                                      ▼
3. Try kbuddhiai-structured-query      Go straight to kbuddhiai-chat
   (fast path)                         (general path) — see step 6
        │
        ▼
4. Lambda loads the file into an in-memory DuckDB table, reads its
   real column schema + a few sample rows, and asks GPT-5.5 to write
   ONE SQL SELECT query — not an answer, just SQL.
        │
        ▼
5. DuckDB executes that SQL locally inside the Lambda (milliseconds).
   The small result (a few rows/numbers) is sent to GPT-5.5 a second
   time, only to phrase it as a natural-language answer.
        │
        ├─ Succeeded → answer shown to user. Done.
        │
        └─ Failed for any reason (wrong file type, bad SQL, anything)
                │
                ▼
6. Silently fall back to kbuddhiai-chat: the file's raw text (up to
   450,000 characters) is pasted directly into GPT-5.5's context and
   it answers from that instead. Slower, but always available as a
   safety net — this is the original, general-purpose path.
```

**Why this two-path design exists:** pasting an entire file into the model's context on every question is slow (large files can take 1–2+ minutes) and imprecise for exact counts ("how many patients have X > Y") because the model is estimating from raw text, not computing. Routing CSV/Excel questions through real SQL first fixes both problems — answers are typically returned in single-digit seconds and are exact, not estimated — while the original full-text path stays in place for everything else (PDF, Word, JSON, images, and any file the fast path can't handle).

---

## AI Chat Features

### Single File Chat
Select one document. AI auto-generates a 2–3 sentence summary on open. Full multi-turn conversation with memory — switch files and back, history preserved. For CSV/Excel files, this automatically uses the fast SQL path described above; other file types use the general path.

### Structured Query (Fast SQL Path)
For CSV and Excel files specifically, questions like *"how many patients have Triglyceride > 195?"* or *"how many male and female patients are there?"* are answered by generating and running real SQL against the file's actual data — not by the model reading and estimating from raw text. Returns exact counts in a few seconds. Falls back automatically to the general chat path if the question or file doesn't fit (multi-turn conversational context, like "and what about *his* cholesterol?", isn't available on this fast path yet — see Known Limitations).

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

| Format | Extraction | Answered via |
|---|---|---|
| `.csv`, `.txt` | Tolerant UTF-8 decode, then loaded into DuckDB | Structured query (fast path), falls back to chat |
| `.xlsx` (Excel 2007+) | openpyxl, normalized to CSV for DuckDB | Structured query (fast path), falls back to chat |
| `.xls` (Excel 97-2003) | xlrd | General chat only |
| `.pdf` | pypdf (pure Python, Lambda-compatible) | General chat only |
| `.docx` (Word) | python-docx | General chat only |

**General chat path limits:** single-file context up to **450,000 characters**; combined mode splits a **450,000 character total budget** evenly across all selected files (minimum 60,000 characters each). Raised from much lower original limits after large files were found to truncate mid-document — see Known Limitations for what this trades off.

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
| S3 Bucket | `kbuddhiai-uploads-{account}` | Encrypted file storage |
| S3 Bucket | `kbuddhiai-static-{account}` | Static website assets |
| CloudFront Distribution | `EVJA4SLTDTVHF` | HTTPS CDN for the frontend |
| Cognito User Pool | `kbuddhiai-users` | User accounts + email verification |
| DynamoDB Table | `kbuddhiai-otp-codes` | OTP storage with 5-min TTL |
| Lambda | `kbuddhiai-send-otp` | Validates login + sends OTP via SES |
| Lambda | `kbuddhiai-verify-otp` | Validates OTP + issues session |
| Lambda | `kbuddhiai-get-upload-url` | Pre-signed S3 upload URLs |
| Lambda | `kbuddhiai-chat` | General-purpose AI document Q&A; own Function URL + legacy API Gateway route |
| Lambda | `kbuddhiai-structured-query` | Fast DuckDB SQL Q&A for CSV/Excel; own Function URL, dedicated read-only IAM role |
| API Gateway | REST API (`kbuddhiai-api`) | `/send-otp`, `/verify-otp`, `/get-upload-url`, legacy `/chat` route |
| Route 53 | `kbuddhiai.com` | DNS with A alias to CloudFront |
| SES Identity | `kbuddhiai.com` | Verified sending domain, DKIM + custom MAIL FROM configured |
| SES Configuration Set | `kbuddhiai-config-set` | Routes bounce/complaint events to SNS; reputation metrics enabled |
| SNS Topic | `kbuddhiai-ses-notifications` | Bounce/complaint alerts — required evidence for AWS SES production-access review |
| Glue Job | `kbuddhiai-convert-to-parquet` | Parquet conversion — **provisioned but never triggered**; not part of the live query path |
| Glue Database | `kbuddhiai_data` | Athena data catalog — provisioned, unused |
| Athena Workgroup | `kbuddhiai` | Provisioned, unused — structured queries use in-Lambda DuckDB instead, see Known Limitations |
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
├── upload.html             → Upload portal: upload, browse, AI chat
├── dashboard.html          → 5-tab dashboard (main app shell; Coming Soon panels for future tabs)
├── styles.css              → Shared styles for all pages
├── auth.js                 → Cognito auth logic (shared)
├── arizonauro/             → Full mirror of the above, served under /arizonauro for the
│                              Arizona Urology subdomain deployment — same config.js-driven
│                              backend, kept in sync manually with the root pages
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
    │   ├── chat/                → General-purpose AI document Q&A (full-text path)
    │   ├── structured-query/    → Fast DuckDB SQL Q&A for CSV/Excel files
    │   ├── sms-send/            → Outbound SMS (two-way patient outreach)
    │   └── sms-reply/           → Inbound SMS webhook handler
    ├── glue/
    │   └── convert_to_parquet.py → ETL job — provisioned, not currently triggered by anything
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

### Migrate LLM calls to Amazon Bedrock (Claude)
Highest-priority near-term item. Move `kbuddhiai-chat` and `kbuddhiai-structured-query` off OpenRouter/GPT-5.5 onto Claude via Amazon Bedrock, closing the BAA gap noted above. Requires enabling Claude model access in Bedrock for the account/region and rewriting the LLM call from OpenRouter's OpenAI-compatible format to Bedrock's Converse API — a real code migration, not a config toggle. Roughly cost-neutral versus OpenRouter; the motivation is compliance, not cost.

### Native Cognito email OTP for login
Registration already sends its verification email through Cognito's own built-in email service, unaffected by SES sandbox limits. Login OTP currently goes through a custom `send-otp` Lambda + DynamoDB + SES, which is blocked by SES sandbox for any email not manually pre-verified. Switching login to Cognito's native email MFA reuses the same proven-working pathway as registration, removes a custom Lambda/DynamoDB layer, and unblocks new-user login immediately without waiting on SES production access (though SES access is still needed for real production volume — Cognito's built-in email has a low daily cap suited to early testing, not scale).

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

## Known Limitations

- **LLM provider is not yet BAA-covered.** See the HIPAA Considerations table below — this is the top-priority open item, not yet fixed.
- **Structured query has no conversation memory.** The fast SQL path (`kbuddhiai-structured-query`) answers each question independently; it doesn't receive prior chat turns. A follow-up like "and what about *his* cholesterol?" right after asking about a specific patient may come back ambiguous, since the pronoun has no context to resolve against. It degrades safely (asks for clarification or falls back to general chat) rather than answering incorrectly, but it's not a true multi-turn agent yet.
- **Structured query only supports single-file, tabular questions.** It can't currently join across multiple uploaded files (e.g. "patients on Metformin AND diagnosed with diabetes" spanning a medications file and a diagnosis file) — that still requires the general chat path's Combined Answer mode, which pastes multiple files into context rather than running a real join.
- **Structured query needs a date/time column to answer trend questions.** Longitudinal reasoning ("has this patient's kidney function declined over their last 3 visits?") only works if the file actually has a date/visit column — verified against the current demo lab-results data that it does **not**, so this class of question isn't answerable yet with that specific file.
- **Glue/Athena are provisioned but unused.** Early planning assumed Parquet conversion (Glue) + Athena SQL would power structured queries. In practice the Glue trigger was never wired to any Lambda, so those resources sit idle; `kbuddhiai-structured-query`'s in-Lambda DuckDB engine does this job instead, with no dependency on Glue/Athena at all. Worth revisiting Athena only if data volume grows well past what a single Lambda invocation can hold in memory.
- **General chat path truncates very large files.** Even at the current 450,000-character cap, an unusually large upload could still exceed it. The structured-query fast path doesn't have this limit for the questions it can answer, since it queries the file rather than pasting all of it into the model's context.

---

## HIPAA Considerations

| Requirement | Status |
|---|---|
| Access controls | Email + password + 6-digit OTP on every session |
| Transmission security | HTTPS enforced end-to-end (CloudFront + API Gateway) |
| Storage encryption | S3 SSE-S3, DynamoDB encryption at rest |
| Session management | sessionStorage — auto-clears on tab close |
| Audit trail | CloudTrail logs all AWS API calls; Cognito logs all auth events |
| Email transport | Amazon SES (HIPAA-eligible) — still in sandbox, awaiting AWS production-access approval; automated bounce/complaint monitoring via SNS is now in place as part of that review |
| BAA coverage — AWS services | AWS BAA (via AWS Artifact) covers S3, Lambda, Cognito, DynamoDB, SES, SNS — all AWS services this app uses |
| **BAA coverage — LLM provider** | **Gap:** `kbuddhiai-chat` and `kbuddhiai-structured-query` currently call GPT-5.5 through **OpenRouter**, a third party **not** covered by the AWS BAA. Patient data (file contents / query results) is sent there today. Planned fix: migrate both Lambdas to call Claude via **Amazon Bedrock**, which *is* covered under the AWS BAA — see Phase 2 Roadmap. |

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

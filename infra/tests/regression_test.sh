#!/usr/bin/env bash
# kBuddhi AI — regression test suite for kbuddhiai-chat and
# kbuddhiai-structured-query.
#
# WHEN TO RUN: after ANY change to either Lambda's code, prompts, IAM,
# model ID, or the CDK resources backing them (env vars, Function URLs,
# timeouts) — BEFORE deploying. Every test here reproduces a real
# question/answer pair or a real bug hit and fixed during development.
# If a test fails, treat it as a regression, not a fluke, and do not
# deploy until it's understood.
#
# Each test is deliberately distinct — different file, different SQL
# shape (COUNT / WHERE / ORDER BY-LIMIT / GROUP BY), different failure
# mode (crash / truncation / wrong routing / duplicate headers) — so a
# pass on one never stands in for another.
#
# Usage:
#   ./regression_test.sh
#   CHAT_URL=... SQ_URL=... ./regression_test.sh   (point at a different deploy)
#
# Exit code 0 = everything passed, safe to deploy.
# Exit code 1 = at least one regression — do not deploy.

set -uo pipefail

CHAT_URL="${CHAT_URL:-https://6slufv4ojjtztlczep5tgznh5y0nivbl.lambda-url.us-east-2.on.aws/}"
SQ_URL="${SQ_URL:-https://wmxext4tqy7a474tqpvyecrjtm0jrkta.lambda-url.us-east-2.on.aws/}"
ORIGIN="${ORIGIN:-https://kbuddhiai.com}"

USER_SUB="d19bb5a0-d0c1-70ea-6977-1019aa7e9c73"
PREFIX7="uploads/user_id=$USER_SUB/year=2026/month=07"
PREFIX6="uploads/user_id=$USER_SUB/year=2026/month=06"

KEY_LAB="$PREFIX7/Lab_Results_CSV.csv"      # 2,000-row CSV, the primary demo file
KEY_PAT="$PREFIX7/Patients.xlsx"            # Excel, 726 patients, no lab columns
KEY_DIAG="$PREFIX7/Diagnosis.xlsx"
KEY_MED="$PREFIX7/Medications.xlsx"
KEY_FHIR="$PREFIX7/fhir_patients.json"      # non-tabular, 100 patient records
KEY_PDF="$PREFIX6/Background_information.pdf"

PASS=0
FAIL=0
FAILED_TESTS=()

# ── JSON payload builders — all argument-based (sys.argv), never string-
# interpolated into python source, so file paths/questions with quotes,
# parentheses, etc. can never break the call. ─────────────────────────────

json_single() {
  # json_single <s3_key> <question>
  python3 -c "import json,sys; print(json.dumps({'s3_key': sys.argv[1], 'question': sys.argv[2]}))" "$1" "$2"
}

json_single_chat() {
  # json_single_chat <s3_key> <question>
  python3 -c "import json,sys; print(json.dumps({'s3_key': sys.argv[1], 'question': sys.argv[2], 'chat_history': []}))" "$1" "$2"
}

json_followup() {
  # json_followup <s3_key> <first_question> <first_answer> <followup_question>
  python3 -c "
import json, sys
s3_key, q1, a1, q2 = sys.argv[1:5]
print(json.dumps({
    's3_key': s3_key,
    'question': q2,
    'chat_history': [{'role': 'user', 'content': q1}, {'role': 'assistant', 'content': a1}],
}))
" "$1" "$2" "$3" "$4"
}

json_combined() {
  # json_combined <question> <key1> [key2] [key3] ...
  python3 -c "
import json, sys
question = sys.argv[1]
keys = sys.argv[2:]
print(json.dumps({'s3_keys': keys, 'question': question, 'chat_history': []}))
" "$@"
}

json_field() {
  # json_field <json> <field>  — empty string if missing/unparseable
  python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get(sys.argv[2], '') or '')
except Exception:
    print('')
" "$1" "$2" 2>/dev/null
}

post() {
  # post <url> <json_body>
  curl -s -X POST "$1" -H "Content-Type: application/json" -d "$2"
}

pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); FAILED_TESTS+=("$1"); }

assert_contains() {
  # assert_contains <test-name> <json> <field> <expected-substring>
  local name="$1" json="$2" field="$3" expected="$4"
  local actual; actual=$(json_field "$json" "$field")
  if echo "$actual" | grep -qi "$expected"; then
    pass "$name"
  else
    fail "$name — expected '$expected' in .$field, got: ${actual:0:200}"
  fi
}

assert_clean_error() {
  # assert_clean_error <test-name> <json>  — expects a proper {"error": ...}, not a crash
  local name="$1" json="$2"
  local err; err=$(json_field "$json" "error")
  if [ -n "$err" ] && ! echo "$err" | grep -qi "NoneType\|Traceback\|internal server error"; then
    pass "$name (cleanly rejected: ${err:0:80})"
  else
    fail "$name — expected a clean rejection, got: ${json:0:200}"
  fi
}

assert_no_error() {
  # assert_no_error <test-name> <json>
  local name="$1" json="$2"
  local err; err=$(json_field "$json" "error")
  if [ -z "$err" ]; then
    pass "$name"
  else
    fail "$name — unexpected error: $err"
  fi
}

echo "===================================================="
echo "kBuddhi AI — regression test suite"
echo "chat:             $CHAT_URL"
echo "structured-query: $SQ_URL"
echo "===================================================="

# ---- 1. Infra sanity ---------------------------------------------------
echo; echo "1. Basic connectivity"
R=$(post "$CHAT_URL" '{"action":"list_files"}')
assert_no_error "list_files responds without error" "$R"

# ---- 2. Structured-query correctness (CSV, 2,000 rows) -----------------
echo; echo "2. Structured-query — Lab_Results_CSV.csv (COUNT / WHERE, multiple operators)"
R=$(post "$SQ_URL" "$(json_single "$KEY_LAB" "How many total rows are in this file?")")
assert_contains "exact row count (2,000)" "$R" answer "2,000\|2000"

R=$(post "$SQ_URL" "$(json_single "$KEY_LAB" "How many patients have Triglyceride > 195?")")
assert_contains "Triglyceride > 195 → 4" "$R" answer "4"

R=$(post "$SQ_URL" "$(json_single "$KEY_LAB" "How many patients have Triglyceride < 150?")")
assert_contains "Triglyceride < 150 → 61 (different operator)" "$R" answer "61"

R=$(post "$SQ_URL" "$(json_single "$KEY_LAB" "How many patients have Calcium > 8?")")
assert_contains "Calcium > 8 → 69 (different column entirely)" "$R" answer "69"

R=$(post "$SQ_URL" "$(json_single "$KEY_LAB" "List the top 3 highest Triglyceride values with patient names.")")
assert_contains "ORDER BY + LIMIT query shape works" "$R" answer "Grazyna Considine"

# ---- 3. Structured-query — previously-crashing edge case ---------------
echo; echo "3. Structured-query — schema/meta question (used to crash, then failed retry)"
R=$(post "$SQ_URL" "$(json_single "$KEY_LAB" "What columns does this data have, and does it include dates for multiple visits per patient?")")
assert_no_error "schema question answers cleanly, no crash" "$R"
assert_contains "correctly reports no date column exists" "$R" answer "no date\|not.*date\|does not include"

# ---- 4. Structured-query — different file type (Excel, GROUP BY) -------
echo; echo "4. Structured-query — Patients.xlsx (Excel, GROUP BY)"
R=$(post "$SQ_URL" "$(json_single "$KEY_PAT" "How many male and female patients are there?")")
assert_contains "gender breakdown → 387 male" "$R" answer "387"

# ---- 5. Structured-query — correct rejection, not a crash ---------------
echo; echo "5. Structured-query — out-of-scope requests fail cleanly"
R=$(post "$SQ_URL" "$(json_single "$KEY_FHIR" "anything")")
assert_clean_error "non-CSV/Excel file (.json) rejected, not crashed" "$R"

R=$(post "$SQ_URL" "$(json_single "$KEY_PAT" "How many patients have Triglyceride > 195?")")
assert_clean_error "question referencing a column the file doesn't have" "$R"

# ---- 6. General chat — non-tabular files --------------------------------
echo; echo "6. General chat — non-tabular files (PDF, JSON)"
R=$(post "$CHAT_URL" "$(json_single_chat "$KEY_PDF" "Summarize this in one sentence.")")
assert_no_error "PDF summary answers without error" "$R"

R=$(post "$CHAT_URL" "$(json_single_chat "$KEY_FHIR" "How many patient records are in this file?")")
assert_contains "FHIR JSON record count → 100" "$R" answer "100"

# ---- 7. Multi-turn conversation -----------------------------------------
echo; echo "7. Multi-turn conversation (chat_history is respected)"
Q1="What test types are in this file?"
T1=$(post "$CHAT_URL" "$(json_single_chat "$KEY_LAB" "$Q1")")
A1=$(json_field "$T1" answer)
if [ -n "$A1" ]; then
  T2=$(post "$CHAT_URL" "$(json_followup "$KEY_LAB" "$Q1" "$A1" "Which one did you list first?")")
  assert_no_error "follow-up question using prior turn answers cleanly" "$T2"
else
  fail "multi-turn conversation — first turn produced no answer at all"
fi

# ---- 8. Combined Answer — the exact truncation bug found in production --
echo; echo "8. Combined Answer — multi-file budget (the exact bug your lead hit)"
R=$(post "$CHAT_URL" "$(json_combined "How many patients have triglyceride > 195" "$KEY_LAB" "$KEY_FHIR")")
assert_contains "Combined Answer, 2 files → 4" "$R" answer "4"

R=$(post "$CHAT_URL" "$(json_combined "How many patients have triglyceride > 195" "$KEY_PAT" "$KEY_DIAG" "$KEY_MED" "$KEY_LAB")")
assert_contains "Combined Answer, 4 files → 4 (the reported bug)" "$R" answer "4"
NOTE=$(json_field "$R" answer)
if echo "$NOTE" | grep -qi "truncat"; then
  fail "Combined Answer, 4 files — response mentions truncation (budget regressed)"
fi

# ---- 9. "Ask Each File" — per-file fast-path routing ---------------------
echo; echo "9. Ask Each File — routes each file through structured-query first"
R=$(post "$SQ_URL" "$(json_single "$KEY_LAB" "How many patients have triglyceride > 195")")
assert_contains "per-file structured-query on Lab file → 4" "$R" answer "4"

# ---- 10. CORS — the duplicate-header bug that broke browser fetches -----
echo; echo "10. CORS — single Access-Control-Allow-Origin header (Function URL)"
HEADERS=$(curl -s -i -X POST "$SQ_URL" -H "Content-Type: application/json" -H "Origin: $ORIGIN" \
  -d "$(json_single "$KEY_LAB" "How many total rows are in this file?")")
CORS_COUNT=$(echo "$HEADERS" | grep -ci "^access-control-allow-origin:")
if [ "$CORS_COUNT" -eq 1 ]; then
  pass "exactly one Access-Control-Allow-Origin header ($CORS_COUNT)"
else
  fail "expected exactly 1 Access-Control-Allow-Origin header, found $CORS_COUNT — browsers will reject this response"
fi

# ---- Summary --------------------------------------------------------------
echo
echo "===================================================="
echo "RESULTS: $PASS passed, $FAIL failed"
echo "===================================================="
if [ "$FAIL" -gt 0 ]; then
  echo "FAILED:"
  for t in "${FAILED_TESTS[@]}"; do echo "  - $t"; done
  echo
  echo "DO NOT DEPLOY until these are understood and fixed."
  exit 1
fi
echo "All tests passed — safe to deploy."
exit 0

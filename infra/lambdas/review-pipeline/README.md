# review-pipeline

A multi-agent review/matching pipeline built on [LangGraph](https://github.com/langchain-ai/langgraph), exposed both as a Lambda Function URL and as an [MCP](https://modelcontextprotocol.io) server so it can be invoked identically regardless of caller.

Rebuilt from scratch on `feature/multi-agent-review-pipeline`; this is not wired into `infra/lib/kbuddhiai-stack.ts` and nothing here touches AWS -- see [Deployment status](#deployment-status).

## What it does

Given one **document record** (fields extracted from an uploaded document -- name, DOB, MRN) and a pool of **candidate patient records**, the pipeline decides which candidate the document belongs to, or whether it shouldn't be auto-matched at all. This is the record-linkage/deduplication problem clinical intake and document-matching systems actually face: an uploaded file's extracted metadata rarely lines up perfectly with the record it belongs to (typos, missing fields, transcription errors), and sometimes it doesn't belong to anyone in the pool.

Every worker returns a score *and* a confidence, and a **self-check gate** means the pipeline never silently guesses on a case it isn't sure about -- it escalates to `needs_human_review` instead.

## Architecture

```
                    supervisor_decompose
                 (splits into 3 field subtasks)
                             |
                             v
                      name_matcher
                             |
                             v
                      dob_matcher            specialized workers --
                             |                each scores document vs.
                             v                EVERY candidate on ONE
                   identifier_matcher         field, returns its own
                             |                confidence
                             v
                   supervisor_aggregate
             (ranks candidates, one overall confidence)
                             |
                             v
                  route_after_aggregate  <-- the self-check gate
                    /         |         \
                   /          |          \
        resolve_no_match  needs_human_review  resolve_match
        (score too low     (confidence too      (auto-resolved)
         for ANY match)    low to auto-resolve)
```

- **Supervisor** (`supervisor_decompose` / `supervisor_aggregate`): decomposes the task into one subtask per identifying field, then combines worker output into a ranked candidate list and one overall confidence.
- **Workers** (`name_matcher`, `dob_matcher`, `identifier_matcher`): each is a specialist over exactly one field, scored deterministically (see [Why deterministic workers](#why-deterministic-workers-not-llm-calls)). Missing/unparseable data on either side makes a worker abstain for that field rather than guess.
- **Self-check gate** (`route_after_aggregate`, [pipeline/nodes.py](pipeline/nodes.py)): a case is auto-resolved only if the best candidate clears both a minimum match-score floor *and* a minimum overall-confidence threshold. Below the confidence threshold, the case is **not** guessed -- it's routed to `needs_human_review` with the pipeline's best guess attached for triage, not committed to.
- **Structured tracing** ([pipeline/logging_utils.py](pipeline/logging_utils.py)): every node appends a step to `state["trace"]` (per-stage, per-field scores and confidences). The final result also carries a `weakest_link` -- the field whose evidence was least usable -- so a wrong answer can be traced back to the exact worker/field that caused it without re-deriving anything from the raw output.

Full field/threshold/weight constants live in [pipeline/nodes.py](pipeline/nodes.py).

### Why deterministic workers, not LLM calls

The three field workers use stdlib-only similarity scoring (`difflib`, `datetime`), not LLM calls -- name/DOB/MRN agreement is exactly the kind of structured comparison that classic record-linkage scoring (Fellegi-Sunter-style fixed field-reliability weights + observed agreement) already does well and deterministically. This also means the pipeline and its eval harness run for free, offline, and give the exact same answer on every run -- no API key, no AWS, no per-invocation token cost. `pipeline/matchers.py` is the one place worker logic lives, so swapping in an LLM-backed matcher for a fuzzier field later is a contained change, not a rewrite.

## MCP interface

[mcp_server.py](mcp_server.py) defines the tool interface explicitly with [FastMCP](https://github.com/modelcontextprotocol/python-sdk) rather than wrapping the Lambda handler ad hoc:

| Tool | Purpose |
|---|---|
| `review_match(document, candidates, gate_enabled=True)` | Runs the full pipeline, returns `{"final": ..., "trace": [...]}`. |
| `list_review_thresholds()` | Returns the pipeline's tunable constants (field weights, gate threshold, no-match floor) so a caller can reason about a decision without hardcoding pipeline internals. |

Both `mcp_server.py` and `lambda_function.py` call the same `pipeline.run_review()` -- an MCP client and a Function URL caller get identical pipeline behavior by construction, not by convention.

```bash
python mcp_server.py                          # stdio transport (default)
python mcp_server.py --transport sse --port 8080
mcp dev mcp_server.py                          # MCP Inspector, for local testing
```

## Evaluation

[eval/run_eval.py](eval/run_eval.py) runs [eval/dataset.jsonl](eval/dataset.jsonl) -- 60 synthetic, seeded document-to-patient-record matching cases (fabricated names/DOBs/MRNs, generated by [eval/generate_dataset.py](eval/generate_dataset.py); no real patient data) -- through the pipeline twice:

- **Baseline** (`gate_enabled=False`): the pipeline commits to its best-scoring candidate on every case, however weak the evidence -- it never abstains and never escalates.
- **Gated** (`gate_enabled=True`): the self-check gate is active; low-confidence cases are routed to `needs_human_review` instead of guessed.

The dataset has four categories: `clean` matches, `noisy_correct` matches (one field corrupted but the other two still agree), `ambiguous` cases (a genuine near-duplicate candidate pair plus a document missing enough evidence to safely tell them apart), and `no_match` cases (the document belongs to no one in its pool).

### Measured results (this commit, `eval/dataset.jsonl`, no LLM/API calls)

| | Baseline (no gate) | Gated (self-check on) |
|---|---|---|
| Accuracy | **73.3%** (44/60) | **100.0%** on the 45/60 cases it auto-resolved |
| Escalated to human review | 0% (never abstains) | **25.0%** (15/60) |

Of the 15 escalated cases, 40.0% would have been *wrong* had the pipeline been forced to guess -- the gate is escalating genuinely hard cases, not firing at random. Baseline's errors are concentrated exactly where you'd expect: it gets 0/10 `no_match` cases right (it never says "no match," so every true no-match is scored as a false match) and 9/15 `ambiguous` cases right (essentially a coin flip on cases specifically constructed to be genuinely confusable); it's perfect on `clean` (20/20) and `noisy_correct` (15/15), where the evidence is actually strong enough to guess correctly.

These numbers are exactly what `run_eval.py` measures against the committed dataset -- not a target. Re-run it yourself:

```bash
pip install -r requirements.txt
python eval/run_eval.py
```

Full per-case output (every case, expected vs. actual, confidence, status) is written to `eval/results.json`.

### Reading the numbers honestly

This is a 60-case synthetic proxy task with a hand-tuned confidence threshold (0.70) and no-match floor (0.35) in [pipeline/nodes.py](pipeline/nodes.py) -- it demonstrates the *mechanism* (gating trades recall for precision by design, and targets the right cases when it does), not a claim about accuracy on a production clinical dataset with real OCR noise, real duplicate-name collision rates, or a different confidence distribution. The dataset's category mix (25% ambiguous, 17% no-match) was chosen to stress the gate, not to mirror any measured real-world case distribution.

## Deployment status

`lambda_function.py` follows the existing `chat`/`structured-query` Lambda conventions (Function URL only, no API Gateway route, same request/response/CORS shape) so it's ready to package the same way -- but it is **intentionally not added to `infra/lib/kbuddhiai-stack.ts`**, has no IAM role, and has not been deployed. This rebuild only touches this module's own directory; wiring it into the CDK stack is left as a deliberate next step, not an oversight.

## Layout

```
review-pipeline/
├── pipeline/
│   ├── state.py          # LangGraph state schema (TypedDict)
│   ├── matchers.py        # deterministic field-level similarity scorers
│   ├── nodes.py            # supervisor + worker node implementations, the gate
│   ├── graph.py             # StateGraph wiring + run_review() entry point
│   └── logging_utils.py      # structured per-stage trace helper
├── lambda_function.py    # Function URL handler wrapping pipeline.run_review()
├── mcp_server.py          # MCP tool interface wrapping the same pipeline.run_review()
├── requirements.txt
├── eval/
│   ├── generate_dataset.py  # deterministic dataset generator
│   ├── dataset.jsonl         # committed 60-case labeled validation set
│   ├── run_eval.py            # baseline-vs-gated harness
│   └── results.json            # full per-case output from the last run
└── README.md
```

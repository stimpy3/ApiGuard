# Changelog

Semver tags mean nothing to a consumer without this file: someone pinning
`:1` needs to know whether `1.1` is safe to pick up.

## [Unreleased]

### Less setup

- `api-guard init`: detects the spec, the framework (FastAPI, Django REST
  framework + drf-spectacular; Spring and NestJS with guidance), the default
  branch and the CI, then writes `api-guard.yaml`, an empty commented
  `waivers.yaml`, the GitHub workflow and `.gitignore` lines. For Jenkins it
  prints a stage to paste. Never overwrites without `--force`; `--dry-run`
  shows what it would write.
- No config needed to start: without `api-guard.yaml`, `check` finds the spec
  in the usual places and compares it with the default branch.
- One headline per run: `API contract: OK`, `BLOCKED - …` or `COULD NOT
  CHECK`, then the checks that ran, and one "not checked" line for the rest.
- Freshness compares the parsed spec, not bytes: JSON from a generator matches
  committed YAML, and key order or line endings no longer cause failures. The
  failure lists where the documents differ.
- A spec generator that can't start because the project's packages aren't
  installed (the Docker image on a laptop) makes freshness "not checked here"
  instead of a tooling error.

### Shared model layer and a sturdier agent

- New `ai/llm.py`: every model call goes through it. A model per job
  (triage, explain, agent), the key loaded once, one copy of the project's
  rules for every prompt (three copies had drifted), and JSON-schema mode for
  the gpt-oss models.
- Rate limits: after the client's retries, a call is tried once on the other
  Groq model (same free key, separate per-model limit), then falls back to the
  rule-based facts.
- Smaller prompts: repeated changes merged, at most 8 sent, fingerprints left
  out, and each prompt trimmed to a token budget before sending.
- Agent loop: at most 12 tool calls in total (not just 8 rounds), a repeated
  call with the same inputs is not run again, and older tool results are
  shortened once the conversation passes ~6000 tokens.
- `AI_PROVIDER` setting (only `groq` implemented; anything else turns AI off).
  Tests set it to `off`, so a real key in `.env` is never spent by a test run.

### One review workflow

- `--explain` is now a step inside the review graph
  (evidence → triage → explain → decide → human), so the report and the
  approver see the same explanation. `check --explain` runs the graph without
  the human step.
- Triage picks the explanation's model: the small one for routine changes,
  the larger one for risky or unclassified ones. The band still never reaches
  a decision.
- The approval question now carries the full context: commit and branch, every
  change with its fingerprint, the risk label with its reason, what breaks and
  the safer route. It is saved in the checkpoint, so a late approver sees
  exactly what was generated at build time.
- Shared rules now also say a required request field breaks clients whenever
  it's introduced.

### Reviews after the fact

- `review --build N [--job ...]` runs the review workflow on an archived
  Jenkins build, with its `result.json` as the evidence (McpEvidence, unused
  until now). The review id is `build-N`.
- Two new read-only MCP tools, `list_pending_reviews` and `get_review`, so
  our agent can answer "what's waiting for approval, and why?". They report
  decisions; they cannot make one.

### Deciding from Jenkins, the terminal or the browser

- `review list` shows every saved review, waiting ones first; `review show ID`
  prints its audit trail from the saved checkpoints (evidence, the model's
  label and explanation, every question and answer, the decision, who, when).
- The sample Jenkinsfile's approval is one form with a decision dropdown
  (approve / reject / question), NAME, TEXT and EXPIRES_IN, looping until a
  decision. A question runs `ask-review` and shows the answer in the next
  form; input api-guard refuses brings the form back instead of failing the
  build; Abort or the timeout is recorded as a rejection.
- `api-guard ui` gains a "Pending reviews" tab with approve / reject / ask
  buttons, for CI systems that cannot pause a job.

### Approve, reject or ask

- A paused review now has three ways forward, each a separate command that
  resumes the saved graph:
  - `approve ID --by --reason [--expires-in 30]`: this build ships, and
    `review.md` carries waiver entries for exactly the blocking fingerprints,
    validated like a hand-written waiver, ready to commit so the next build
    passes without another approval. `--reason` is now required.
  - `reject ID --by --reason`: records who and why, with a fix checklist.
  - `ask-review ID "question"`: our agent answers from the review's saved
    facts (tools only if needed); the review pauses again with the answer
    shown. At most 5 questions per review.
- Every question and answer is kept in the checkpoint and in `review.md`.
- The agent's inner loop is never checkpointed, so it can run inside the
  review graph.

### Waivers

- Expired waivers are **ignored with a warning** instead of failing the build.
  The change they covered counts again, and every report lists them under
  "Expired waivers - remove them". `result.json` gains `waivers.expired`
  (schema 1.1).
- New `policy.max_waiver_days` (default 90): a waiver expiring further ahead
  is refused (exit 2), so waivers can't quietly become permanent.

### Other

- `--base` overrides `spec.base` for one run. The sample Jenkinsfile uses it on
  `main`, where `origin/main` is the build's own commit and the breaking check
  could never find anything; it now compares with the last successful commit.

- The LangGraph `classify_severity` node now uses `openai/gpt-oss-20b` by
  default, set with the new `GROQ_CLASSIFY_MODEL` variable. A routine/risky label
  does not need the large model. `GROQ_MODEL` (still `gpt-oss-120b`) now applies
  only to `--explain`.
- `api-guard review` runs the checks and then the approval workflow. A blocked
  build pauses and saves its state to SQLite (`.api-guard/reviews.db`), and
  `api-guard approve <id> --by <name>` resumes it from any later process. The
  exit code of `review` is the same as `check`; approval records sign-off in
  `review.md` and does not rewrite `result.json`.
- `api-guard ask "<question>"`: Groq investigates past builds using the MCP
  server's read-only tools, choosing which to call and iterating until it can
  answer. Uses `openai/gpt-oss-20b` by default (`GROQ_AGENT_MODEL`). Adds
  `langchain-mcp-adapters` to the `ai` extra, which requires `mcp<2`.
- `ask` answers carry the model's confidence and reason, a caution label, and
  computed warnings: identifiers not found in the tool output, tool errors, or
  no build data read. The model is told to mark inference as inference.
- `api-guard ui`: a Streamlit page for `ask`, in a new `ui` extra.
- Fixed: a tool that found nothing (no expiring waivers) sent Groq an empty
  tool message, which it rejects with a 400. Empty results now read
  "(no results)".
- Fixed: every MCP tool advertised its inputs as `args` and `kwargs` instead of
  `build_id`, because the error-handling wrapper hid the real signature, so
  calls from any MCP client failed validation.
- `review` writes `approval-request.md` to the report directory only while a
  review is paused, and removes a stale one otherwise, so CI can decide whether
  to wait by checking for the file instead of parsing console output.
- The approval question says "contract change(s)", not "breaking change(s)": it
  counts every detected change, including those below the blocking threshold.
- `--help` keeps the module docstring's line breaks.
- Restart persistence is now tested with two separate processes sharing only
  the SQLite file (`tests/test_review.py`). The earlier in-memory test only
  proved pause and resume within one process.

## [1.0.0] — 2026-09-21

First release.

### The three checks

- **freshness** — regenerates the spec and compares it to the committed one.
  Without this the other two pass happily against a stale contract.
- **breaking** — `oasdiff`, with the verdict computed here rather than taken
  from its exit code.
- **conformance** — `schemathesis` against a running API, catching drift that
  leaves the spec untouched.

### Waivers

Acknowledged breaking changes live in `waivers.yaml`, matched on oasdiff's
`fingerprint` rather than its output text, and requiring a reason, an approver
and an expiry date. Expired waivers fail the build.

### Notable behaviour, established by testing rather than assumption

- `oasdiff breaking` exits **0** on breaking changes unless `--fail-on` is
  passed. api-guard therefore never relies on that exit code. A pipeline that
  did would go green forever while printing the breakages in its log.
- `x-sunset` applies to **endpoints only**. For a response field the breaking
  moment is the required-to-optional demotion; once optional, removal is clean.
- Fingerprints identify the change, not its position, so reformatting a spec
  does not invalidate waivers.
- Severity levels encode as `info`=1, `warn`=2, `error`=3, and `--fail-on`
  rejects `INFO` despite the documentation listing it.

### Exit codes

`0` intact · `1` would break consumers · `2` could not reach a conclusion.
`1` and `2` are kept distinct on purpose — a config error reported as a
breaking change sends people hunting for something that does not exist.

### Images

- `sohanbhadalkar/api-guard:1` — the gate
- `sohanbhadalkar/api-guard:1-ai` — adds `--explain`, the LangGraph workflow
  and the MCP server

The AI layer is a separate variant because it is roughly 100MB of
machine-learning dependencies for a feature that by design cannot change a
build result.

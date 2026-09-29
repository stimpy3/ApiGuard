# Changelog

Semver tags mean nothing to a consumer without this file: someone pinning
`:1` needs to know whether `1.1` is safe to pick up.

## [Unreleased]

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

# Changelog

Semver tags mean nothing to a consumer without this file: someone pinning
`:1` needs to know whether `1.1` is safe to pick up.

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

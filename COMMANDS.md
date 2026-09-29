# api-guard commands

Every command, one example each. Full flags: `api-guard <command> --help`.

## Run it

```bash
# Installed locally (the breaking check also needs the oasdiff binary on PATH)
pip install -e ".[cli]"            # the gate
pip install -e ".[cli,ai]"         # + explain, review/approve, ask
pip install -e ".[cli,ai,ui]"      # + the web page

# Or with Docker, which already has oasdiff and Schemathesis inside
docker run --rm -v "$PWD:/work" -w /work sohanbhadalkar/api-guard:1-ai check
```

## The gate

| Command | Example | Use it when |
|---|---|---|
| `check` | `api-guard check` | Run all three checks against `./api-guard.yaml` |
| `check --config` | `api-guard check --config path/to/api-guard.yaml` | Your config is not in the current folder |
| `check --generated-spec` | `api-guard check --generated-spec generated.yaml` | You already exported the spec from your code (e.g. in CI, or from the Docker image, which can't run your code) |
| `check --only` | `api-guard check --only breaking` | You want just some checks: `freshness`, `breaking`, `conformance` |
| `check --url` | `api-guard check --only conformance --url http://localhost:8080` | Testing a different running API than the config says, e.g. staging after a deploy |

**Exit codes:** `0` contract OK (or every break waived) · `1` would break consumers · `2` api-guard itself couldn't run (bad config, missing tool).

Reports land in `api-guard-report/`: `report.md` to read, `result.json` for tools, `junit.xml` for CI.

## AI extras (need `GROQ_API_KEY`; never change the verdict)

| Command | Example | Use it when |
|---|---|---|
| `check --explain` | `api-guard check --explain` | You want a plain-English "what breaks and how to migrate" section in `report.md` |
| `ask` | `api-guard ask "why did build 42 fail?"` | Investigating a past Jenkins build; Groq picks which tools to read |
| `ask --job` | `api-guard ask "did build 1 fail conformance?" --job 'sample-api-local/job/demo%252Fbreaking-rename'` | The build is in a different Jenkins job (multibranch jobs need the `%252F`) |
| `ask` (local) | `api-guard ask "what failed in build local?"` | Asking about the `result.json` in the current folder, no Jenkins |
| `ui` | `api-guard ui` | Same as `ask`, as a web page on http://localhost:8501 |

`ask` answers end with the model's own confidence, plus warnings api-guard computes itself (made-up IDs, tool errors). Treat both as advice.

## Approval (sign off a breaking change)

| Command | Example | Use it when |
|---|---|---|
| `review` | `api-guard review --id 42` | Same as `check`, but a blocked build pauses and waits for sign-off |
| `approve` | `api-guard approve 42 --by sohan` | Someone decides to ship the break anyway; writes `api-guard-report/review.md` |
| `--state` | `api-guard review --id 42 --state /shared/reviews.db` | The paused review must survive the workspace (use the same `--state` for `approve`) |

`approve` records who signed off. It does not change the verdict or `result.json`. In Jenkins, the pipeline shows an **Approve and ship** button instead.

## Allowing a break without approval

```yaml
# Retire an endpoint: mark it, ship, delete it after the date. No waiver.
paths:
  /users/search:
    get:
      deprecated: true
      x-sunset: '2027-03-01'
```

```yaml
# waivers.yaml - for anything else. Fingerprint is printed in the failure output.
- fingerprint: "3c11fcf1ab0e"
  id: response-property-became-optional
  path: /users
  reason: "PROD-142 - email superseded by phone, both consumers migrated."
  approved_by: sohan
  expires: 2026-12-31      # expired waivers fail the build
```

## MCP server (lets an AI client read build results)

```bash
python -m api_guard.ai.mcp_server
```

Six read-only tools: `get_report`, `get_spec_diff`, `get_conformance_results`, `get_freshness_result`, `get_build_context`, `list_expiring_waivers`. Claude Code picks it up from `.mcp.json`; `ask` starts it by itself.

## Settings (environment or `.env`)

| Variable | Default | Used by |
|---|---|---|
| `GROQ_API_KEY` | none | All AI features |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | `--explain` |
| `GROQ_CLASSIFY_MODEL` | `openai/gpt-oss-20b` | The routine/risky label in `review` |
| `GROQ_AGENT_MODEL` | `openai/gpt-oss-20b` | `ask`, `ui` |
| `JENKINS_URL` | `http://localhost:8081` | `ask`, MCP server |
| `JENKINS_JOB` | `sample-api` | `ask`, MCP server |
| `JENKINS_USER`, `JENKINS_TOKEN` | none | Only if Jenkins needs a login |

## In CI

```yaml
# GitHub Actions (needs actions/checkout with fetch-depth: 0)
- uses: stimpy3/ApiGuard@main
  with:
    config: api-guard.yaml
    generated-spec: generated.yaml
    url: http://localhost:8000
```

Jenkins: see `sample-api/Jenkinsfile` for the full pipeline (gate → approval → ship).

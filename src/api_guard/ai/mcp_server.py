"""MCP server exposing api-guard's results to an AI client.

    python -m api_guard.ai.mcp_server

**What this is not.** It does not run the gate and it does not decide anything.
`api-guard -> checks -> verdict` stays the only path to pass/fail; this reads
results those checks already produced and archived. An MCP tool that could
produce a verdict of record would be a way around the whole design.

Results come from Jenkins' archived artifacts, so there is no separate
database: the pipeline already calls archiveArtifacts on the report directory,
and `build_id` is just the Jenkins build number.

**Tool granularity is deliberate.** Five small tools rather than one big
`get_report`, because the models this is aimed at pick correctly from short,
clearly-named tools with small payloads far more reliably than from one tool
returning a large blob. The granularity is for the model's benefit, not the
architecture's.

**Everything returned here is untrusted data.** Build logs and spec diffs are
written by whoever opened the pull request. The tools are read-only and take no
action based on their content — there is no "re-run the build if the log says
to".
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

JENKINS_URL = os.environ.get("JENKINS_URL", "http://localhost:8081")
JENKINS_JOB = os.environ.get("JENKINS_JOB", "sample-api")
JENKINS_USER = os.environ.get("JENKINS_USER", "")
JENKINS_TOKEN = os.environ.get("JENKINS_TOKEN", "")
REPORT_PATH = "api-guard-report/result.json"
_TIMEOUT = 20


class ReportUnavailable(Exception):
    """The archived result could not be fetched. Said plainly, not guessed at."""


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url)
    if JENKINS_USER and JENKINS_TOKEN:
        import base64

        token = base64.b64encode(f"{JENKINS_USER}:{JENKINS_TOKEN}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReportUnavailable(
                f"Nothing at {url}. Either that build does not exist, or it "
                "failed before api-guard produced a report."
            ) from exc
        raise ReportUnavailable(f"Jenkins returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ReportUnavailable(f"Could not reach Jenkins at {JENKINS_URL}: {exc}") from exc


def load_report(build_id: str) -> dict:
    """Fetch one build's result.json from Jenkins' archived artifacts."""
    local = Path(REPORT_PATH)
    if build_id in ("local", "latest-local") and local.exists():
        return json.loads(local.read_text(encoding="utf-8"))

    url = f"{JENKINS_URL}/job/{JENKINS_JOB}/{build_id}/artifact/{REPORT_PATH}"
    return json.loads(_fetch(url))


# --- tool implementations, kept importable for testing -------------------


def get_report(build_id: str) -> dict:
    return load_report(build_id)


def get_spec_diff(build_id: str) -> list[dict]:
    return list(load_report(build_id).get("changes", []))


def get_conformance_results(build_id: str) -> list[dict]:
    return [
        c
        for c in load_report(build_id).get("checks", [])
        if c.get("name") == "conformance"
    ]


def get_freshness_result(build_id: str) -> dict:
    for check in load_report(build_id).get("checks", []):
        if check.get("name") == "freshness":
            return check
    return {"status": "unknown", "summary": "no freshness result in this report"}


def get_build_context(build_id: str) -> dict:
    report = load_report(build_id)
    context: dict[str, Any] = {
        "verdict": report.get("verdict"),
        "exit_code": report.get("exit_code"),
        "generated_at": report.get("generated_at"),
        "meta": report.get("meta", {}),
    }
    try:
        info = json.loads(_fetch(f"{JENKINS_URL}/job/{JENKINS_JOB}/{build_id}/api/json"))
        context["jenkins"] = {
            "result": info.get("result"),
            "building": info.get("building"),
            "url": info.get("url"),
        }
    except ReportUnavailable:
        context["jenkins"] = None  # the report is the useful part regardless
    return context


def list_expiring_waivers(build_id: str, within_days: int = 30) -> list[dict]:
    """Waivers about to lapse — turning a surprise red build into a calendar item."""
    from datetime import date, timedelta

    cutoff = date.today() + timedelta(days=within_days)
    report = load_report(build_id)
    waivers = report.get("waivers", {})

    expiring = []
    for waiver in [*waivers.get("applied", []), *waivers.get("stale", [])]:
        raw = waiver.get("expires")
        if not raw:
            continue
        try:
            expires = date.fromisoformat(str(raw))
        except ValueError:
            continue
        if expires <= cutoff:
            expiring.append({**waiver, "days_left": (expires - date.today()).days})
    return sorted(expiring, key=lambda w: w["days_left"])


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise SystemExit(
            "The MCP server needs the AI extra: pip install 'api-guard[ai]'"
        ) from None

    server = FastMCP("api-guard")

    def _wrap(fn, description: str):
        """Turn ReportUnavailable into a readable answer rather than a stack trace."""

        def tool(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ReportUnavailable as exc:
                return {"error": str(exc)}

        tool.__name__ = fn.__name__
        tool.__doc__ = description
        return tool

    server.add_tool(_wrap(get_report, "The complete api-guard result for a build."))
    server.add_tool(
        _wrap(get_spec_diff, "Breaking changes oasdiff found in a build, as structured data.")
    )
    server.add_tool(
        _wrap(get_conformance_results, "Where the running API disagreed with its spec.")
    )
    server.add_tool(
        _wrap(get_freshness_result, "Whether the committed spec matched the code.")
    )
    server.add_tool(
        _wrap(get_build_context, "Verdict, commit, branch and Jenkins status for a build.")
    )
    server.add_tool(
        _wrap(list_expiring_waivers, "Waivers expiring soon, so they can be renewed or dropped.")
    )

    server.run()


if __name__ == "__main__":
    main()

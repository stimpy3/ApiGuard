"""Writing results out.

`result.json` is the machine-readable interface everything downstream reads.
The markdown is for humans in a PR comment or a Jenkins page; the JUnit XML is
so CI systems show check failures in their native test UI.

One rule throughout: **applied waivers are always shown**, including on a
passing run. A waiver that silences a breaking change without anyone seeing it
is no better than having no gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from api_guard.results import Status
from api_guard.verdict import RunResult

_ICON = {
    Status.PASSED: "PASS",
    Status.FAILED: "FAIL",
    Status.SKIPPED: "SKIP",
    Status.ERROR: "ERROR",
}


def write(
    result: RunResult,
    out_dir: Path,
    formats: list[str],
    *,
    analysis: str | None = None,
) -> dict[str, Path]:
    """Write the reports. `analysis` is optional AI prose appended to markdown.

    It is not written into result.json: that file is the machine-readable
    record of what the deterministic checks found, and mixing generated prose
    into it would let a downstream consumer mistake one for the other.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    if "json" in formats:
        path = out_dir / "result.json"
        path.write_bytes(result.model_dump_json(indent=2).encode("utf-8"))
        written["json"] = path

    if "markdown" in formats:
        path = out_dir / "report.md"
        markdown = render_markdown(result)
        if analysis:
            markdown = f"{markdown}\n{analysis}"
        path.write_bytes(markdown.encode("utf-8"))
        written["markdown"] = path

    if "junit" in formats:
        path = out_dir / "junit.xml"
        path.write_bytes(_render_junit(result))
        written["junit"] = path

    return written


def render_markdown(result: RunResult) -> str:
    headline = {
        Status.PASSED: "Contract check passed",
        Status.FAILED: "Contract check failed - deployment blocked",
        Status.ERROR: "Contract check could not complete",
    }.get(result.verdict, "Contract check")

    lines = [f"# {headline}", ""]

    lines += ["| Check | Result | Summary |", "|---|---|---|"]
    for check in result.checks:
        lines.append(f"| {check.name} | {_ICON[check.status]} | {check.summary} |")
    lines.append("")

    for check in result.checks:
        if check.detail and check.status is not Status.PASSED:
            lines += [f"## {check.name}", "", "```", check.detail, "```", ""]

    if result.waivers.applied:
        lines += [
            "## Waived breaking changes",
            "",
            "These changes were detected and allowed through because somebody "
            "accepted the consequences:",
            "",
            "| Change | Reason | Approved by | Expires |",
            "|---|---|---|---|",
        ]
        for waiver in result.waivers.applied:
            lines.append(
                f"| `{waiver.describe()}` | {waiver.reason} | "
                f"{waiver.approved_by} | {waiver.expires.isoformat()} |"
            )
        lines.append("")

    if result.waivers.stale:
        lines += [
            "## Stale waivers",
            "",
            "These waivers matched nothing in this run. Usually that means the "
            "underlying problem was fixed properly, and the waiver can be deleted:",
            "",
        ]
        lines += [f"- `{w.describe()}` (expires {w.expires.isoformat()})" for w in result.waivers.stale]
        lines.append("")

    if result.waivers.expired:
        lines += [
            "## Expired waivers - remove them",
            "",
            "These waivers are past their date and were ignored: a change they "
            "used to cover now counts as if they did not exist. Delete them from "
            "the waivers file, or renew with a fresh reason and date if the change "
            "is still acceptable:",
            "",
        ]
        lines += [
            f"- `{w.describe()}` (expired {w.expires.isoformat()}, approved by {w.approved_by})"
            for w in result.waivers.expired
        ]
        lines.append("")

    if result.meta:
        lines += ["---", "", "<details><summary>Run details</summary>", ""]
        lines += [f"- **{key}**: {value}" for key, value in sorted(result.meta.items())]
        lines += ["", "</details>", ""]

    return "\n".join(lines)


def _render_junit(result: RunResult) -> bytes:
    """One test case per check, so CI renders failures natively.

    A skipped check is reported as skipped rather than passed: "we did not look"
    and "we looked and it was fine" should not appear the same in a dashboard.
    """
    failures = sum(1 for c in result.checks if c.status is Status.FAILED)
    errors = sum(1 for c in result.checks if c.status is Status.ERROR)
    skipped = sum(1 for c in result.checks if c.status is Status.SKIPPED)

    suite = ET.Element(
        "testsuite",
        name="api-guard",
        tests=str(len(result.checks)),
        failures=str(failures),
        errors=str(errors),
        skipped=str(skipped),
    )

    for check in result.checks:
        case = ET.SubElement(suite, "testcase", classname="api-guard", name=check.name)
        body = check.detail or check.summary
        if check.status is Status.FAILED:
            ET.SubElement(case, "failure", message=check.summary).text = body
        elif check.status is Status.ERROR:
            ET.SubElement(case, "error", message=check.summary).text = body
        elif check.status is Status.SKIPPED:
            ET.SubElement(case, "skipped", message=check.summary)

    suites = ET.Element("testsuites")
    suites.append(suite)
    return ET.tostring(suites, encoding="utf-8", xml_declaration=True)

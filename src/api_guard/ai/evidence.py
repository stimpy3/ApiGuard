"""Where the AI workflow gets its facts.

Two implementations of one interface, and the difference is *when* the question
is asked rather than what is asked.

**LocalEvidence** — during the build. The checks just ran in this process and
the results are in memory. Reading them directly is the whole of it: no
network, no auth, works offline.

**McpEvidence** — afterwards, from an editor or a chat window, hours later and
possibly on another machine. The process that produced those results is long
gone, so the facts come back through the MCP server reading an archived
result.json.

This is the answer to the obvious objection about MCP: why serialise data out
through a protocol and back when it is already in memory? In-pipeline, we do
not. MCP exists for the case where **the asker is not the pipeline** — a
different process, at a different time, that was never part of the build. That
is the problem MCP was built for, and it is why the graph is written against an
interface instead of against either one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from api_guard.verdict import RunResult


class Evidence(Protocol):
    """What the workflow needs to know. Deliberately small.

    Every method returns plain data that the deterministic checks already
    produced. Nothing here computes a verdict — an evidence source that could
    decide things would be a way around the rule that only the checks decide.
    """

    def verdict(self) -> str: ...
    def changes(self) -> list[dict]: ...
    def conformance_failures(self) -> list[str]: ...
    def waivers_applied(self) -> list[dict]: ...
    def describe(self) -> str: ...


class LocalEvidence:
    """In-pipeline: read the results already sitting in memory."""

    def __init__(self, result: RunResult) -> None:
        self._result = result

    def verdict(self) -> str:
        return str(self._result.verdict)

    def changes(self) -> list[dict]:
        return [c.model_dump() for c in self._result.changes]

    def conformance_failures(self) -> list[str]:
        return [
            c.detail or c.summary
            for c in self._result.checks
            if c.name == "conformance" and c.is_blocking
        ]

    def waivers_applied(self) -> list[dict]:
        return [w.model_dump(mode="json") for w in self._result.waivers.applied]

    def describe(self) -> str:
        return "this build (in-process)"


class McpEvidence:
    """Post-hoc: fetch an archived result through the MCP tools.

    Takes an already-connected client rather than opening its own session, so
    the caller owns the connection lifetime and this stays testable with a
    stub.
    """

    def __init__(self, client: Any, build_id: str) -> None:
        self._client = client
        self._build_id = build_id
        self._cache: dict | None = None

    def _report(self) -> dict:
        if self._cache is None:
            self._cache = self._client.get_report(self._build_id)
        return self._cache

    def verdict(self) -> str:
        return str(self._report().get("verdict", "unknown"))

    def changes(self) -> list[dict]:
        return list(self._report().get("changes", []))

    def conformance_failures(self) -> list[str]:
        return [
            check.get("detail") or check.get("summary", "")
            for check in self._report().get("checks", [])
            if check.get("name") == "conformance"
            and check.get("status") in ("failed", "error")
        ]

    def waivers_applied(self) -> list[dict]:
        return list(self._report().get("waivers", {}).get("applied", []))

    def describe(self) -> str:
        return f"build {self._build_id} (via MCP)"

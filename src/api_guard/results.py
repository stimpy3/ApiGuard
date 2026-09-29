"""Result types shared by every check.

These are also the shape of `result.json`, which is api-guard's real interface:
the MCP server, PR comments and any dashboard read it rather than scraping
console output. Adding a field is fine; renaming or removing one is a breaking
change to our own consumers, which is why SCHEMA_VERSION exists.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from api_guard.config import Severity

# Bump the minor for additive changes, the major for anything a consumer could
# trip over. A tool that enforces API contracts should keep its own.
# 1.1: waivers.expired added.
SCHEMA_VERSION = "1.1"


class Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"

    @property
    def is_blocking(self) -> bool:
        """SKIPPED is not a failure.

        A check can be legitimately skipped — no `generate_cmd`, no previous
        spec to compare against on a brand-new repo — and treating that as a
        failure would make the tool unusable on day one of a project.
        """
        return self in (Status.FAILED, Status.ERROR)


class Change(BaseModel):
    """One entry from oasdiff, normalised.

    Mirrors oasdiff's JSON but only the fields we rely on, so an upstream
    addition cannot surprise us and a removal fails loudly here rather than
    somewhere downstream.
    """

    fingerprint: str | None = None
    id: str = ""
    text: str = ""
    severity: Severity = Severity.ERR
    operation: str | None = None
    path: str | None = None

    @classmethod
    def from_oasdiff(cls, entry: dict) -> Change:
        return cls(
            fingerprint=entry.get("fingerprint"),
            id=entry.get("id", ""),
            text=entry.get("text", ""),
            severity=Severity.from_oasdiff_level(entry.get("level", 3)),
            operation=entry.get("operation"),
            path=entry.get("path"),
        )

    def describe(self) -> str:
        where = f"{self.operation} {self.path}" if self.operation and self.path else self.path
        return f"{where}: {self.text}" if where else self.text


class CheckResult(BaseModel):
    """The outcome of one of the three checks."""

    name: str
    status: Status
    summary: str = Field(description="One line, safe to print in a CI log.")
    detail: str | None = Field(
        default=None, description="Longer explanation, shown when the check did not pass."
    )

    @property
    def is_blocking(self) -> bool:
        return self.status.is_blocking

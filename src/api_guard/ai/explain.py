"""Turns a verdict into something a human can act on.

Three outputs: what breaks for consumers, the migration that avoids breaking
them, and — only if the team decides to break anyway — a draft waiver entry.

Two constraints shape all of this.

**It cannot affect the build.** By the time anything here runs, the exit code
is already decided. Every failure path returns quietly. A gate that goes red
because an LLM provider had a bad minute is a gate people route around.

**The model is small.** Groq serves open models, so this sends a filtered,
pre-parsed list of changes rather than raw OpenAPI documents, asks for
structured output, and validates what comes back. It is not trusted to parse a
spec or to know the policy rules — those are computed here and handed over.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from api_guard.ai import llm
from api_guard.results import Status

if TYPE_CHECKING:
    from api_guard.verdict import RunResult

# Small models lose the thread on long lists, and a report nobody reads is
# worth nothing anyway. The worst breakages are enough to act on.
_MAX_CHANGES = 8
# Input budget for the prompt, in tokens: comfortably inside the free tier.
_BUDGET = 2500


class Explanation(BaseModel):
    """The structured answer requested from the model."""

    impact: str = Field(description="What breaks for existing consumers, in plain English.")
    migration: str = Field(description="How to make this change without breaking them.")
    severity_note: str = Field(
        default="",
        description="Anything a reviewer should notice that the rules cannot express.",
    )


def explain(result: RunResult) -> str | None:
    """Append an impact analysis to the report. Returns the markdown, or None.

    Runs the review graph without its human step (graph.analysis_for), so
    `check --explain` and the approval question share one explanation path:
    triage picks the model, then the explain step writes this section.

    Never raises. Every failure — missing key, missing extra, provider down,
    malformed response — degrades to returning None, because the alternative is
    an advisory feature taking down a deployment gate.
    """
    if result.verdict is Status.PASSED and not result.changes:
        return None  # nothing to explain
    try:
        from api_guard.ai import graph

        return graph.analysis_for(result)
    except Exception:  # noqa: BLE001 - advisory only; see the module docstring
        return None


def _prompt(changes: list) -> str:
    compact, omitted = llm.compact_changes(changes, limit=_MAX_CHANGES)
    # The facts are computed here and handed over. The model is asked to
    # explain them, not to work them out: it never sees a spec file, so it
    # cannot decide what counts as breaking.
    facts = llm.fit(llm.format_changes(compact, omitted), _BUDGET)
    return (
        "You are reviewing breaking changes to an HTTP API, detected by a "
        "deterministic diff tool. The analysis below is already correct; do not "
        "dispute it or re-classify anything.\n\n"
        f"Detected changes:\n{facts}\n\n"
        f"{llm.POLICY_RULES}\n\n"
        "Write, for the developer who is now blocked:\n"
        "1. impact - what breaks for existing consumers, concretely. Name the "
        "fields and endpoints. No preamble.\n"
        "2. migration - how to ship this without breaking them, following the "
        "rules above. Prefer the deprecation route over a waiver.\n"
        "3. severity_note - optional, only if a reviewer would otherwise miss "
        "something.\n\n"
        "Be concise and specific."
    )


def _ask(changes: list, *, model_name: str | None = None) -> Explanation | None:
    """One structured call, with the shared fallbacks.

    A rate limit moves to the other Groq model (llm.with_fallback). A model
    that cannot produce valid structured output falls back to prose rather
    than losing the explanation entirely.
    """
    prompt = _prompt(changes)

    def call(model):
        try:
            answer = llm.structured(model, Explanation).invoke(prompt)
            return answer if isinstance(answer, Explanation) else None
        except Exception as exc:  # noqa: BLE001
            if llm.is_rate_limit(exc):
                raise  # let with_fallback try the other model
            text = (getattr(model.invoke(prompt), "content", "") or "").strip()
            return Explanation(impact=text, migration="") if text else None

    try:
        return llm.with_fallback("explain", call, name=model_name)
    except Exception:  # noqa: BLE001 - advisory only
        return None


def _render(explanation: Explanation, changes: list) -> str:
    lines = [
        "## What this means",
        "",
        explanation.impact.strip(),
        "",
    ]

    if explanation.migration.strip():
        lines += ["### Suggested migration", "", explanation.migration.strip(), ""]

    if explanation.severity_note.strip():
        lines += ["> " + explanation.severity_note.strip(), ""]

    # The fingerprints are computed, not generated. Getting one wrong would
    # produce a waiver that silently matches nothing, so the model is never
    # asked to reproduce them.
    lines += [
        "### If you are breaking this deliberately",
        "",
        "Prefer the migration above. If the team decides to break it now, add "
        "this to `waivers.yaml` and fill in the reason — a waiver is reviewed "
        "in the pull request and kept in git history, so it needs to say "
        "something a reader can evaluate:",
        "",
        "```yaml",
    ]
    for change in changes:
        if change.fingerprint:
            lines += [
                f'- fingerprint: "{change.fingerprint}"',
                f"  id: {change.id}",
                f"  path: {change.path or ''}",
                '  reason: "TICKET-000 - why this is acceptable, and who confirmed consumers are migrated"',
                "  approved_by: your-name",
                "  expires: 2027-01-01",
            ]
    lines += ["```", ""]

    lines += [
        "<sub>Written by a language model from the diff above. Advisory only - "
        "it ran after the verdict and did not influence it.</sub>",
        "",
    ]
    return "\n".join(lines)

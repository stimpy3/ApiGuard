"""The review workflow, as a LangGraph.

    load_evidence -> triage (LLM) -> explain (LLM) -> decide -> [human] -> render

Every AI step that follows a verdict lives in this one graph. `check --explain`
runs it without the human step; `review` runs it with one. So the approver of
a blocked build sees the same explanation the report carries, instead of the
two coming from separate code paths.

**Why a graph rather than a few function calls.** The checkpointer. Human
approval can pause the workflow for hours or days; a plain script would either
block a build agent that whole time or lose its place. A checkpointed graph
persists its state and resumes exactly where it stopped, in a different process
if need be — and what it persists includes what the model wrote, so the
approver sees exactly what was generated at build time, with no second call.

**What `decide` may look at.** The deterministic results, and nothing else. The
model's band and explanation are written into the state and then ignored by the
node that matters. That is demonstrable: delete the model steps and the report
changes while the decision does not. The band does choose one thing — which
model writes the explanation — and that choice never reaches a verdict.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from api_guard.ai import llm
from api_guard.ai.evidence import Evidence

# Advisory bands. Not severities — oasdiff already assigned those, and this is
# a different question: how much human attention does it deserve?
Band = Literal["routine", "risky", "unknown"]

# A two-way label from a pre-parsed list is a small job: the "triage" role in
# llm.py, which defaults to the smaller, faster model.
_BUDGET = 1500  # input tokens for the triage prompt

# How many changes the approval question lists before summarising the rest.
_QUESTION_CHANGES = 8


class Severity(BaseModel):
    band: Band = Field(description="routine, risky, or unknown.")
    rationale: str = Field(default="", description="One sentence, for a reviewer.")


class State(TypedDict, total=False):
    evidence_label: str
    context: dict
    verdict: str
    changes: list[dict]
    conformance_failures: list[str]
    waivers_applied: list[dict]

    # Written by the model. Read by render and by the approval question,
    # never by decide.
    band: Band
    rationale: str
    impact: str
    migration: str
    severity_note: str
    explain_model: str
    analysis: str  # the "What this means" markdown, as report.md carries it

    blocked: bool
    needs_approval: bool
    question: str
    approved_by: str | None
    report: str


def build(
    evidence: Evidence | None,
    *,
    checkpointer: Any | None = None,
    approval: bool = True,
):
    """Compile the workflow. Returns None if langgraph is not installed.

    `evidence` is None when resuming a saved run: the facts are already in the
    checkpoint, and loading them again would mean the pause did not really
    persist anything.

    `approval=False` is `check --explain`: the same steps, never pausing.
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return None

    graph = StateGraph(State)

    graph.add_node("load_evidence", _loader(evidence))
    graph.add_node("classify_severity", _classify)
    graph.add_node("explain", _explain)
    graph.add_node("decide", _decide)
    graph.add_node("human_approval", _approval)
    graph.add_node("render_report", _render)

    graph.add_edge(START, "load_evidence")
    graph.add_edge("load_evidence", "classify_severity")
    graph.add_edge("classify_severity", "explain")
    graph.add_edge("explain", "decide")
    graph.add_conditional_edges(
        "decide",
        lambda s: "human_approval" if approval and s.get("needs_approval") else "render_report",
        {"human_approval": "human_approval", "render_report": "render_report"},
    )
    graph.add_edge("human_approval", "render_report")
    graph.add_edge("render_report", END)

    return graph.compile(checkpointer=checkpointer)


def analysis_for(result: Any) -> str | None:
    """`check --explain`: run the graph on a finished check, return its markdown.

    Never raises. Every failure — missing extra, no key, provider down —
    degrades to None, because the alternative is an advisory feature taking
    down a deployment gate.
    """
    try:
        from api_guard.ai.evidence import LocalEvidence

        compiled = build(LocalEvidence(result), approval=False)
        if compiled is None:
            return None
        return compiled.invoke({}).get("analysis") or None
    except Exception:  # noqa: BLE001 - advisory only
        return None


def _loader(evidence: Evidence | None):
    def load_evidence(_state: State) -> State:
        if evidence is None:
            raise RuntimeError(
                "load_evidence ran on a resume-only graph; the checkpoint should "
                "already hold the evidence"
            )
        context = getattr(evidence, "context", None)
        return {
            "evidence_label": evidence.describe(),
            "context": context() if callable(context) else {},
            "verdict": evidence.verdict(),
            "changes": evidence.changes(),
            "conformance_failures": evidence.conformance_failures(),
            "waivers_applied": evidence.waivers_applied(),
        }

    return load_evidence


def _classify(state: State) -> State:
    """Ask the model how much attention this deserves. ADVISORY ONLY.

    Failure here is not an error. An unavailable model means the band is
    'unknown', the report says so, and the verdict is untouched.
    """
    changes = state.get("changes", [])
    if not changes:
        return {"band": "routine", "rationale": "No contract changes detected."}

    if not llm.available():
        return {"band": "unknown", "rationale": "No model configured; not classified."}

    try:
        compact, omitted = llm.compact_changes(changes)
        listed = llm.fit(llm.format_changes(compact, omitted), _BUDGET)
        prompt = (
            "Classify how much human attention these API contract changes need.\n\n"
            f"{listed}\n\n"
            "'routine' if a reviewer would wave this through — additive, or a "
            "well-signposted deprecation. 'risky' if a consumer could plausibly "
            "break in a way the rules alone do not convey.\n\n"
            "This is advice for a human, not a decision. The build outcome is "
            "already settled."
        )
        answer = llm.with_fallback(
            "triage", lambda model: llm.structured(model, Severity).invoke(prompt)
        )
        if isinstance(answer, Severity):
            return {"band": answer.band, "rationale": answer.rationale}
    except Exception:  # noqa: BLE001 - advisory; see the module docstring
        pass

    return {"band": "unknown", "rationale": "Classification unavailable."}


def _explain(state: State) -> State:
    """Write "what breaks and how to migrate". ADVISORY ONLY.

    The band picks the model: a routine change gets the small one, anything
    risky or unclassified gets the explain role's model (the larger one by
    default). That saves tokens where the write-up is simple, and it is the
    only thing the band ever influences.
    """
    changes = state.get("changes") or []
    if not changes or not llm.available():
        return {}

    from api_guard.ai import explain
    from api_guard.results import Change

    name = llm.SMALL if state.get("band") == "routine" else llm.model_name("explain")
    answer = explain._ask(changes, model_name=name)
    if answer is None:
        return {}

    ranked = sorted((Change(**c) for c in changes), key=lambda c: c.severity.rank, reverse=True)
    return {
        "impact": answer.impact,
        "migration": answer.migration,
        "severity_note": answer.severity_note,
        "explain_model": name,
        "analysis": explain._render(answer, ranked[: explain._MAX_CHANGES]),
    }


def _decide(state: State) -> State:
    """The only node that determines anything.

    Reads `verdict` — produced by the deterministic checks. It does NOT read
    `band`, `rationale` or the explanation, though all of them are sitting in
    the state right beside it. That restraint is the entire architecture, and
    test_boundaries.py plus test_graph.py keep it honest.
    """
    blocked = state.get("verdict") in ("failed", "error")

    # Approval is offered for a blocked build that someone might legitimately
    # want to push through — never as a way to bypass a tooling error, where
    # nothing was actually established.
    needs_approval = blocked and state.get("verdict") == "failed"

    return {"blocked": blocked, "needs_approval": needs_approval}


def approval_question(state: State) -> str:
    """Everything an approver needs, in one message.

    Built from the saved state only, so an approver days later sees exactly
    what was generated at build time. Facts first (they are always there),
    then the model's advice, clearly labelled as such.
    """
    changes = state.get("changes") or []
    context = state.get("context") or {}

    head = f"Review of {state.get('evidence_label', 'this build')}"
    if context.get("commit"):
        head += f" · commit {str(context['commit'])[:7]}"
    if context.get("branch") and context["branch"] != "HEAD":
        head += f" · branch {context['branch']}"
    lines = [head, ""]

    lines.append(f"Changes ({len(changes)}):")
    for c in changes[:_QUESTION_CHANGES]:
        where = f"{c.get('operation') or ''} {c.get('path') or ''}".strip()
        lines.append(f"  - [{c.get('severity', 'ERR')}] {where}: {c.get('text')}")
    if len(changes) > _QUESTION_CHANGES:
        lines.append(f"  - ... and {len(changes) - _QUESTION_CHANGES} more (see report.md)")
    fingerprints = [c.get("fingerprint") for c in changes if c.get("fingerprint")]
    if fingerprints:
        lines.append(f"Fingerprints: {', '.join(fingerprints)}")
    lines.append("")

    band = state.get("band", "unknown")
    lines.append(f"Risk (model's opinion): {band.upper()}"
                 + (f" - {state['rationale']}" if state.get("rationale") else ""))
    if state.get("impact"):
        lines.append(f"What breaks (model): {state['impact']}")
    if state.get("migration"):
        lines.append(f"Safer route (model): {state['migration']}")
    if not state.get("impact"):
        lines.append("No AI explanation available; the changes above are the facts.")
    lines += ["", "Approve shipping this anyway?"]
    return "\n".join(lines)


def _approval(state: State) -> State:
    """Pause for a human, with the full context.

    interrupt() suspends the graph and persists it. Resuming is a separate
    call, possibly days later — which is exactly why a checkpointer earns its
    place here. On resume this node runs again from the top, so everything
    before the interrupt is computed only from the saved state.
    """
    try:
        from langgraph.types import interrupt
    except ImportError:
        return {"approved_by": None}

    question = approval_question(state)
    answer = interrupt({"question": question, "changes": state.get("changes", [])})

    approved_by = answer.get("approved_by") if isinstance(answer, dict) else (str(answer) if answer else None)
    return {"question": question, "approved_by": approved_by}


def _render(state: State) -> State:
    lines = [
        f"# Contract analysis — {state.get('evidence_label', 'unknown source')}",
        "",
        f"**Verdict:** {state.get('verdict', 'unknown')}"
        f"{' (blocked)' if state.get('blocked') else ''}",
        f"**Advisory band:** {state.get('band', 'unknown')}",
    ]
    if state.get("rationale"):
        lines.append(f"> {state['rationale']}")
    lines.append("")

    changes = state.get("changes", [])
    if changes:
        lines += ["## Changes", ""]
        lines += [
            f"- `{c.get('id')}` {c.get('operation') or ''} {c.get('path') or ''} — {c.get('text')}"
            for c in changes
        ]
        lines.append("")

    if state.get("conformance_failures"):
        lines += ["## Conformance", ""]
        lines += [f"- {f.splitlines()[0] if f else ''}" for f in state["conformance_failures"]]
        lines.append("")

    if state.get("waivers_applied"):
        lines += ["## Waived", ""]
        lines += [
            f"- `{w.get('id') or w.get('fingerprint')}` — {w.get('reason')} "
            f"({w.get('approved_by')}, expires {w.get('expires')})"
            for w in state["waivers_applied"]
        ]
        lines.append("")

    if state.get("analysis"):
        lines += [state["analysis"].rstrip(), ""]

    if state.get("approved_by"):
        lines += [f"Approved to ship by **{state['approved_by']}**.", ""]

    lines += [
        "<sub>The band, rationale and explanation are model-generated and "
        "advisory. The verdict came from the deterministic checks and was fixed "
        "before any of this ran.</sub>",
    ]
    return {"report": "\n".join(lines)}

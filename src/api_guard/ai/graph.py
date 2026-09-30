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

import operator
from typing import Annotated, Any, Literal, TypedDict

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

# Questions an approver may ask per review before deciding.
MAX_QUESTIONS = 5

_SEVERITY_RANK = {"INFO": 1, "WARN": 2, "ERR": 3}


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

    # Policy, passed in by review.start from api-guard.yaml.
    fail_on: str
    max_waiver_days: int

    blocked: bool
    needs_approval: bool
    question: str

    # The human's side. Every question and answer is appended, never
    # overwritten: together with the checkpoints this is the audit trail.
    qa: Annotated[list[dict], operator.add]
    pending_question: str
    decision: str  # "approve" | "reject" once decided
    decided_by: str
    reason: str
    expires: str  # ISO date, for the waiver an approval produces
    approved_by: str | None
    waiver_snippet: str
    checklist: list[str]
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
    graph.add_node("answer_question", _answer)
    graph.add_node("approve", _approve)
    graph.add_node("reject", _reject)
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
    # The human's three choices. A question loops back to the same pause, with
    # the answer added to what the approver sees; approve and reject end it.
    graph.add_conditional_edges(
        "human_approval",
        _route_decision,
        {"answer_question": "answer_question", "approve": "approve", "reject": "reject"},
    )
    graph.add_edge("answer_question", "human_approval")
    graph.add_edge("approve", "render_report")
    graph.add_edge("reject", "render_report")
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

    qa = state.get("qa") or []
    if qa:
        lines += ["", "Your questions so far:"]
        for item in qa:
            lines.append(f"  Q: {item.get('question')}")
            lines.append(f"  A: {item.get('answer')}")
    left = MAX_QUESTIONS - len(qa)
    lines += [
        "",
        "Approve (ships this build and gives you a waiver to commit), reject "
        "(fails it with a fix checklist)"
        + (f", or ask a question ({left} left)?" if left > 0 else "? No questions left."),
    ]
    return "\n".join(lines)


def _approval(state: State) -> State:
    """Pause for a human, with the full context.

    interrupt() suspends the graph and persists it. Resuming is a separate
    call, possibly days later — which is exactly why a checkpointer earns its
    place here. On resume this node runs again from the top, so everything
    before the interrupt is computed only from the saved state.

    The resume value is validated by review.py before it gets here:
    {"decision": "approve", "by", "reason", "expires"},
    {"decision": "reject", "by", "reason"}, or
    {"decision": "question", "question"}.
    """
    try:
        from langgraph.types import interrupt
    except ImportError:
        return {"approved_by": None}

    question = approval_question(state)
    answer = interrupt({"question": question, "changes": state.get("changes", [])})
    if not isinstance(answer, dict):
        answer = {"decision": "approve", "by": str(answer) if answer else ""}

    decision = answer.get("decision") or ("approve" if answer.get("approved_by") else "")
    by = (answer.get("by") or answer.get("approved_by") or "").strip()

    if decision == "question":
        return {"question": question, "pending_question": answer.get("question", ""), "decision": "question"}
    if decision == "reject":
        return {"question": question, "decision": "reject", "decided_by": by,
                "reason": answer.get("reason", "")}
    return {
        "question": question,
        "decision": "approve",
        "decided_by": by,
        "approved_by": by,
        "reason": answer.get("reason", ""),
        "expires": answer.get("expires", ""),
    }


def _route_decision(state: State) -> str:
    return {"question": "answer_question", "reject": "reject"}.get(state.get("decision", ""), "approve")


def _answer(state: State) -> State:
    """Our agent answers the approver's question, then the graph pauses again.

    It starts from the facts the review already saved (the same text the
    approver sees), so most questions need no tool calls; the MCP tools are
    there for anything those facts don't cover. The answer is advisory like
    every other model output, and can't decide anything.
    """
    question = state.get("pending_question", "")
    qa = state.get("qa") or []
    entry = {"question": question, "answer": "", "tools": [], "confidence": ""}

    if len(qa) >= MAX_QUESTIONS:
        entry["answer"] = f"Not answered: the limit of {MAX_QUESTIONS} questions is reached. Approve or reject."
    elif not llm.available():
        entry["answer"] = "AI is off (no Groq key), so there is no answer. The facts above are complete."
    else:
        try:
            from api_guard.ai import agent

            reply = agent.answer_review(question, approval_question(state))
            entry.update(answer=reply.text, tools=reply.steps, confidence=reply.confidence or "",
                         warnings=reply.warnings)
        except Exception as exc:  # noqa: BLE001 - advisory: an answer that failed is still recorded
            message = str(exc) or type(exc).__name__
            entry["answer"] = message[0].upper() + message[1:]

    return {"qa": [entry], "pending_question": "", "decision": ""}


def _blocking(state: State) -> list[dict]:
    """The changes that blocked this build: at or above the policy threshold."""
    threshold = _SEVERITY_RANK.get(str(state.get("fail_on", "ERR")), 3)
    return [
        c for c in (state.get("changes") or [])
        if _SEVERITY_RANK.get(str(c.get("severity", "ERR")), 3) >= threshold
    ]


def _approve(state: State) -> State:
    """Turn the approval into waiver entries the developer can commit.

    Approval ships *this* build. Without a waiver, the next build of the same
    branch would block again, because waivers live in git and CI cannot write
    to it. So the approval produces the exact entries — real fingerprints,
    the approver's name and reason, the expiry — each validated with the same
    model waivers.yaml is loaded with, ready to paste into the pull request.
    """
    import yaml

    from api_guard.policy import Waiver

    # review.approve always supplies both. A bare resume with only a name (the
    # graph driven directly) records the sign-off without inventing a waiver.
    if not state.get("expires") or len(state.get("reason", "").strip()) < 10:
        return {"waiver_snippet": ""}

    entries = []
    for change in _blocking(state):
        if not change.get("fingerprint"):
            continue
        waiver = Waiver(
            fingerprint=change["fingerprint"],
            id=change.get("id") or None,
            path=change.get("path") or None,
            reason=state.get("reason", ""),
            approved_by=state.get("decided_by", ""),
            expires=state.get("expires"),
        )
        entry = {
            "fingerprint": waiver.fingerprint,
            "id": waiver.id,
            "path": waiver.path,
            "reason": waiver.reason,
            "approved_by": waiver.approved_by,
            "expires": waiver.expires,
        }
        entries.append({k: v for k, v in entry.items() if v is not None})
    snippet = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True) if entries else ""
    return {"waiver_snippet": snippet}


def _reject(state: State) -> State:
    """Record who rejected and why, and what to do next. No model call."""
    items = []
    for change in _blocking(state):
        where = f"{change.get('operation') or ''} {change.get('path') or ''}".strip()
        fingerprint = f" (fingerprint {change['fingerprint']})" if change.get("fingerprint") else ""
        items.append(f"Fix `{change.get('id')}` on {where}: {change.get('text')}{fingerprint}")
    if state.get("migration"):
        items.append(f"Suggested route (model): {state['migration']}")
    items += [
        "Push the fix; the gate checks the branch again on the next build.",
        "If the break turns out to be intended, add a waiver to waivers.yaml in the "
        "pull request (fingerprint, reason, approver, expiry) and have it reviewed.",
    ]
    return {"checklist": items}


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

    if state.get("qa"):
        lines += ["## Questions asked before deciding", ""]
    for item in state.get("qa") or []:
        tools = f" _(looked at: {', '.join(item['tools'])})_" if item.get("tools") else ""
        lines += [f"**Q:** {item.get('question')}", "", f"**A:** {item.get('answer')}{tools}", ""]

    if state.get("decision") == "reject":
        lines += [
            f"## Rejected by {state.get('decided_by')}",
            "",
            f"> {state.get('reason')}",
            "",
            "Nothing was shipped. To move forward:",
            "",
        ]
        lines += [f"{i}. {item}" for i, item in enumerate(state.get("checklist") or [], 1)]
        lines.append("")
    elif state.get("approved_by"):
        lines += [f"Approved to ship by **{state['approved_by']}**."]
        if state.get("reason"):
            lines += ["", f"> {state['reason']}"]
        lines.append("")
        if state.get("waiver_snippet"):
            lines += [
                "### Add this to waivers.yaml",
                "",
                "This approval covers this build only. Commit these entries in the "
                "pull request so the next build passes without another approval "
                f"(they expire {state.get('expires')}):",
                "",
                "```yaml",
                state["waiver_snippet"].rstrip(),
                "```",
                "",
            ]

    lines += [
        "<sub>The band, rationale and explanation are model-generated and "
        "advisory. The verdict came from the deterministic checks and was fixed "
        "before any of this ran.</sub>",
    ]
    return {"report": "\n".join(lines)}

"""The advisory workflow, as a LangGraph.

    load_evidence -> classify_severity (LLM) -> decide -> [approval] -> render

**Why a graph rather than four function calls.** The checkpointer. Human
approval can pause the workflow for hours or days; a plain script would either
block a build agent that whole time or lose its place. A checkpointed graph
persists its state and resumes exactly where it stopped, in a different process
if need be. State plus interrupt plus resume is the one thing here that
genuinely needs a framework — and if a dependency cannot be justified that way
it should not be in the project.

**What `decide` may look at.** The deterministic results, and nothing else. The
LLM's classification is written into the state and then ignored by the node
that matters. That is demonstrable: delete the classify node and the report
changes while the verdict does not.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from api_guard.ai.evidence import Evidence

# Advisory bands. Not severities — oasdiff already assigned those, and this is
# a different question: how much human attention does it deserve?
Band = Literal["routine", "risky", "unknown"]

# A two-way label from a pre-parsed list is a small job, so it gets Groq's
# smaller, faster model rather than the one explain.py uses for prose.
# Separate variable so the two can be tuned independently.
DEFAULT_CLASSIFY_MODEL = "openai/gpt-oss-20b"


class Severity(BaseModel):
    band: Band = Field(description="routine, risky, or unknown.")
    rationale: str = Field(default="", description="One sentence, for a reviewer.")


class State(TypedDict, total=False):
    evidence_label: str
    verdict: str
    changes: list[dict]
    conformance_failures: list[str]
    waivers_applied: list[dict]

    # Written by the LLM. Read by render, never by decide.
    band: Band
    rationale: str

    blocked: bool
    needs_approval: bool
    approved_by: str | None
    report: str


def build(evidence: Evidence | None, *, checkpointer: Any | None = None):
    """Compile the workflow. Returns None if langgraph is not installed.

    `evidence` is None when resuming a saved run: the facts are already in the
    checkpoint, and loading them again would mean the pause did not really
    persist anything.
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return None

    graph = StateGraph(State)

    graph.add_node("load_evidence", _loader(evidence))
    graph.add_node("classify_severity", _classify)
    graph.add_node("decide", _decide)
    graph.add_node("human_approval", _approval)
    graph.add_node("render_report", _render)

    graph.add_edge(START, "load_evidence")
    graph.add_edge("load_evidence", "classify_severity")
    graph.add_edge("classify_severity", "decide")
    graph.add_conditional_edges(
        "decide",
        lambda s: "human_approval" if s.get("needs_approval") else "render_report",
        {"human_approval": "human_approval", "render_report": "render_report"},
    )
    graph.add_edge("human_approval", "render_report")
    graph.add_edge("render_report", END)

    return graph.compile(checkpointer=checkpointer)


def _loader(evidence: Evidence | None):
    def load_evidence(_state: State) -> State:
        if evidence is None:
            raise RuntimeError(
                "load_evidence ran on a resume-only graph; the checkpoint should "
                "already hold the evidence"
            )
        return {
            "evidence_label": evidence.describe(),
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

    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return {"band": "unknown", "rationale": "No model configured; not classified."}

    try:
        from langchain_groq import ChatGroq

        model = ChatGroq(
            api_key=key,
            model=os.environ.get("GROQ_CLASSIFY_MODEL", DEFAULT_CLASSIFY_MODEL),
            temperature=0,
            timeout=45,
            max_retries=1,
        )
        listed = "\n".join(
            f"- {c.get('id')}: {c.get('text')} ({c.get('operation')} {c.get('path')})"
            for c in changes[:8]
        )
        # json_schema, not the default tool calling: gpt-oss-20b lowercases the
        # tool name ("severity" for Severity) and Groq rejects the call.
        answer = model.with_structured_output(Severity, method="json_schema").invoke(
            "Classify how much human attention these API contract changes need.\n\n"
            f"{listed}\n\n"
            "'routine' if a reviewer would wave this through — additive, or a "
            "well-signposted deprecation. 'risky' if a consumer could plausibly "
            "break in a way the rules alone do not convey.\n\n"
            "This is advice for a human, not a decision. The build outcome is "
            "already settled."
        )
        if isinstance(answer, Severity):
            return {"band": answer.band, "rationale": answer.rationale}
    except Exception:  # noqa: BLE001 - advisory; see the module docstring
        pass

    return {"band": "unknown", "rationale": "Classification unavailable."}


def _decide(state: State) -> State:
    """The only node that determines anything.

    Reads `verdict` and `changes` — both produced by the deterministic checks.
    It does NOT read `band` or `rationale`, though both are sitting in the state
    right beside them. That restraint is the entire architecture, and
    test_boundaries.py plus test_graph.py keep it honest.
    """
    blocked = state.get("verdict") in ("failed", "error")

    # Approval is offered for a blocked build that someone might legitimately
    # want to push through — never as a way to bypass a tooling error, where
    # nothing was actually established.
    needs_approval = blocked and state.get("verdict") == "failed"

    return {"blocked": blocked, "needs_approval": needs_approval}


def _approval(state: State) -> State:
    """Pause for a human. This is Tier 3 of the change policy.

    interrupt() suspends the graph and persists it. Resuming is a separate
    call, possibly days later — which is exactly why a checkpointer earns its
    place here.
    """
    try:
        from langgraph.types import interrupt
    except ImportError:
        return {"approved_by": None}

    summary = (
        # All detected changes, not only those at the blocking threshold, so
        # "breaking" would overstate it; the gate's own summary gives that count.
        f"{len(state.get('changes', []))} contract change(s) detected. "
        f"Advisory band: {state.get('band', 'unknown')}. "
        f"{state.get('rationale', '')}\n\n"
        "Approve shipping this anyway?"
    )
    answer = interrupt({"question": summary, "changes": state.get("changes", [])})

    if isinstance(answer, dict):
        return {"approved_by": answer.get("approved_by")}
    return {"approved_by": str(answer) if answer else None}


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

    if state.get("approved_by"):
        lines += [f"Approved to ship by **{state['approved_by']}**.", ""]

    lines += [
        "<sub>The band and rationale above are model-generated and advisory. "
        "The verdict came from the deterministic checks and was fixed before "
        "any of this ran.</sub>",
    ]
    return {"report": "\n".join(lines)}

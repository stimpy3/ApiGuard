"""Running the review workflow across processes.

    api-guard review  --id 42                                   # checks, then the graph
    api-guard ask-review 42 "does this break the mobile app?"   # our agent answers
    api-guard approve 42 --by sohan --reason "..."              # ships + waiver to commit
    api-guard reject  42 --by sohan --reason "..."              # fails + fix checklist

The graph's state lives in a SQLite file, not in memory. `review` exits once
the graph pauses, so no build agent sits waiting for a human, and every later
command picks the run up from that file in a fresh process — possibly days
later.

Everything a human sends is validated here, before the graph resumes: a name
and a real reason for any decision, an expiry within policy for an approval,
and the question limit. The graph can then trust what it is given.

**What a decision does not do.** It does not change the verdict or the exit
code of the run that was reviewed; result.json is untouched. Approval records
a named sign-off and produces waiver entries to commit; whether a signed-off
failure may ship is the pipeline's call.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from api_guard.ai import graph as workflow
from api_guard.ai.evidence import LocalEvidence

if TYPE_CHECKING:
    from api_guard.verdict import RunResult

DEFAULT_STATE = Path(".api-guard") / "reviews.db"
DEFAULT_EXPIRES_IN = 30
_MIN_REASON = 10  # the same bar as a hand-written waiver's reason


class ReviewError(Exception):
    """The review could not be started or resumed. Said plainly."""


@dataclass
class Outcome:
    review_id: str
    paused: bool
    question: str = ""
    report: str = ""
    approved_by: str | None = None
    decision: str = ""
    answer: str = ""
    waiver_snippet: str = ""
    checklist: list[str] = field(default_factory=list)


@contextmanager
def _checkpointer(state: Path) -> Iterator:
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:
        raise ReviewError(
            "review needs the AI extra: pip install 'api-guard[ai]'"
        ) from exc

    state.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(state)) as saver:
        yield saver


def _config(review_id: str) -> dict:
    return {"configurable": {"thread_id": review_id}}


def start(
    result: RunResult,
    *,
    state: Path = DEFAULT_STATE,
    review_id: str | None = None,
    fail_on: str = "ERR",
    max_waiver_days: int = 90,
) -> Outcome:
    """Run the workflow on a finished check run, stopping if approval is needed.

    `fail_on` and `max_waiver_days` come from api-guard.yaml, so an approval
    later waives exactly the changes that blocked, within the waiver policy.
    """
    return _start(
        LocalEvidence(result), state=state, review_id=review_id,
        fail_on=fail_on, max_waiver_days=max_waiver_days,
    )


def start_archived(
    build_id: str,
    *,
    state: Path = DEFAULT_STATE,
    review_id: str | None = None,
    fail_on: str = "ERR",
    max_waiver_days: int = 90,
) -> Outcome:
    """Review a build that already finished, from its archived result.json.

    The evidence comes from Jenkins' archived artifacts, read the same way the
    MCP server's tools read them (McpEvidence). Useful when the approver is
    not the pipeline: hours later, on another machine, or for a CI system
    that could not pause.
    """
    from api_guard.ai import mcp_server
    from api_guard.ai.evidence import McpEvidence

    class _ArchivedReports:
        @staticmethod
        def get_report(build: str) -> dict:
            return mcp_server.load_report(build)

    evidence = McpEvidence(_ArchivedReports(), build_id)
    try:
        evidence.verdict()  # fetch now, so an unreachable Jenkins is a plain error
    except mcp_server.ReportUnavailable as exc:
        raise ReviewError(str(exc)) from exc
    return _start(
        evidence, state=state, review_id=review_id or f"build-{build_id}",
        fail_on=fail_on, max_waiver_days=max_waiver_days,
    )


def _start(evidence, *, state: Path, review_id: str | None, fail_on: str, max_waiver_days: int) -> Outcome:
    review_id = review_id or uuid.uuid4().hex[:8]

    with _checkpointer(state) as saver:
        compiled = workflow.build(evidence, checkpointer=saver)
        if compiled is None:
            raise ReviewError("review needs the AI extra: pip install 'api-guard[ai]'")

        # Reusing an id would silently fork an existing review, possibly one
        # already waiting on somebody's approval.
        if compiled.get_state(_config(review_id)).values:
            raise ReviewError(f"review {review_id!r} already exists in {state}")

        out = compiled.invoke(
            {"fail_on": str(fail_on), "max_waiver_days": int(max_waiver_days)},
            config=_config(review_id),
        )

    return _outcome(review_id, out)


def approve(
    review_id: str,
    approved_by: str,
    *,
    reason: str,
    expires_in_days: int = DEFAULT_EXPIRES_IN,
    state: Path = DEFAULT_STATE,
) -> Outcome:
    """Approve: this build ships, and the approver gets waiver entries to commit."""
    by = _name(approved_by, "an approval")
    reason = _reason(reason, "an approval")

    def check(values: dict) -> dict:
        limit = int(values.get("max_waiver_days", 90))
        if not 1 <= expires_in_days <= limit:
            raise ReviewError(
                f"--expires-in must be between 1 and {limit} days (policy.max_waiver_days). "
                "Pick the date the reason stops being true."
            )
        expires = (date.today() + timedelta(days=expires_in_days)).isoformat()
        return {"decision": "approve", "by": by, "reason": reason, "expires": expires}

    return _resume(review_id, state, check)


def reject(review_id: str, rejected_by: str, *, reason: str, state: Path = DEFAULT_STATE) -> Outcome:
    """Reject: the build fails, with who, why, and a checklist of what to do next."""
    by = _name(rejected_by, "a rejection")
    reason = reason.strip()
    if not reason:
        raise ReviewError("a rejection needs a reason: --reason <why>")
    return _resume(review_id, state, lambda _: {"decision": "reject", "by": by, "reason": reason})


def ask(review_id: str, question: str, *, state: Path = DEFAULT_STATE) -> Outcome:
    """Ask our agent about the paused review. The review pauses again after."""
    question = question.strip()
    if not question:
        raise ReviewError("ask-review needs a question")

    def check(values: dict) -> dict:
        if len(values.get("qa") or []) >= workflow.MAX_QUESTIONS:
            raise ReviewError(
                f"the limit of {workflow.MAX_QUESTIONS} questions is reached for review "
                f"{review_id!r}; approve or reject it"
            )
        return {"decision": "question", "question": question}

    return _resume(review_id, state, check)


@dataclass
class Summary:
    """One review, as `review list` and the web inbox show it."""

    review_id: str
    status: str  # "waiting" | "approved" | "rejected" | "no decision needed"
    verdict: str = ""
    band: str = ""
    changes: int = 0
    commit: str = ""
    question: str = ""
    questions_asked: int = 0
    started: str = ""
    updated: str = ""


def _status(values: dict, waiting: bool) -> str:
    if waiting:
        return "waiting"
    return {"approve": "approved", "reject": "rejected"}.get(values.get("decision", ""), "no decision needed")


def _summary(review_id: str, snapshot, history: list) -> Summary:
    values = snapshot.values
    waiting = bool(snapshot.next)
    return Summary(
        review_id=review_id,
        status=_status(values, waiting),
        verdict=values.get("verdict", ""),
        band=values.get("band", ""),
        changes=len(values.get("changes") or []),
        commit=str((values.get("context") or {}).get("commit", ""))[:7],
        question=workflow.approval_question(values) if waiting else "",
        questions_asked=len(values.get("qa") or []),
        started=history[-1].created_at if history else "",
        updated=history[0].created_at if history else "",
    )


def list_reviews(*, state: Path = DEFAULT_STATE) -> list[Summary]:
    """Every review in the state file, waiting ones first, newest first."""
    if not state.exists():
        return []
    with _checkpointer(state) as saver:
        compiled = workflow.build(None, checkpointer=saver)
        ids = []
        for item in saver.list(None):
            thread = item.config["configurable"]["thread_id"]
            if thread not in ids:
                ids.append(thread)
        summaries = []
        for review_id in ids:
            config = _config(review_id)
            summaries.append(_summary(review_id, compiled.get_state(config), list(compiled.get_state_history(config))))
    newest_first = sorted(summaries, key=lambda s: s.updated, reverse=True)
    return sorted(newest_first, key=lambda s: s.status != "waiting")  # stable: waiting on top


def show(review_id: str, *, state: Path = DEFAULT_STATE) -> str:
    """The audit trail of one review, as text.

    Built from the saved checkpoints: what the evidence was, what the model
    said, every question and answer, and who decided what, when. Nothing here
    is regenerated.
    """
    if not state.exists():
        raise ReviewError(f"no review state at {state}")
    with _checkpointer(state) as saver:
        compiled = workflow.build(None, checkpointer=saver)
        config = _config(review_id)
        snapshot = compiled.get_state(config)
        if not snapshot.values:
            raise ReviewError(f"no review {review_id!r} in {state}")
        history = list(compiled.get_state_history(config))

    v = snapshot.values
    s = _summary(review_id, snapshot, history)
    context = v.get("context") or {}
    lines = [
        f"Review {review_id}: {s.status.upper()}",
        f"  started  {s.started}",
        f"  updated  {s.updated}",
        f"  commit   {context.get('commit', '-')}   branch {context.get('branch', '-')}",
        "",
        f"Evidence (rules): verdict {v.get('verdict')}, {s.changes} change(s)",
    ]
    for c in v.get("changes") or []:
        lines.append(f"  - [{c.get('severity', 'ERR')}] {c.get('operation') or ''} {c.get('path') or ''}: "
                     f"{c.get('text')} (fingerprint {c.get('fingerprint')})")
    lines += ["", f"Triage (model): {v.get('band', '-')} - {v.get('rationale', '')}"]
    if v.get("impact"):
        lines += [f"Explanation (model: {v.get('explain_model')}):",
                  f"  What breaks: {v.get('impact')}",
                  f"  Safer route: {v.get('migration')}"]
    for i, item in enumerate(v.get("qa") or [], 1):
        tools = f" [tools: {', '.join(item['tools'])}]" if item.get("tools") else ""
        lines += ["", f"Question {i}: {item.get('question')}", f"  Answer (model){tools}: {item.get('answer')}"]
    lines.append("")
    if v.get("decision") == "approve":
        lines += [f"Decision: APPROVED by {v.get('decided_by')}", f"  reason:  {v.get('reason')}",
                  f"  waiver expires {v.get('expires')}"]
    elif v.get("decision") == "reject":
        lines += [f"Decision: REJECTED by {v.get('decided_by')}", f"  reason: {v.get('reason')}"]
    elif s.status == "waiting":
        lines.append(f"Decision: waiting ({workflow.MAX_QUESTIONS - s.questions_asked} question(s) left)")
    else:
        lines.append("Decision: none needed (the build was not blocked)")
    lines.append(f"\n{len(history)} saved checkpoint(s).")
    return "\n".join(lines)


def _resume(review_id: str, state: Path, payload) -> Outcome:
    if not state.exists():
        raise ReviewError(f"no review state at {state}")

    with _checkpointer(state) as saver:
        # No evidence: everything the remaining nodes need is in the saved
        # state. If loading ever ran again on resume, this would raise.
        compiled = workflow.build(None, checkpointer=saver)
        if compiled is None:
            raise ReviewError("this needs the AI extra: pip install 'api-guard[ai]'")

        snapshot = compiled.get_state(_config(review_id))
        if not snapshot.values:
            raise ReviewError(f"no review {review_id!r} in {state}")
        if not snapshot.next:
            raise ReviewError(f"review {review_id!r} is not waiting for a decision")

        from langgraph.types import Command

        out = compiled.invoke(Command(resume=payload(snapshot.values)), config=_config(review_id))

    return _outcome(review_id, out)


def _name(name: str, what: str) -> str:
    name = name.strip()
    if not name:
        raise ReviewError(f"{what} needs a name: --by <who>")
    return name


def _reason(reason: str, what: str) -> str:
    reason = reason.strip()
    if len(reason) < _MIN_REASON:
        raise ReviewError(
            f"{what} needs a reason of at least {_MIN_REASON} characters: it becomes the "
            "waiver's reason, which a reviewer has to be able to evaluate"
        )
    return reason


def _outcome(review_id: str, out: dict) -> Outcome:
    qa = out.get("qa") or []
    answer = qa[-1].get("answer", "") if qa else ""
    interrupts = out.get("__interrupt__") or []
    if interrupts:
        value = getattr(interrupts[0], "value", {}) or {}
        return Outcome(
            review_id=review_id, paused=True, question=str(value.get("question", "")), answer=answer
        )
    return Outcome(
        review_id=review_id,
        paused=False,
        report=out.get("report", ""),
        approved_by=out.get("approved_by"),
        decision=out.get("decision", ""),
        answer=answer,
        waiver_snippet=out.get("waiver_snippet", ""),
        checklist=list(out.get("checklist") or []),
    )

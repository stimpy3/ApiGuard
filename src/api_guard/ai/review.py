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
    review_id = review_id or uuid.uuid4().hex[:8]

    with _checkpointer(state) as saver:
        compiled = workflow.build(LocalEvidence(result), checkpointer=saver)
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

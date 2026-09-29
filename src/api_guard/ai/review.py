"""Running the approval workflow across processes.

    api-guard review  --id 42      # checks, then the graph; pauses if blocked
    api-guard approve 42 --by sohan  # later, anywhere with the same state file

The graph's state lives in a SQLite file, not in memory. `review` exits once
the graph pauses, so no build agent sits waiting for a human, and `approve`
picks the run up from that file in a fresh process — possibly days later.

**What approval does not do.** It does not change the verdict or the exit code
of the run that was reviewed; result.json is untouched. It records a named
human's sign-off in the rendered report. Whether a signed-off failure may ship
is the pipeline's call, made explicitly, not something this module decides.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from api_guard.ai import graph as workflow
from api_guard.ai.evidence import LocalEvidence

if TYPE_CHECKING:
    from api_guard.verdict import RunResult

DEFAULT_STATE = Path(".api-guard") / "reviews.db"


class ReviewError(Exception):
    """The review could not be started or resumed. Said plainly."""


@dataclass
class Outcome:
    review_id: str
    paused: bool
    question: str = ""
    report: str = ""
    approved_by: str | None = None


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


def start(result: RunResult, *, state: Path = DEFAULT_STATE, review_id: str | None = None) -> Outcome:
    """Run the workflow on a finished check run, stopping if approval is needed."""
    review_id = review_id or uuid.uuid4().hex[:8]

    with _checkpointer(state) as saver:
        compiled = workflow.build(LocalEvidence(result), checkpointer=saver)
        if compiled is None:
            raise ReviewError("review needs the AI extra: pip install 'api-guard[ai]'")

        # Reusing an id would silently fork an existing review, possibly one
        # already waiting on somebody's approval.
        if compiled.get_state(_config(review_id)).values:
            raise ReviewError(f"review {review_id!r} already exists in {state}")

        out = compiled.invoke({}, config=_config(review_id))

    return _outcome(review_id, out)


def approve(review_id: str, approved_by: str, *, state: Path = DEFAULT_STATE) -> Outcome:
    """Resume a paused review with a named approver."""
    if not approved_by.strip():
        raise ReviewError("an approval needs a name: --by <who>")
    if not state.exists():
        raise ReviewError(f"no review state at {state}")

    with _checkpointer(state) as saver:
        # No evidence: everything the remaining nodes need is in the saved
        # state. If loading ever ran again on resume, this would raise.
        compiled = workflow.build(None, checkpointer=saver)
        if compiled is None:
            raise ReviewError("approve needs the AI extra: pip install 'api-guard[ai]'")

        snapshot = compiled.get_state(_config(review_id))
        if not snapshot.values:
            raise ReviewError(f"no review {review_id!r} in {state}")
        if not snapshot.next:
            raise ReviewError(f"review {review_id!r} is not waiting for approval")

        from langgraph.types import Command

        out = compiled.invoke(
            Command(resume={"approved_by": approved_by.strip()}),
            config=_config(review_id),
        )

    return _outcome(review_id, out)


def _outcome(review_id: str, out: dict) -> Outcome:
    interrupts = out.get("__interrupt__") or []
    if interrupts:
        value = getattr(interrupts[0], "value", {}) or {}
        return Outcome(review_id=review_id, paused=True, question=str(value.get("question", "")))
    return Outcome(
        review_id=review_id,
        paused=False,
        report=out.get("report", ""),
        approved_by=out.get("approved_by"),
    )

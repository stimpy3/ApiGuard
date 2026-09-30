"""The approval workflow survives a process restart.

test_graph.py shows pause and resume inside one process with an in-memory
saver. That does not support the claim the graph exists for — "approval can
arrive days later, from somewhere else" — because an in-memory saver dies with
its process. These tests run each half in a separate Python process sharing
only a SQLite file on disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite", reason="needs the AI extra")

SRC = Path(__file__).parent.parent / "src"

# Process A: a failed check run, handed to the workflow, which pauses.
START = """
from pathlib import Path
from api_guard.ai import review
from api_guard.results import Change, CheckResult, Status
from api_guard.verdict import decide
from api_guard.policy import WaiverOutcome

result = decide(
    [CheckResult(name="breaking", status=Status.FAILED, summary="1 breaking change")],
    [Change(id="response-required-property-removed", text="removed the required property `email`",
            operation="GET", path="/users/{id}", fingerprint="abc123")],
    WaiverOutcome(),
    {},
)
out = review.start(result, state=Path(STATE), review_id="42")
print(json.dumps({"paused": out.paused, "question": out.question}))
"""

# Process B: a fresh interpreter. Loading or classifying again would raise,
# so a clean finish proves resume started from the saved step.
APPROVE = """
from pathlib import Path
from api_guard.ai import graph, review

def must_not_run(_state):
    raise AssertionError("classify_severity ran again on resume")

graph._classify = must_not_run
out = review.approve("42", "sohan", reason="Both clients migrated, PROD-142.", state=Path(STATE))
print(json.dumps({"paused": out.paused, "report": out.report, "approved_by": out.approved_by}))
"""


def _run(script: str, state: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}  # no network
    env["PYTHONPATH"] = str(SRC)
    code = f"import json\nSTATE = {str(state)!r}\n" + textwrap.dedent(script)
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_approval_resumes_in_a_different_process(tmp_path: Path) -> None:
    state = tmp_path / "reviews.db"

    first = _run(START, state)
    assert first["paused"] is True
    assert "Approve (ships this build" in first["question"]
    assert state.exists(), "the pause must be written to disk, not held in memory"

    second = _run(APPROVE, state)
    assert second["paused"] is False
    assert second["approved_by"] == "sohan"
    # Facts gathered by process A, rendered by process B.
    assert "/users/{id}" in second["report"]
    assert "Approved to ship by **sohan**" in second["report"]
    assert "No model configured" in second["report"], "band from process A should survive"


def test_cannot_approve_twice(tmp_path: Path) -> None:
    from api_guard.ai import review

    state = tmp_path / "reviews.db"
    _run(START, state)
    _run(APPROVE, state)

    with pytest.raises(review.ReviewError, match="not waiting for a decision"):
        review.approve("42", "someone-else", reason="a perfectly good reason here", state=state)


def test_unknown_review_is_reported_plainly(tmp_path: Path) -> None:
    from api_guard.ai import review

    state = tmp_path / "reviews.db"
    _run(START, state)

    with pytest.raises(review.ReviewError, match="no review 'nope'"):
        review.approve("nope", "sohan", reason="a perfectly good reason here", state=state)


def test_review_id_cannot_be_reused(tmp_path: Path) -> None:
    """Reusing an id would fork a review that may be waiting on someone."""
    state = tmp_path / "reviews.db"
    _run(START, state)

    env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
    env["PYTHONPATH"] = str(SRC)
    code = f"import json\nSTATE = {str(state)!r}\n" + textwrap.dedent(START)
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert done.returncode != 0
    assert "already exists" in done.stderr


def _failed_result():
    from api_guard.policy import WaiverOutcome
    from api_guard.results import Change, CheckResult, Status
    from api_guard.verdict import decide

    return decide(
        [CheckResult(name="breaking", status=Status.FAILED, summary="1 breaking change")],
        [Change(id="response-required-property-removed", text="removed `email`",
                operation="GET", path="/users", fingerprint="abc123")],
        WaiverOutcome(),
        {},
    )


def test_cli_signals_ci_through_the_approval_request_file(tmp_path: Path, monkeypatch) -> None:
    """CI decides whether to pause by the file's presence, so it must be exact:
    written when paused, removed after a run that needs no approval."""
    from api_guard import cli
    from api_guard.verdict import decide
    from api_guard.policy import WaiverOutcome

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "openapi.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "api-guard.yaml").write_text("spec:\n  path: openapi.yaml\n", encoding="utf-8")
    request = tmp_path / "api-guard-report" / "approval-request.md"

    monkeypatch.setattr(cli, "_check", lambda *a, **k: _failed_result())
    assert cli.main(["review", "--id", "7"]) == 1, "review keeps the gate's exit code"
    assert "Review 7" in request.read_text(encoding="utf-8")

    monkeypatch.setattr(cli, "_check", lambda *a, **k: decide([], [], WaiverOutcome(), {}))
    assert cli.main(["review", "--id", "8"]) == 0
    assert not request.exists(), "a stale request would make CI wait for nothing"

    assert cli.main(["approve", "7", "--by", "sohan", "--reason", "Both clients migrated, PROD-142."]) == 0
    assert "Approved to ship by **sohan**" in (
        tmp_path / "api-guard-report" / "review.md"
    ).read_text(encoding="utf-8")


def test_approval_needs_a_name(tmp_path: Path) -> None:
    from api_guard.ai import review

    state = tmp_path / "reviews.db"
    _run(START, state)

    with pytest.raises(review.ReviewError, match="needs a name"):
        review.approve("42", "  ", reason="a perfectly good reason here", state=state)

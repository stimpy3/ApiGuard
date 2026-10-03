"""The three human routes out of a paused review: approve, reject, question.

In-process with a real SQLite file (the two-process guarantee is covered in
test_review.py). The agent is stubbed wherever an answer is needed, so no
network and no key.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from api_guard.ai import graph, review
from api_guard.policy import WaiverOutcome, load_waivers
from api_guard.results import Change, CheckResult, Status
from api_guard.verdict import decide

pytest.importorskip("langgraph.checkpoint.sqlite", reason="needs the AI extra")

REASON = "Both clients migrated to phone, confirmed on PROD-142."


def _blocked_result():
    """One ERR change that blocks, one WARN change that does not."""
    return decide(
        [CheckResult(name="breaking", status=Status.FAILED, summary="1 breaking change")],
        [
            Change(id="response-required-property-removed", text="removed `email`",
                   operation="GET", path="/users", fingerprint="abc123"),
            Change(id="request-property-removed", text="removed request property `email`",
                   operation="POST", path="/users", fingerprint="warn99", severity="WARN"),
        ],
        WaiverOutcome(),
        {"commit": "94cf4b0c9573", "branch": "demo/rename"},
    )


@pytest.fixture
def paused(tmp_path: Path) -> Path:
    state = tmp_path / "reviews.db"
    outcome = review.start(_blocked_result(), state=state, review_id="42")
    assert outcome.paused
    return state


# --- approve -> waiver to commit ---------------------------------------------


def test_approval_produces_a_waiver_that_loads(paused: Path, tmp_path: Path) -> None:
    """The snippet must be a real, valid waiver for exactly the blocking change."""
    outcome = review.approve("42", "sohan", reason=REASON, expires_in_days=30, state=paused)

    assert outcome.decision == "approve" and outcome.approved_by == "sohan"
    entries = yaml.safe_load(outcome.waiver_snippet)
    assert [e["fingerprint"] for e in entries] == ["abc123"], "only the change that blocked"

    waivers_file = tmp_path / "waivers.yaml"
    waivers_file.write_text(outcome.waiver_snippet, encoding="utf-8")
    [waiver] = load_waivers(waivers_file)
    assert waiver.approved_by == "sohan" and waiver.reason == REASON
    assert waiver.expires == date.today() + timedelta(days=30)

    assert "Add this to waivers.yaml" in outcome.report
    assert "abc123" in outcome.report


def test_approval_needs_a_real_reason(paused: Path) -> None:
    with pytest.raises(review.ReviewError, match="at least 10 characters"):
        review.approve("42", "sohan", reason="ok", state=paused)


def test_approval_expiry_stays_within_policy(paused: Path) -> None:
    with pytest.raises(review.ReviewError, match="between 1 and 90"):
        review.approve("42", "sohan", reason=REASON, expires_in_days=365, state=paused)


def test_policy_from_the_config_reaches_the_approval(tmp_path: Path) -> None:
    """fail_on WARN makes the WARN change blocking too; max days comes along."""
    state = tmp_path / "reviews.db"
    review.start(_blocked_result(), state=state, review_id="7", fail_on="WARN", max_waiver_days=10)

    with pytest.raises(review.ReviewError, match="between 1 and 10"):
        review.approve("7", "sohan", reason=REASON, expires_in_days=30, state=state)
    outcome = review.approve("7", "sohan", reason=REASON, expires_in_days=10, state=state)
    assert {e["fingerprint"] for e in yaml.safe_load(outcome.waiver_snippet)} == {"abc123", "warn99"}


# --- reject -> checklist -----------------------------------------------------


def test_rejection_records_who_why_and_what_next(paused: Path) -> None:
    outcome = review.reject("42", "lead", reason="Billing still reads email.", state=paused)

    assert outcome.decision == "reject" and not outcome.paused
    assert any("abc123" in item for item in outcome.checklist)
    assert "Rejected by lead" in outcome.report
    assert "Billing still reads email." in outcome.report
    assert not outcome.waiver_snippet, "a rejection never produces a waiver"

    with pytest.raises(review.ReviewError, match="not waiting for a decision"):
        review.approve("42", "sohan", reason=REASON, state=paused)


def test_rejection_needs_a_reason(paused: Path) -> None:
    with pytest.raises(review.ReviewError, match="reason"):
        review.reject("42", "lead", reason="  ", state=paused)


# --- question loop -----------------------------------------------------------


def test_question_is_answered_from_the_saved_facts_then_it_pauses_again(paused: Path, monkeypatch) -> None:
    from api_guard.ai import agent, llm

    seen = {}

    def fake_answer(question, facts):
        seen["facts"] = facts
        return agent.Answer(text="Yes: GET /users no longer returns email.", confidence="high")

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(agent, "answer_review", fake_answer)

    outcome = review.ask("42", "Does this break the mobile app?", state=paused)
    assert outcome.paused, "after an answer, the approver still has to decide"
    assert outcome.answer.startswith("Yes: GET /users")
    assert "abc123" in seen["facts"], "the agent starts from the review's saved facts"
    assert "Does this break the mobile app?" in outcome.question
    assert "4 left" in outcome.question

    final = review.approve("42", "sohan", reason=REASON, state=paused)
    assert "Does this break the mobile app?" in final.report, "the Q&A is part of the record"


def test_question_limit(paused: Path) -> None:
    for i in range(graph.MAX_QUESTIONS):
        assert review.ask("42", f"question {i}?", state=paused).paused
    with pytest.raises(review.ReviewError, match="limit of 5 questions"):
        review.ask("42", "one more?", state=paused)
    assert review.reject("42", "lead", reason="enough questions, no", state=paused).decision == "reject"


def test_agent_loop_never_uses_the_reviews_checkpointer(paused: Path, monkeypatch) -> None:
    """Regression: run inside the review graph, the agent inherited the review's
    sync SQLite saver and every question failed with NotImplementedError."""
    import asyncio

    from langchain_core.messages import AIMessage

    from api_guard.ai import agent, llm
    from test_agent import ScriptedModel, get_build_context

    def answer_with_real_loop(question, facts):
        model = ScriptedModel(messages=iter([
            AIMessage(content="", tool_calls=[{"name": "get_build_context", "args": {"build_id": "local"}, "id": "c1"}]),
            AIMessage(content="It breaks GET /users.\nConfidence: high - in the facts"),
        ]))
        model.seen = []
        compiled = agent.build_agent(model, [get_build_context])
        return asyncio.run(agent.run(compiled, question, facts=facts))

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(agent, "answer_review", answer_with_real_loop)
    outcome = review.ask("42", "What breaks?", state=paused)
    assert outcome.answer == "It breaks GET /users."


def test_with_ai_off_a_question_still_gets_an_honest_reply(paused: Path) -> None:
    outcome = review.ask("42", "What breaks?", state=paused)
    assert "AI is off" in outcome.answer
    assert outcome.paused


# --- phase 5: archived builds and the MCP review tools -----------------------


def test_archived_build_is_reviewed_from_its_result_json(tmp_path: Path, monkeypatch) -> None:
    """review --build N: the same graph, fed by McpEvidence instead of a live run."""
    from api_guard.ai import mcp_server

    archived = _blocked_result().model_dump(mode="json")
    monkeypatch.setattr(mcp_server, "load_report", lambda build: archived)

    state = tmp_path / "reviews.db"
    outcome = review.start_archived("17", state=state)
    assert outcome.paused and outcome.review_id == "build-17"
    assert "build 17 (via MCP)" in outcome.question
    assert "abc123" in outcome.question and "commit 94cf4b0" in outcome.question

    done = review.approve("build-17", "sohan", reason=REASON, state=state)
    assert "abc123" in done.waiver_snippet


def test_archived_build_that_cannot_be_fetched_is_a_plain_error(tmp_path: Path, monkeypatch) -> None:
    from api_guard.ai import mcp_server

    def unreachable(build):
        raise mcp_server.ReportUnavailable("Could not reach Jenkins")

    monkeypatch.setattr(mcp_server, "load_report", unreachable)
    with pytest.raises(review.ReviewError, match="Could not reach Jenkins"):
        review.start_archived("17", state=tmp_path / "reviews.db")


def test_mcp_tools_report_reviews_but_cannot_decide(paused: Path, monkeypatch) -> None:
    from api_guard.ai import mcp_server

    monkeypatch.setenv("API_GUARD_STATE", str(paused))
    [pending] = mcp_server.list_pending_reviews()
    assert pending["review_id"] == "42" and pending["status"] == "waiting"

    detail = mcp_server.get_review("42")
    assert "Review 42: WAITING" in detail["audit"]
    assert "error" in mcp_server.get_review("nope")

    review.approve("42", "sohan", reason=REASON, state=paused)
    assert mcp_server.list_pending_reviews() == [], "decided reviews drop off the pending list"


# --- audit trail and list ----------------------------------------------------


def test_list_puts_waiting_reviews_first(paused: Path) -> None:
    review.start(_blocked_result(), state=paused, review_id="43")
    review.reject("43", "lead", reason="not this time", state=paused)

    rows = review.list_reviews(state=paused)
    assert [(r.review_id, r.status) for r in rows] == [("42", "waiting"), ("43", "rejected")]
    assert rows[0].commit == "94cf4b0" and rows[0].changes == 2
    assert "Approve (ships this build" in rows[0].question


def test_show_is_the_audit_trail(paused: Path) -> None:
    review.ask("42", "What breaks?", state=paused)
    review.approve("42", "sohan", reason=REASON, state=paused)

    trail = review.show("42", state=paused)
    assert "Review 42: APPROVED" in trail
    assert "fingerprint abc123" in trail
    assert "Question 1: What breaks?" in trail
    assert f"reason:  {REASON}" in trail
    assert "saved checkpoint(s)" in trail


def test_cli_review_show_and_list(paused: Path, capsys) -> None:
    from api_guard import cli

    assert cli.main(["review", "list", "--state", str(paused)]) == 0
    assert "42" in capsys.readouterr().out
    assert cli.main(["review", "show", "42", "--state", str(paused)]) == 0
    assert "Review 42: WAITING" in capsys.readouterr().out
    assert cli.main(["review", "show", "nope", "--state", str(paused)]) == 2


def test_cli_json_for_the_editor(paused: Path, tmp_path: Path, capsys) -> None:
    """The VS Code panel reads these; one JSON document on stdout, errors too."""
    import json

    from api_guard import cli

    assert cli.main(["review", "list", "--json", "--state", str(paused)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(r["review_id"], r["status"]) for r in listed["reviews"]] == [("42", "waiting")]

    assert cli.main(["review", "show", "42", "--json", "--state", str(paused)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["status"] == "waiting" and shown["questions_left"] == graph.MAX_QUESTIONS
    assert [c["fingerprint"] for c in shown["change_list"]] == ["abc123", "warn99"]
    assert shown["branch"] == "demo/rename"

    assert cli.main(["review", "show", "nope", "--json", "--state", str(paused)]) == 2
    assert "nope" in json.loads(capsys.readouterr().out)["error"]

    report_dir = tmp_path / "out"
    assert cli.main(["approve", "42", "--by", "sohan", "--reason", REASON, "--json",
                     "--state", str(paused), "--report-dir", str(report_dir)]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["decision"] == "approve" and "abc123" in approved["waiver_snippet"]
    assert (report_dir / "review.md").exists()

    assert cli.main(["reject", "42", "--by", "sohan", "--reason", REASON, "--json",
                     "--state", str(paused)]) == 2
    assert "not waiting" in json.loads(capsys.readouterr().out)["error"]

"""The Streamlit page, driven headlessly with Streamlit's own AppTest.

The agent is replaced with a stub, so this checks the page — that a question
reaches agent.ask with the chosen job, and that confidence, warnings and the
caution are all shown — not the model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="needs the ui extra")
pytest.importorskip("langgraph", reason="needs the AI extra")

from streamlit.testing.v1 import AppTest  # noqa: E402

from api_guard.ai import agent  # noqa: E402

PAGE = str(Path(__file__).parent.parent / "src" / "api_guard" / "ai" / "ui.py")


@pytest.fixture
def asked(monkeypatch):
    calls: list[dict] = []

    def fake_ask(question, *, jenkins_url=None, job=None, on_step=None):
        calls.append({"question": question, "job": job})
        if on_step:
            on_step("get_report(1)")
        return agent.Answer(
            text="Build 1 removed `email` from GET /users.",
            steps=["get_report(1)"],
            confidence="medium",
            confidence_reason="the cause is inferred",
            warnings=["Not found in the build data, possibly made up: deadbeef1234"],
        )

    monkeypatch.setattr(agent, "ask", fake_ask)
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    return calls


def test_example_button_asks_once_with_the_chosen_job(asked) -> None:
    app = AppTest.from_file(PAGE, default_timeout=30).run()
    app.text_input(key="job").set_value("sample-api-local/job/demo%252Fbreaking-rename").run()

    app.button(key="example-1").click().run()

    assert len(asked) == 1
    assert asked[0]["job"] == "sample-api-local/job/demo%252Fbreaking-rename"
    assert "conformance" in asked[0]["question"]

    page = " ".join(m.value for m in app.markdown)
    assert "removed `email`" in page
    assert "medium" in page and "the cause is inferred" in page
    assert any("deadbeef1234" in w.value for w in app.warning)
    assert any("can be wrong" in c.value for c in app.caption)

    # A later rerun (typing elsewhere) must not ask the example again.
    app.run()
    assert len(asked) == 1


def test_typed_question_is_asked(asked) -> None:
    app = AppTest.from_file(PAGE, default_timeout=30).run()
    app.chat_input[0].set_value("why did build 7 fail?").run()
    assert asked[-1]["question"] == "why did build 7 fail?"


def test_missing_key_is_shown(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("api_guard.ai.llm.api_key", lambda: None)
    app = AppTest.from_file(PAGE, default_timeout=30).run()
    assert any("GROQ_API_KEY" in e.value for e in app.error)

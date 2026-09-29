"""The shared LLM layer: fallbacks, one copy of the rules, small prompts.

No network: models are stand-ins. What is checked is the plumbing every AI
feature now depends on.
"""

from __future__ import annotations

import pytest

from api_guard.ai import llm


class RateLimitError(Exception):
    """Named like groq's, which is how llm.is_rate_limit recognises it."""


class Fake:
    def __init__(self, name: str) -> None:
        self.model_name = name


@pytest.fixture
def models(monkeypatch):
    """Pretend a key exists; model_for returns a stand-in named after the model."""
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "model_for", lambda role, name=None: Fake(name or llm.model_name(role)))


# --- fallback on rate limits ------------------------------------------------


def test_rate_limit_moves_to_the_other_groq_model(models) -> None:
    tried: list[str] = []

    def call(model):
        tried.append(model.model_name)
        if model.model_name == llm.SMALL:
            raise RateLimitError("429")
        return "answer"

    assert llm.with_fallback("triage", call) == "answer"
    assert tried == [llm.SMALL, llm.LARGE]


def test_both_models_limited_gives_none_so_callers_show_facts_only(models) -> None:
    def call(model):
        raise RateLimitError("429")

    assert llm.with_fallback("explain", call) is None


def test_other_errors_are_not_swallowed_as_rate_limits(models) -> None:
    """A real bug must surface to the caller, not silently hop models."""

    def call(model):
        raise ValueError("bad schema")

    with pytest.raises(ValueError):
        llm.with_fallback("triage", call)


def test_no_key_means_no_call(monkeypatch) -> None:
    monkeypatch.setattr(llm, "available", lambda: False)
    assert llm.with_fallback("triage", lambda m: pytest.fail("called")) is None


def test_unknown_provider_is_not_available(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "not-real")
    monkeypatch.setenv("AI_PROVIDER", "somethingelse")
    assert llm.available() is False


def test_each_role_has_its_own_setting(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_AGENT_MODEL", "custom-model")
    assert llm.model_name("agent") == "custom-model"
    assert llm.model_name("explain") == llm.LARGE
    assert llm._other("custom-model") is None, "no guessed fallback for a custom model"


# --- one copy of the rules --------------------------------------------------


def test_every_prompt_uses_the_same_policy_rules() -> None:
    """Three hand-kept copies had drifted before; now there is one."""
    from api_guard.ai import agent, explain

    assert llm.POLICY_RULES in agent.SYSTEM
    assert llm.POLICY_RULES in explain._prompt([
        {"id": "response-required-property-removed", "text": "removed email",
         "severity": "ERR", "operation": "GET", "path": "/users"}
    ])
    assert "IS the breaking step and needs a waiver" in llm.POLICY_RULES
    assert "there is no\n  `x-deprecated`" in llm.POLICY_RULES


# --- small prompts ----------------------------------------------------------


def change(id_="response-required-property-removed", path="/users", severity="ERR", text="removed email"):
    return {"id": id_, "path": path, "operation": "GET", "severity": severity,
            "text": text, "fingerprint": "abc123"}


def test_repeats_are_merged_and_worst_comes_first() -> None:
    compact, omitted = llm.compact_changes([
        change(severity="WARN", id_="request-property-removed"),
        change(), change(text="removed email again"),
    ])
    assert omitted == 0
    assert [c["severity"] for c in compact] == ["ERR", "WARN"]
    assert compact[0]["count"] == 2


def test_fingerprints_never_reach_the_prompt() -> None:
    """The model is never asked to repeat them; a mistyped one matches nothing."""
    text = llm.format_changes(*llm.compact_changes([change()]))
    assert "abc123" not in text
    assert "[ERR] GET /users: removed email (rule: response-required-property-removed)" in text


def test_limit_reports_what_was_left_out() -> None:
    many = [change(path=f"/r{i}") for i in range(11)]
    compact, omitted = llm.compact_changes(many, limit=8)
    assert len(compact) == 8 and omitted == 3
    assert "3 less severe change(s) not shown" in llm.format_changes(compact, omitted)


def test_fit_trims_to_budget_and_says_so() -> None:
    assert llm.fit("short", 100) == "short"
    trimmed = llm.fit("x" * 10_000, 100)
    assert llm.estimate_tokens(trimmed) <= 100
    assert "trimmed" in trimmed


def test_gpt_oss_uses_json_schema_mode() -> None:
    """Tool-calling mode made gpt-oss-20b lowercase the schema's name."""
    seen = {}

    class M(Fake):
        def with_structured_output(self, schema, method):
            seen["method"] = method

    llm.structured(M(llm.SMALL), object)
    assert seen["method"] == "json_schema"

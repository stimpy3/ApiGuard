"""The LangGraph workflow, and the guarantee it is supposed to provide.

The headline test is `test_verdict_is_identical_without_the_llm_node`: run the
graph normally, then run it with the classification node removed entirely, and
assert the decision is byte-identical. That is "the LLM only explains" made
checkable rather than asserted.
"""

from __future__ import annotations

import pytest

from api_guard.ai import graph as g

pytest.importorskip("langgraph", reason="needs the AI extra")


class FakeEvidence:
    """Stands in for either LocalEvidence or McpEvidence.

    That a fake satisfies the same interface is the point: the graph never
    learns which side its facts came from.
    """

    def __init__(self, verdict="failed", changes=None):
        self._verdict = verdict
        self._changes = changes if changes is not None else [
            {
                "id": "response-required-property-removed",
                "text": "removed the required property `email`",
                "operation": "GET",
                "path": "/users",
                "fingerprint": "aaa1",
            }
        ]

    def verdict(self):
        return self._verdict

    def changes(self):
        return list(self._changes)

    def conformance_failures(self):
        return []

    def waivers_applied(self):
        return []

    def describe(self):
        return "fake evidence"


def run(evidence, monkeypatch, band="risky"):
    """Run the graph with the model stubbed — no network, no key, no cost."""
    monkeypatch.setattr(
        g, "_classify", lambda state: {"band": band, "rationale": "stubbed"}
    )
    compiled = g.build(evidence)
    assert compiled is not None
    return compiled.invoke({})


def test_blocked_build_is_reported_as_blocked(monkeypatch) -> None:
    state = run(FakeEvidence(verdict="failed"), monkeypatch)
    assert state["blocked"] is True


def test_passing_build_is_not_blocked(monkeypatch) -> None:
    state = run(FakeEvidence(verdict="passed", changes=[]), monkeypatch)
    assert state["blocked"] is False


def test_tool_error_does_not_offer_approval(monkeypatch) -> None:
    """Nothing was established, so there is nothing to approve.

    Letting someone click through a tooling failure would turn 'api-guard could
    not run' into 'shipped with sign-off', which is worse than a red build.
    """
    state = run(FakeEvidence(verdict="error"), monkeypatch)
    assert state["blocked"] is True
    assert state["needs_approval"] is False


@pytest.mark.parametrize("band", ["routine", "risky", "unknown"])
def test_llm_band_cannot_change_the_decision(monkeypatch, band: str) -> None:
    """Whatever the model says, the decision is the same."""
    state = run(FakeEvidence(verdict="failed"), monkeypatch, band=band)
    assert state["blocked"] is True
    assert state["needs_approval"] is True


def test_verdict_is_identical_without_the_llm_node(monkeypatch) -> None:
    """Delete the classifier entirely; the decision must not move.

    This is the demonstrable version of the design rule, and the thing to show
    an examiner: the report loses its band and rationale, and nothing else
    changes.

    Uses a passing build with sub-threshold changes so the graph runs to
    completion. A blocked build suspends at the approval interrupt before
    rendering, which is correct but leaves no report to compare — that path is
    covered by test_blocked_build_pauses_for_approval.
    """
    evidence = FakeEvidence(verdict="passed")

    with_llm = run(evidence, monkeypatch, band="routine")

    # Now a graph whose classification node contributes nothing at all.
    monkeypatch.setattr(g, "_classify", lambda state: {})
    without_llm = g.build(evidence).invoke({})

    decision_keys = ("verdict", "blocked", "needs_approval")
    assert {k: with_llm.get(k) for k in decision_keys} == {
        k: without_llm.get(k) for k in decision_keys
    }, "removing the model changed the decision — the boundary has been breached"

    assert with_llm["report"] != without_llm["report"], (
        "the report should lose the advisory band; if it did not, the model "
        "was contributing nothing and the node is dead weight"
    )


def test_blocked_build_pauses_for_approval(monkeypatch) -> None:
    """A blocked build suspends rather than rendering a verdict nobody signed.

    The pause is the point of using a graph: with a checkpointer the state
    survives, so approval can arrive hours later from another process. A plain
    script would have to block a build agent for that whole time.
    """
    from langgraph.checkpoint.memory import MemorySaver

    monkeypatch.setattr(g, "_classify", lambda state: {"band": "risky", "rationale": "s"})
    compiled = g.build(FakeEvidence(verdict="failed"), checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "test-1"}}
    result = compiled.invoke({}, config=config)

    assert "__interrupt__" in result, "a blocked build should wait for a human"
    assert "report" not in result, "nothing should be rendered before approval"

    # The state persisted: resuming is a separate call, and the graph picks up
    # exactly where it stopped.
    from langgraph.types import Command

    resumed = compiled.invoke(Command(resume={"approved_by": "sohan"}), config=config)
    assert resumed["approved_by"] == "sohan"
    assert "Approved to ship by **sohan**" in resumed["report"]


def test_decide_never_reads_the_model_output() -> None:
    """Checked structurally, not by grepping.

    A behavioural test could pass by luck if the node read `band` and happened
    to reach the same answer anyway, so this looks at what the function
    actually accesses. It parses the AST rather than searching the text,
    because the docstring necessarily mentions `band` in order to explain why
    it is ignored — and a test that cannot tell those apart would either fail
    on a comment or have to ban explaining itself.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(g._decide)))

    accessed: set[str] = set()
    for node in ast.walk(tree):
        # state["band"]
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if isinstance(node.slice.value, str):
                accessed.add(node.slice.value)
        # state.get("band")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    accessed.add(first.value)

    forbidden = accessed & {"band", "rationale"}
    assert not forbidden, (
        f"_decide reads {sorted(forbidden)} from the state. The node that "
        "decides must use only the deterministic results, however tempting the "
        "model's opinion sitting right beside them."
    )
    assert "verdict" in accessed, "sanity check: _decide should read the verdict"

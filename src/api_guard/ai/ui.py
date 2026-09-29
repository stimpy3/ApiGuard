"""A small web page for `api-guard ask`.

    api-guard ui        # opens http://localhost:8501

Deliberately thin: everything it shows comes from agent.ask(), the same call
the CLI makes, so the page and the terminal cannot disagree. It adds three
things a terminal does badly — the investigation appearing step by step, the
model's confidence and the grounding checks shown next to the answer rather
than below it, and a history of the session's questions.

Like the CLI, it reads archived results and cannot change a verdict.
"""

from __future__ import annotations

import os

import streamlit as st

from api_guard.ai import agent

EXAMPLES = [
    "Why did build 1 fail, and what should the developer do?",
    "Did build 1 also fail conformance? Was it caused by the same change?",
    "Which waivers expire in the next 90 days, according to build 1?",
]

BADGE = {"high": "green", "medium": "orange", "low": "red"}


def _settings() -> tuple[str, str]:
    """On the page, not in the sidebar: Streamlit hides the sidebar on narrow
    screens, and the job setting decides which builds exist at all, so a
    hidden one makes "build 1 not found" look like a bug."""
    from api_guard.ai import llm

    st.session_state.setdefault("job", os.environ.get("JENKINS_JOB", "sample-api"))
    st.session_state.setdefault("url", os.environ.get("JENKINS_URL", "http://localhost:8081"))
    has_key = bool(llm.api_key())

    label = f"Looking at `{st.session_state.job}`" + ("" if has_key else " · no Groq key")
    with st.expander(label, expanded=not has_key):
        st.text_input("Jenkins URL", key="url")
        st.text_input(
            "Jenkins job",
            key="job",
            help="For a multibranch job: sample-api-local/job/demo%252Fbreaking-rename",
        )
        st.caption(f"Model: `{llm.model_name('agent')}`")
        if not has_key:
            st.error("No GROQ_API_KEY in the environment or .env.")
    return st.session_state.url, st.session_state.job


def _show(entry: dict) -> None:
    answer: agent.Answer = entry["answer"]
    with st.chat_message("user"):
        st.write(entry["question"])
    with st.chat_message("assistant"):
        if answer.steps:
            st.caption("Investigated: " + " → ".join(f"`{s}`" for s in answer.steps))
        st.markdown(answer.text)

        if answer.confidence:
            colour = BADGE.get(answer.confidence, "gray")
            reason = f" — {answer.confidence_reason}" if answer.confidence_reason else ""
            st.markdown(f"**Model's confidence:** :{colour}[{answer.confidence}]{reason}")
        else:
            st.markdown("**Model's confidence:** :gray[not stated]")
        for warning in answer.warnings:
            st.warning(warning, icon="⚠️")

        st.caption(agent.Answer.CAUTION + " The confidence is the model's own rating and is not calibrated.")


def main() -> None:
    st.set_page_config(
        page_title="api-guard ask",
        page_icon="🛡️",
        layout="centered",
    )
    st.title("Ask about a build")
    st.caption(
        "Groq investigates api-guard's archived results with read-only tools, "
        "choosing what to look at until it can answer."
    )

    url, job = _settings()
    history: list[dict] = st.session_state.setdefault("history", [])

    for entry in history:
        _show(entry)

    # Buttons rather than pills: a button is true for exactly one rerun, so an
    # example is asked once, with no selection state to clear afterwards.
    picked = None
    for i, example in enumerate(EXAMPLES):
        if st.button(example, key=f"example-{i}", type="tertiary"):
            picked = example
    question = st.chat_input("Ask about a build, e.g. why did build 1 fail?") or picked

    if not question:
        return

    with st.status("Investigating…", expanded=True) as status:
        def on_step(step: str) -> None:
            st.write(f"Calling `{step}`")

        try:
            answer = agent.ask(question, jenkins_url=url, job=job, on_step=on_step)
        except agent.AskError as exc:
            status.update(label="Could not answer", state="error")
            st.error(str(exc))
            return
        status.update(label=f"Done: {len(answer.steps)} tool call(s)", state="complete", expanded=False)

    history.append({"question": question, "answer": answer})
    _show(history[-1])


main()

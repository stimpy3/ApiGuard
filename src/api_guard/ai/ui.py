"""A small web page for `api-guard ask` and for pending reviews.

    api-guard ui        # opens http://localhost:8501

Deliberately thin: everything it shows comes from the same calls the CLI
makes (agent.ask, and review.approve / reject / ask), so the page, the
terminal and the Jenkins form cannot disagree.

Two tabs:
- Ask about a build: the investigation appearing step by step, the model's
  confidence and grounding checks next to the answer, and the session's
  history.
- Pending reviews: every review waiting for a decision, with the full context
  and approve / reject / ask buttons. This is the approval route for CI
  systems that cannot pause a job, like GitHub Actions.

Like the CLI, nothing here can change a verdict.
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
    ask_tab, inbox_tab = st.tabs(["Ask about a build", "Pending reviews"])
    with inbox_tab:
        _inbox()
    with ask_tab:
        _ask_tab(url, job)


def _inbox() -> None:
    """Reviews waiting for a decision, with the same three choices as the CLI.

    Calls review.approve / reject / ask directly, so the page, the CLI and the
    Jenkins form all resume the same saved graph with the same validation.
    """
    from pathlib import Path

    from api_guard.ai import review

    st.session_state.setdefault("state_path", os.environ.get("API_GUARD_STATE", str(review.DEFAULT_STATE)))
    state = Path(st.text_input("Review state file", key="state_path"))
    try:
        reviews = review.list_reviews(state=state)
    except review.ReviewError as exc:
        st.error(str(exc))
        return

    waiting = [r for r in reviews if r.status == "waiting"]
    if not waiting:
        st.info("No reviews are waiting for a decision.")
    for r in waiting:
        title = f"Review {r.review_id} · {r.changes} change(s)" + (f" · commit {r.commit}" if r.commit else "")
        with st.expander(title, expanded=True):
            st.code(r.question, language=None, wrap_lines=True)
            rid = r.review_id
            name = st.text_input("Your name", key=f"name-{rid}")
            text = st.text_area("Reason (approve or reject), or your question", key=f"text-{rid}")
            days = st.number_input("Waiver expires in (days, approve only)", 1, 365, 30, key=f"days-{rid}")
            approve, reject, ask = st.columns(3)
            try:
                if approve.button("Approve", key=f"approve-{rid}", type="primary"):
                    done = review.approve(rid, name, reason=text, expires_in_days=int(days), state=state)
                    st.success(f"Approved by {done.approved_by}. Commit this to waivers.yaml "
                               "so the next build passes without approval:")
                    st.code(done.waiver_snippet or "(nothing to waive)", language="yaml")
                if reject.button("Reject", key=f"reject-{rid}"):
                    done = review.reject(rid, name, reason=text, state=state)
                    st.warning(f"Rejected by {name}. Next steps:")
                    st.markdown("\n".join(f"{i}. {item}" for i, item in enumerate(done.checklist, 1)))
                if ask.button("Ask", key=f"ask-{rid}"):
                    with st.spinner("Our agent is looking…"):
                        done = review.ask(rid, text, state=state)
                    st.markdown(f"**Answer:** {done.answer}")
                    st.caption(agent.Answer.CAUTION)
            except review.ReviewError as exc:
                st.error(str(exc))

    decided = [r for r in reviews if r.status != "waiting"]
    if decided:
        st.subheader("Decided")
        st.dataframe(
            [{"id": r.review_id, "status": r.status, "verdict": r.verdict, "band": r.band,
              "changes": r.changes, "commit": r.commit, "updated": r.updated[:19]} for r in decided],
            hide_index=True,
        )


def _ask_tab(url: str, job: str) -> None:
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

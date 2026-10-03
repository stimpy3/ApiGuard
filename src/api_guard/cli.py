"""Command-line entry point.

    api-guard check --config api-guard.yaml
    api-guard review --id 42              # check, then pause for approval if blocked
    api-guard approve 42 --by <name>      # resume it, from any later process

Exit codes follow the tools we wrap, so any CI understands them unaided:

    0  the contract is intact (or every breach is waived)
    1  the contract would break consumers
    2  api-guard could not reach a conclusion - bad config, missing tool

Keeping 1 and 2 distinct matters more than it looks. A typo'd URL reported as
"breaking change detected" sends somebody hunting for a change that does not
exist, and after that happens twice people stop believing the gate.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from api_guard import report, specs
from api_guard.checks import breaking, conformance, freshness
from api_guard.config import Config, ConfigError, load
from api_guard.policy import PolicyError, Waiver, load_waivers
from api_guard.results import Change, CheckResult, Status
from api_guard.verdict import EXIT_TOOL_ERROR, RunResult, decide


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="api-guard",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared by `check` and `review`: review is a check followed by the
    # approval workflow, so it takes exactly the same inputs.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=Path("api-guard.yaml"),
        help="Path to api-guard.yaml (default: ./api-guard.yaml).",
    )
    common.add_argument(
        "--generated-spec",
        type=Path,
        default=None,
        help=(
            "A spec the pipeline already generated, to compare the committed one "
            "against. Use this from the Docker image, which has no access to your "
            "project's dependencies. Takes precedence over spec.generate_cmd."
        ),
    )
    common.add_argument(
        "--only",
        default=None,
        metavar="CHECK[,CHECK]",
        help=(
            "Run only these checks: freshness, breaking, conformance. The "
            "post-deploy smoke test wants conformance alone — freshness was "
            "already settled during the build, and re-asking it against a "
            "deployed container is meaningless."
        ),
    )
    common.add_argument(
        "--url",
        default=None,
        help=(
            "Override runtime.url. The same config is used against the ephemeral "
            "test stack during the build and against staging after deploying, and "
            "those are different addresses."
        ),
    )
    common.add_argument(
        "--base",
        default=None,
        help=(
            "Override spec.base: what to compare against (git:<ref>, a file or a "
            "URL). On a build of main itself, origin/main is this commit, so the "
            "breaking check would compare the spec with itself; pass the last "
            "deployed commit instead, e.g. --base git:<previous-commit>."
        ),
    )
    common.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Add an LLM-written impact analysis to the report. Advisory only: "
            "it runs after the verdict and cannot change it."
        ),
    )

    sub.add_parser("check", parents=[common], help="Run the contract checks.")

    init = sub.add_parser(
        "init",
        help=(
            "Set up api-guard in this project: find the spec and framework, then write "
            "api-guard.yaml, waivers.yaml and the CI config. Never overwrites without --force."
        ),
    )
    init.add_argument("--ci", choices=["auto", "github", "jenkins", "none"], default="auto",
                      help="Which CI to set up (default: whatever the project already uses).")
    init.add_argument("--force", action="store_true", help="Replace files that already exist.")
    init.add_argument("--dry-run", action="store_true", help="Show what would be written, write nothing.")
    init.add_argument("--dir", type=Path, default=Path("."), help="Project folder (default: here).")
    init.add_argument("--json", action="store_true", help="Print the result as JSON (for tools such as the VS Code extension).")

    review = sub.add_parser(
        "review",
        parents=[common],
        help=(
            "Run the checks, then the approval workflow. A blocked build pauses "
            "and saves its state; the exit code is the same as `check`."
        ),
    )
    review.add_argument(
        "--id",
        dest="review_id",
        default=None,
        help="Name for this review, e.g. the CI build number (default: random).",
    )
    review.add_argument(
        "--state",
        type=Path,
        default=None,
        help="SQLite file holding paused reviews (default: .api-guard/reviews.db).",
    )
    review.add_argument(
        "action",
        nargs="?",
        choices=["show", "list"],
        help=(
            "Instead of running the checks: `review list` shows every saved "
            "review, waiting ones first; `review show ID` prints one review's "
            "audit trail."
        ),
    )
    review.add_argument("target", nargs="?", help="The review id, for `review show`.")
    review.add_argument(
        "--build",
        default=None,
        metavar="N",
        help=(
            "Review an archived Jenkins build instead of running the checks: "
            "the evidence is its result.json (JENKINS_URL / JENKINS_JOB, or --job)."
        ),
    )
    review.add_argument("--job", default=None, help="Jenkins job path, for --build.")

    approve = sub.add_parser(
        "approve",
        help=(
            "Approve a paused review: this build may ship, and review.md gets "
            "waiver entries to commit so the next build passes too. The "
            "reviewed run's verdict is unchanged."
        ),
    )
    approve.add_argument("review_id", help="The id printed by `api-guard review`.")
    approve.add_argument("--by", required=True, help="Who is approving.")
    approve.add_argument(
        "--reason",
        required=True,
        help="Why this break is acceptable (at least 10 characters). Becomes the waiver's reason.",
    )
    approve.add_argument(
        "--expires-in",
        type=int,
        default=30,
        metavar="DAYS",
        help="Days until the waiver expires (default 30, at most policy.max_waiver_days).",
    )

    reject = sub.add_parser(
        "reject", help="Reject a paused review: record who and why, with a checklist of next steps."
    )
    reject.add_argument("review_id", help="The id printed by `api-guard review`.")
    reject.add_argument("--by", required=True, help="Who is rejecting.")
    reject.add_argument("--reason", required=True, help="Why it can't ship.")

    ask_review = sub.add_parser(
        "ask-review",
        help=(
            "Ask our agent a question about a paused review before deciding. It "
            "answers from the saved review, using tools only if needed."
        ),
    )
    ask_review.add_argument("review_id", help="The id printed by `api-guard review`.")
    ask_review.add_argument("question", nargs="+", help='e.g. "does this break the mobile app?"')

    for decision in (approve, reject, ask_review):
        decision.add_argument("--state", type=Path, default=None, help="Same file as `review --state`.")
        decision.add_argument(
            "--report-dir",
            type=Path,
            default=Path("api-guard-report"),
            help="Where review.md and approval-request.md go (default: api-guard-report).",
        )

    ask = sub.add_parser(
        "ask",
        help=(
            "Ask about past builds. Groq investigates using the MCP server's "
            "read-only tools. Needs GROQ_API_KEY; never affects a verdict."
        ),
    )
    ask.add_argument("question", nargs="+", help='e.g. "why did build 42 fail?"')
    ask.add_argument(
        "--job",
        default=None,
        help=(
            "Jenkins job path (default: $JENKINS_JOB or sample-api). For a "
            "multibranch job: sample-api-local/job/demo%%252Fbreaking-rename"
        ),
    )
    ask.add_argument("--jenkins-url", default=None, help="Default: $JENKINS_URL or http://localhost:8081.")

    ui = sub.add_parser("ui", help="Open a web page for `ask` (needs the ui extra: streamlit).")
    ui.add_argument("--port", type=int, default=8501)

    args = parser.parse_args(argv)

    if args.command == "ui":
        return _ui(args.port)

    if args.command == "ask":
        return _ask(" ".join(args.question), job=args.job, jenkins_url=args.jenkins_url)

    if args.command in ("approve", "reject", "ask-review"):
        return _decide(args)

    if args.command == "init":
        from api_guard import init as setup

        return setup.run(args.dir, ci=args.ci, force=args.force, dry_run=args.dry_run, as_json=args.json)

    if args.command == "review" and args.action:
        if args.action == "show" and not args.target:
            parser.error("review show needs a review id: api-guard review show 42")
        return _review_query(args.action, args.target, args.state)

    if args.command == "review" and args.build:
        return _review_archived(args)

    if args.command in ("check", "review"):
        selected = None
        if args.only is not None:
            selected = {name.strip() for name in args.only.split(",") if name.strip()}
            unknown = selected - ALL_CHECKS
            if unknown:
                parser.error(
                    f"unknown check(s): {', '.join(sorted(unknown))}. "
                    f"Choose from: {', '.join(sorted(ALL_CHECKS))}"
                )
        return _run_check(
            args.config,
            generated_spec=args.generated_spec,
            url=args.url,
            base=args.base,
            only=selected,
            explain=args.explain,
            review=(args.state, args.review_id) if args.command == "review" else None,
        )
    parser.error(f"unknown command {args.command}")
    return EXIT_TOOL_ERROR


ALL_CHECKS = {freshness.NAME, breaking.NAME, conformance.NAME}


def _skipped(name: str) -> CheckResult:
    return CheckResult(
        name=name,
        status=Status.SKIPPED,
        summary="not selected by --only",
    )


def _run_check(
    config_path: Path,
    *,
    generated_spec: Path | None,
    url: str | None,
    base: str | None = None,
    only: set[str] | None,
    explain: bool,
    review: tuple[Path | None, str | None] | None = None,
) -> int:
    if not config_path.exists() and config_path == Path("api-guard.yaml"):
        # No setup yet: run on defaults if a spec can be found, so the first
        # try needs nothing written.
        from api_guard.config import defaults_for

        config = defaults_for(Path.cwd())
        if config is None:
            print(
                "api-guard: no api-guard.yaml here, and no OpenAPI spec found in the usual "
                "places (openapi.yaml, docs/openapi.yaml, ...).\n"
                "Run `api-guard init` to set up, or pass --config.",
                file=sys.stderr,
            )
            return EXIT_TOOL_ERROR
        print(f"api-guard: no api-guard.yaml, using defaults (spec {config.spec.path.as_posix()}, "
              f"compared with {config.spec.base[4:]}). `api-guard init` writes them to a file.")
    else:
        try:
            config = load(config_path)
        except ConfigError as exc:
            print(f"api-guard: {exc}", file=sys.stderr)
            return EXIT_TOOL_ERROR

    if base is not None:
        config.spec.base = base

    if url is not None:
        if config.runtime is None:
            print(
                "api-guard: --url given but api-guard.yaml has no `runtime:` section, "
                "so there is nothing to check against it.",
                file=sys.stderr,
            )
            return EXIT_TOOL_ERROR
        config.runtime.url = url

    generated: bytes | None = None
    if generated_spec is not None:
        try:
            generated = generated_spec.read_bytes()
        except OSError as exc:
            print(f"api-guard: cannot read --generated-spec {generated_spec}: {exc}", file=sys.stderr)
            return EXIT_TOOL_ERROR

    try:
        waivers = _load_waivers(config)
    except PolicyError as exc:
        print(f"api-guard: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    result = _check(config, waivers, generated=generated, only=only)

    # Runs after the verdict, deliberately. The exit code below is already
    # fixed by this point, whatever the model does or fails to do.
    analysis = _explain(result) if explain else None

    written = report.write(
        result, config.resolve(config.report.dir), config.report.formats, analysis=analysis
    )

    _print_summary(result, written)

    if review is not None:
        _start_review(
            result, *review, report_dir=config.resolve(config.report.dir), policy=config.policy
        )

    return result.exit_code


def _load_waivers(config: Config) -> list[Waiver]:
    if config.policy.waivers is None:
        return []
    path = config.resolve(config.policy.waivers)
    if not path.exists():
        # An absent waivers file is the normal state for a healthy project, not
        # a misconfiguration.
        return []
    return load_waivers(path, max_days=config.policy.max_waiver_days)


def _check(
    config: Config,
    waivers: list[Waiver],
    *,
    generated: bytes | None = None,
    only: set[str] | None = None,
) -> RunResult:
    checks: list[CheckResult] = []
    changes: list[Change] = []
    waiver_outcome = None

    def wanted(name: str) -> bool:
        return only is None or name in only

    spec_path = config.resolve(config.spec.path)

    try:
        revision = specs.read_revision(spec_path)
    except specs.SpecError as exc:
        return decide(
            [CheckResult(name="spec", status=Status.ERROR, summary=str(exc))],
            [],
            _empty_waivers(),
            _meta(config),
        )

    if wanted(freshness.NAME):
        checks.append(
            freshness.run(
                revision,
                generated=generated,
                generate_cmd=config.spec.generate_cmd,
                root=config.root,
            )
        )
    else:
        checks.append(_skipped(freshness.NAME))

    if not wanted(breaking.NAME):
        checks.append(_skipped(breaking.NAME))
        base = None
    else:
        base = _resolve_base(config, spec_path, checks)

    if base is not None:
        result, changes, waiver_outcome = breaking.run(
            base, revision, config.policy, waivers, config.root
        )
        checks.append(result)

    if wanted(conformance.NAME):
        checks.append(conformance.run(config.runtime, spec_path, config.root))
    else:
        checks.append(_skipped(conformance.NAME))

    return decide(checks, changes, waiver_outcome or _empty_waivers(), _meta(config))


def _resolve_base(config: Config, spec_path: Path, checks: list[CheckResult]) -> bytes | None:
    """Fetch the previous contract, recording why if it cannot be had.

    Returns None when there is nothing to compare against. Appends the
    explanatory CheckResult itself, since "no base spec" and "base spec
    unreadable" are a skip and an error respectively, and only this function
    knows which happened.
    """
    try:
        return specs.read_base(config.spec.base, spec_path, config.root)
    except specs.BaseNotFound as exc:
        # Nothing to compare against is not a failure: on a repository's first
        # build there is no previous contract, so nothing can have broken.
        checks.append(
            CheckResult(
                name=breaking.NAME,
                status=Status.SKIPPED,
                summary=f"no base spec at {config.spec.base}",
                detail=str(exc),
            )
        )
    except specs.SpecError as exc:
        checks.append(
            CheckResult(name=breaking.NAME, status=Status.ERROR, summary=str(exc))
        )
    return None


def _empty_waivers():
    from api_guard.policy import WaiverOutcome

    return WaiverOutcome()


def _meta(config: Config) -> dict[str, str]:
    """Best-effort provenance. Never fails the run — this is context, not a check."""
    meta = {"spec": str(config.spec.path), "base": config.spec.base}
    for key, command in (
        ("commit", ["git", "rev-parse", "HEAD"]),
        ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
    ):
        try:
            done = subprocess.run(command, cwd=config.root, capture_output=True, timeout=10)
            if done.returncode == 0:
                meta[key] = done.stdout.decode("utf-8", errors="replace").strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return meta


def _explain(result: RunResult) -> str | None:
    """Ask the AI layer for an impact analysis, if it is installed.

    Imported inside the function, not at module scope, so the core gate carries
    no dependency on the AI layer and a missing extra degrades to a warning
    rather than taking the build down. test_boundaries.py checks that this stays
    true.
    """
    try:
        from api_guard.ai.explain import explain
    except ImportError:
        print(
            "api-guard: --explain needs the AI extra: pip install 'api-guard[ai]'",
            file=sys.stderr,
        )
        return None

    analysis = explain(result)
    if analysis is None:
        print(
            "api-guard: no explanation produced (no GROQ_API_KEY, or the "
            "provider was unavailable). The verdict above is unaffected.",
            file=sys.stderr,
        )
    return analysis


def _start_review(
    result: RunResult,
    state: Path | None,
    review_id: str | None,
    *,
    report_dir: Path,
    policy=None,
) -> None:
    """Run the approval workflow on a finished check.

    Like --explain, a failure here is reported and then ignored: the exit code
    was fixed by the checks, and a missing extra or a clashing id must not turn
    a contract violation into a tooling error or vice versa.

    Writes approval-request.md only when the review is paused. CI checks for
    that file rather than parsing console output, so a stale copy from an
    earlier run in the same workspace is removed first.
    """
    request = report_dir / "approval-request.md"
    request.unlink(missing_ok=True)

    try:
        from api_guard.ai import review as workflow
    except ImportError:
        print("api-guard: review needs the AI extra: pip install 'api-guard[ai]'", file=sys.stderr)
        return

    state = state or workflow.DEFAULT_STATE
    extra = {}
    if policy is not None:
        extra = {"fail_on": str(policy.fail_on), "max_waiver_days": policy.max_waiver_days}
    try:
        outcome = workflow.start(result, state=state, review_id=review_id, **extra)
    except workflow.ReviewError as exc:
        print(f"api-guard: review not started: {exc}", file=sys.stderr)
        return

    if outcome.paused:
        print(f"\n  Review {outcome.review_id} is waiting for a decision.")
        print("  " + outcome.question.replace("\n", "\n  "))
        _print_decision_help(outcome.review_id, state)
        _write_request(report_dir, outcome)
        return

    path = report_dir / "review.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(outcome.report, encoding="utf-8")
    print(f"\n  Review {outcome.review_id} complete, no approval needed: {path}")


def _print_decision_help(review_id: str, state: Path) -> None:
    print(
        f"\n  Decide with one of:\n"
        f"    api-guard approve {review_id} --by <name> --reason \"<why it's acceptable>\" --state {state}\n"
        f"    api-guard reject {review_id} --by <name> --reason \"<why not>\" --state {state}\n"
        f"    api-guard ask-review {review_id} \"<question>\" --state {state}"
    )


def _write_request(report_dir: Path, outcome) -> None:
    request = report_dir / "approval-request.md"
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text(f"Review {outcome.review_id}\n\n{outcome.question}\n", encoding="utf-8")


def _decide(args) -> int:
    """approve / reject / ask-review. Exit 0 once recorded, 2 if refused."""
    try:
        from api_guard.ai import review as workflow
    except ImportError:
        print(f"api-guard: {args.command} needs the AI extra: pip install 'api-guard[ai]'", file=sys.stderr)
        return EXIT_TOOL_ERROR

    state = args.state or workflow.DEFAULT_STATE
    try:
        if args.command == "approve":
            outcome = workflow.approve(
                args.review_id, args.by, reason=args.reason,
                expires_in_days=args.expires_in, state=state,
            )
        elif args.command == "reject":
            outcome = workflow.reject(args.review_id, args.by, reason=args.reason, state=state)
        else:
            outcome = workflow.ask(args.review_id, " ".join(args.question), state=state)
    except workflow.ReviewError as exc:
        print(f"api-guard: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    request = args.report_dir / "approval-request.md"
    if outcome.paused:
        # A question: the review is waiting again, and the request now
        # carries the answer, so CI shows it in the next form.
        _reconfigure_stdout()
        print(outcome.answer)
        print(
            "\n(Model-written from the saved review, advisory only. It can't approve "
            "or reject anything; you decide.)"
        )
        _write_request(args.report_dir, outcome)
        _print_decision_help(args.review_id, state)
        return 0

    request.unlink(missing_ok=True)
    out = args.report_dir / "review.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(outcome.report, encoding="utf-8")
    if outcome.decision == "reject":
        print(f"Review {args.review_id} rejected by {args.by}. Checklist in {out}:")
        for i, item in enumerate(outcome.checklist, 1):
            print(f"  {i}. {item}")
    else:
        print(f"Review {args.review_id} approved by {outcome.approved_by}: {out}")
        if outcome.waiver_snippet:
            print("\nAdd this to waivers.yaml in the pull request, so the next build passes too:\n")
            print(outcome.waiver_snippet)
    return 0


def _review_archived(args) -> int:
    """`review --build N`: the review workflow on an archived Jenkins build.

    No checks run: the verdict is the one that build already reached. Exit 0
    once the review is saved (paused or complete), 2 if it could not start.
    """
    import os

    if args.job:
        os.environ["JENKINS_JOB"] = args.job
    try:
        from api_guard.ai import review as workflow
    except ImportError:
        print("api-guard: review needs the AI extra: pip install 'api-guard[ai]'", file=sys.stderr)
        return EXIT_TOOL_ERROR

    state = args.state or workflow.DEFAULT_STATE
    try:
        outcome = workflow.start_archived(args.build, state=state, review_id=args.review_id)
    except workflow.ReviewError as exc:
        print(f"api-guard: review not started: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    _reconfigure_stdout()
    if outcome.paused:
        print(f"Review {outcome.review_id} (archived build {args.build}) is waiting for a decision.\n")
        print(outcome.question)
        _print_decision_help(outcome.review_id, state)
    else:
        print(f"Review {outcome.review_id} complete, no decision needed:\n\n{outcome.report}")
    return 0


def _review_query(action: str, review_id: str | None, state: Path | None) -> int:
    """`review list` and `review show ID`: read-only views of the saved reviews."""
    try:
        from api_guard.ai import review as workflow
    except ImportError:
        print("api-guard: review needs the AI extra: pip install 'api-guard[ai]'", file=sys.stderr)
        return EXIT_TOOL_ERROR

    state = state or workflow.DEFAULT_STATE
    _reconfigure_stdout()
    try:
        if action == "show":
            print(workflow.show(review_id, state=state))
            return 0
        reviews = workflow.list_reviews(state=state)
    except workflow.ReviewError as exc:
        print(f"api-guard: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    if not reviews:
        print(f"No reviews in {state}.")
        return 0
    print(f"{'ID':<10} {'STATUS':<19} {'VERDICT':<8} {'BAND':<8} {'CHANGES':>7}  {'COMMIT':<8} UPDATED")
    for r in reviews:
        print(f"{r.review_id:<10} {r.status:<19} {r.verdict:<8} {r.band:<8} {r.changes:>7}  "
              f"{r.commit or '-':<8} {r.updated[:19]}")
    return 0


def _reconfigure_stdout() -> None:
    # Model output contains typographic characters (narrow spaces, dashes) that
    # a Windows cp1252 console cannot encode. Degrade them, don't crash on them.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def _ask(question: str, *, job: str | None, jenkins_url: str | None) -> int:
    """Answer a question about past builds. Exit 0 with an answer, 2 otherwise."""
    try:
        from api_guard.ai import agent
    except ImportError:
        print("api-guard: ask needs the AI extra: pip install 'api-guard[ai]'", file=sys.stderr)
        return EXIT_TOOL_ERROR

    try:
        answer = agent.ask(question, job=job, jenkins_url=jenkins_url)
    except agent.AskError as exc:
        print(f"api-guard: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    # Model output contains typographic characters (narrow spaces, dashes) that
    # a Windows cp1252 console cannot encode. Degrade them, don't crash on them.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    if answer.steps:
        print("Investigated: " + " -> ".join(answer.steps) + "\n")
    print(answer.text)
    print()
    if answer.confidence:
        print(f"Model's confidence: {answer.confidence}"
              + (f" - {answer.confidence_reason}" if answer.confidence_reason else ""))
    else:
        print("Model's confidence: not stated")
    for warning in answer.warnings:
        print(f"Check: {warning}")
    print(f"\n({answer.CAUTION})")
    return 0


def _ui(port: int) -> int:
    """Start the Streamlit page. Streamlit runs a script file, not a function,
    so this hands it the module's path."""
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("api-guard: ui needs the ui extra: pip install 'api-guard[ai,ui]'", file=sys.stderr)
        return EXIT_TOOL_ERROR

    page = Path(__file__).parent / "ai" / "ui.py"
    return subprocess.call(
        [sys.executable, "-m", "streamlit", "run", str(page),
         "--server.port", str(port), "--browser.gatherUsageStats", "false",
         # Without this, a first run blocks on an interactive email prompt.
         "--server.headless", "true",
         # A local tool: no "Deploy to Streamlit Cloud" button.
         "--client.toolbarMode", "minimal"]
    )


_WHAT_FAILED = {
    "breaking": "changes that would break clients",
    "freshness": "the committed spec is out of date",
    "conformance": "the running API doesn't match its spec",
}


def headline(result: RunResult) -> str:
    """One line a person can act on, before any per-check detail."""
    if result.verdict is Status.ERROR:
        return "API contract: COULD NOT CHECK (a setup problem, not your API; details below)"
    if result.verdict is Status.FAILED:
        failed = [c for c in result.checks if c.status is Status.FAILED]
        return "API contract: BLOCKED - " + "; ".join(
            _WHAT_FAILED.get(c.name, c.summary) for c in failed
        )
    waived = f" ({len(result.waivers.applied)} change(s) waived)" if result.waivers.applied else ""
    return f"API contract: OK{waived}"


def _print_summary(result: RunResult, written: dict[str, Path]) -> None:
    print()
    print(headline(result))
    print()
    # The checks that ran, then the ones that didn't, on one line: "not
    # checked" is information, not a problem to shout about.
    for check in result.checks:
        if check.status is not Status.SKIPPED:
            print(f"  {check.status.value.upper():<8} {check.name:<12} {check.summary}")
    skipped = [c for c in result.checks if c.status is Status.SKIPPED]
    if skipped:
        print("  not checked: " + "; ".join(f"{c.name} ({c.summary})" for c in skipped))

    if result.waivers.applied:
        print(f"\n  {len(result.waivers.applied)} waived breaking change(s):")
        for waiver in result.waivers.applied:
            print(f"    - {waiver.describe()} (expires {waiver.expires.isoformat()})")

    if result.waivers.stale:
        print(f"\n  {len(result.waivers.stale)} stale waiver(s) matched nothing:")
        for waiver in result.waivers.stale:
            print(f"    - {waiver.describe()}")

    if result.waivers.expired:
        print(f"\n  WARNING: {len(result.waivers.expired)} expired waiver(s) were ignored. Remove them:")
        for waiver in result.waivers.expired:
            print(f"    - {waiver.describe()} (expired {waiver.expires.isoformat()})")

    for check in result.checks:
        if check.detail and check.status.is_blocking:
            print(f"\n{check.name}:\n{check.detail}")

    if written:
        print("\n  reports: " + ", ".join(str(p) for p in written.values()))
    print()


if __name__ == "__main__":
    raise SystemExit(main())

"""Check 3: does the running API still honour its spec?

Checks 1 and 2 only ever read files. This one sends real requests at a real
server, which is why it catches the failure the other two structurally cannot:
the spec is unchanged, so oasdiff sees nothing to compare, but the
implementation has drifted away from it — a handler now returns null for a
non-nullable field, or a status code the spec never mentions.

Schemathesis generates the requests from the spec itself, so no test cases have
to be written or maintained by hand. That is the whole appeal: the tests come
from the contract, so they cannot fall out of step with it.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from api_guard.config import RuntimeConfig
from api_guard.results import CheckResult, Status

NAME = "conformance"

# Phrases Schemathesis uses when it could not reach the API at all. It reports
# these as ordinary test failures and exits 1, identically to a real contract
# violation — so without this list an unreachable API is indistinguishable from
# a broken one, and somebody goes looking for a bug that does not exist.
_NETWORK_MARKERS = (
    "connection refused",
    "connection failed",
    "network error",
    "max retries exceeded",
    "failed to establish a new connection",
    "name or service not known",
    "connection reset",
    "read timed out",
)

# Beyond this, more examples of the same failure add nothing to a CI log.
_MAX_REPORTED = 10

# Generous: this runs after a container start, and the budget is bounded by
# max_examples anyway. Timing out mid-run tells us nothing useful.
_TIMEOUT = 900

# Schemathesis' documented exit codes.
_EXIT_PASS = 0
_EXIT_FAILED = 1
_EXIT_ERROR = 2


# --if-running: how long to knock before deciding the API simply isn't up.
_IF_RUNNING_WAIT = 3


def run(
    runtime: RuntimeConfig | None, spec_path: Path, root: Path, *, if_running: bool = False
) -> CheckResult:
    """Run Schemathesis against the live API.

    `if_running` is for places where the API may legitimately be down, like an
    editor: knock briefly, and if nothing answers report "not checked" instead
    of an error. CI leaves it off, because there the pipeline starts the API,
    so an API that isn't answering is a real problem worth stopping on.
    """
    if runtime is None:
        return CheckResult(
            name=NAME,
            status=Status.SKIPPED,
            summary="no runtime section configured",
            detail=(
                "Add a `runtime:` block with the base URL of the running API to "
                "have api-guard verify the implementation matches the spec."
            ),
        )

    binary = shutil.which("schemathesis")
    if binary is None:
        return CheckResult(
            name=NAME,
            status=Status.ERROR,
            summary="schemathesis is not installed",
            detail=(
                "Run api-guard from its Docker image, or install the extra: "
                "pip install 'api-guard[cli]'"
            ),
        )

    # Fail fast and clearly if the API is not there. Schemathesis would
    # otherwise run every generated case against a closed port and report a
    # wall of "connection refused" as contract failures.
    wait = min(_IF_RUNNING_WAIT, runtime.wait_for_schema) if if_running else runtime.wait_for_schema
    unreachable = _wait_for_api(runtime.url, wait)
    if unreachable is not None:
        if if_running:
            return CheckResult(
                name=NAME,
                status=Status.SKIPPED,
                summary=f"API not running at {runtime.url}",
                detail=(
                    "Conformance sends requests to the running API. Start it to have this "
                    f"checked too.\n\n{unreachable.detail}"
                ),
            )
        return unreachable

    with tempfile.TemporaryDirectory(prefix="api-guard-conformance-") as tmp:
        junit = Path(tmp) / "junit.xml"
        command = [
            binary,
            "run",
            str(spec_path),
            "--url",
            runtime.url,
            "--checks",
            ",".join(runtime.checks),
            "--max-examples",
            str(runtime.max_examples),
            "--wait-for-schema",
            str(runtime.wait_for_schema),
            "--report",
            "junit",
            "--report-junit-path",
            str(junit),
            "--no-color",
        ]
        if runtime.deterministic:
            command.append("--generation-deterministic")
        if runtime.seed is not None:
            command += ["--seed", str(runtime.seed)]

        try:
            # Run from the temp directory, not the project root. Schemathesis
            # writes a .schemathesis/ cache into its working directory, and the
            # working directory here is the caller's repository, mounted
            # read-write. A checker that leaves droppings in the thing it is
            # checking gets them committed by somebody eventually. The spec
            # path is absolute, so nothing depends on the cwd.
            completed = subprocess.run(
                command, cwd=tmp, capture_output=True, timeout=_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name=NAME,
                status=Status.ERROR,
                summary=f"schemathesis timed out after {_TIMEOUT}s",
                detail=(
                    "Lower `runtime.max_examples`, or check the API is not hanging "
                    "on some generated input."
                ),
            )

        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        failures = _parse_junit(junit)

    return _verdict(completed.returncode, failures, stdout, stderr, runtime)


def _verdict(
    code: int,
    failures: list[str],
    stdout: str,
    stderr: str,
    runtime: RuntimeConfig,
) -> CheckResult:
    if code == _EXIT_PASS:
        return CheckResult(
            name=NAME,
            status=Status.PASSED,
            summary=f"API matches its spec across {len(runtime.checks)} check(s)",
        )

    if code == _EXIT_FAILED:
        # Schemathesis exits 1 both for "your API violates its spec" and for
        # "I could not reach your API". Those need opposite responses from
        # whoever reads the build, so separate them before reporting.
        if failures and all(_is_network_error(f) for f in failures):
            return CheckResult(
                name=NAME,
                status=Status.ERROR,
                summary=f"could not reach the API at {runtime.url}",
                detail=(
                    f"Every request failed to connect, so nothing about the "
                    f"contract was actually tested.\n\n{_first(failures)}\n\n"
                    "This is an environment problem, not a contract violation. "
                    "Check the API is running and that the URL is reachable from "
                    "wherever api-guard is executing — inside a container, "
                    "'localhost' means the container itself, not the host."
                ),
            )

        detail = _summarise(failures) if failures else _tail(stdout)
        return CheckResult(
            name=NAME,
            status=Status.FAILED,
            summary=f"{len(failures) or 'some'} conformance failure(s)",
            detail=(
                f"{detail}\n\n"
                "The spec and the implementation disagree. Either the code is "
                "wrong, or the spec promises something the code never did."
            ),
        )

    # Exit 2 is Schemathesis saying it could not run — unreachable URL, invalid
    # schema. Reporting that as a contract violation would send somebody hunting
    # for a breaking change that does not exist, so it is an ERROR, not a FAILED.
    return CheckResult(
        name=NAME,
        status=Status.ERROR,
        summary="schemathesis could not run",
        detail=(
            f"Exit code {code}.\n\n{_tail(stderr) or _tail(stdout)}\n\n"
            f"Check the API is reachable at {runtime.url} and the spec is valid."
        ),
    )


def _parse_junit(path: Path) -> list[str]:
    """Pull failure messages out of the JUnit report.

    JUnit XML is a stable, boring format — safer to depend on than scraping the
    console output, which is written for humans and changes freely.
    """
    if not path.exists():
        return []
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return []

    messages: list[str] = []
    for case in tree.iter("testcase"):
        name = case.get("name", "operation")
        for tag in ("failure", "error"):
            for node in case.findall(tag):
                body = (node.text or node.get("message") or "").strip()
                messages.append(f"{name}\n{_tail(body, lines=25)}")
    return messages


def _wait_for_api(url: str, timeout: int) -> CheckResult | None:
    """Poll the base URL until something answers. None means it is up.

    Any HTTP response counts, including 404: we are checking that a server is
    listening, not that a particular route exists. Schemathesis' own
    --wait-for-schema does not help here, because with a local spec file it
    never fetches anything from the API before testing starts.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            urllib.request.urlopen(url, timeout=5)  # noqa: S310
            return None
        except urllib.error.HTTPError:
            return None  # it answered, which is all we needed
        except Exception as exc:  # noqa: BLE001 - any failure means "not yet"
            last = str(exc)
        if time.monotonic() >= deadline:
            break
        time.sleep(1)

    host = urlparse(url).hostname or url
    hint = ""
    if host in ("localhost", "127.0.0.1", "::1"):
        hint = (
            "\n\nThe URL points at 'localhost'. If api-guard is running in a "
            "container, that is the container itself. Use the service name when "
            "both run in one compose network (http://api:8000), or "
            "host.docker.internal to reach a port published on the host."
        )

    return CheckResult(
        name=NAME,
        status=Status.ERROR,
        summary=f"no API listening at {url} after {timeout}s",
        detail=(
            f"Last connection error: {last}{hint}\n\n"
            "Reported as an environment error rather than a contract failure — "
            "nothing was tested, so nothing can be concluded about the contract."
        ),
    )


def _is_network_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _NETWORK_MARKERS)


def _summarise(failures: list[str]) -> str:
    """Group identical failures instead of printing all of them.

    Property-based testing produces many examples of the same underlying
    problem. Sixty repetitions of one bug is not sixty bugs, and printing them
    all buries anything else that went wrong.
    """
    counts = Counter(_signature(f) for f in failures)
    lines: list[str] = []

    for signature, count in counts.most_common(_MAX_REPORTED):
        suffix = f"   (x{count})" if count > 1 else ""
        lines.append(f"{signature}{suffix}")

    if len(counts) > _MAX_REPORTED:
        lines.append(f"\n... and {len(counts) - _MAX_REPORTED} more distinct failure(s).")

    total = sum(counts.values())
    header = (
        f"{total} failing case(s), {len(counts)} distinct problem(s):\n"
        if total != len(counts)
        else ""
    )
    return header + "\n\n".join(lines)


def _signature(failure: str) -> str:
    """Collapse a failure to the part that identifies the problem.

    Generated payloads differ on every example, so keeping them would make
    every occurrence look unique and defeat the grouping.
    """
    return _tail(failure, lines=12)


def _first(failures: list[str]) -> str:
    return _tail(failures[0], lines=8) if failures else ""


def _tail(text: str, lines: int = 40) -> str:
    """Keep output readable in a CI log — the end is where the failure is."""
    stripped = text.strip()
    if not stripped:
        return ""
    split = stripped.splitlines()
    if len(split) <= lines:
        return stripped
    return "...\n" + "\n".join(split[-lines:])

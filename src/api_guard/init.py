"""`api-guard init`: write the setup files, instead of making a user write them.

    api-guard init              # detect, write, explain
    api-guard init --dry-run    # show what it would write
    api-guard init --ci github  # force a CI target (github | jenkins | none)

It looks at the project (project.py) and writes:

- api-guard.yaml   — spec path, base branch, and the generator command when
                     the framework is known (which switches freshness on)
- waivers.yaml     — empty, with a comment explaining what goes in it
- .github/workflows/api-guard.yml — when the project uses GitHub
- .gitignore lines — for api-guard's own output

For Jenkins it prints a stage to paste rather than editing a Jenkinsfile:
pipelines are hand-tuned, and rewriting one is not a setup tool's call.

It never overwrites an existing file unless --force is given, and everything
it writes is plain, commented YAML meant to be read and edited.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from api_guard import project as detect_project

GENERATED_SPEC = "api-guard-generated.json"
_GITIGNORE_LINES = ("api-guard-report/", ".api-guard/", GENERATED_SPEC)

# init's exit codes: 0 set up; 3 nothing written, because the project has no
# OpenAPI spec yet and no way to generate one. Distinct from check's 1 and 2,
# so a caller (CI, an editor extension) can show "add a spec first" as a step
# to take rather than as an error.
NEEDS_SPEC = 3


def run(
    root: Path, *, ci: str = "auto", force: bool = False, dry_run: bool = False, as_json: bool = False
) -> int:
    """Set the project up. With `as_json`, print one JSON object instead of text.

    The JSON form is for tools that drive init (the VS Code extension): the
    same decisions, as fields, plus the human text under "text".
    """
    if not as_json:
        return _run(root, ci=ci, force=force, dry_run=dry_run)[0]

    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code, info = _run(root, ci=ci, force=force, dry_run=dry_run)
    print(json.dumps({**info, "exit_code": code, "text": buffer.getvalue()}, indent=2))
    return code


def _run(root: Path, *, ci: str, force: bool, dry_run: bool) -> tuple[int, dict]:
    found = detect_project.detect(root)
    framework = found.framework
    cmd = framework.generate_cmd if framework else None

    if found.spec is None and cmd is None:
        # Nothing to guard yet. Writing a config that points at a file that
        # doesn't exist would be half a setup; say exactly what to do instead.
        stack, steps = detect_project.how_to_add_a_spec(found.root)
        print("api-guard init\n")
        print("  No OpenAPI spec found, and no way to generate one was detected.")
        print("  api-guard checks your API's OpenAPI spec, so it needs one first.\n")
        if stack:
            print(f"  Detected: {stack}")
        if framework and framework.note:
            print(f"  {framework.note}")
        print("\n  Next:")
        for i, line in enumerate(steps):
            prefix = "    1. " if i == 0 else "       "
            text = f"    {line[2:]}" if line.startswith("$ ") else line
            print(prefix + text)
        print("    2. Run `api-guard init` again: it will find the spec and finish the setup.")
        print("\n  Nothing was written.")
        return NEEDS_SPEC, {
            "status": "needs_spec",
            "stack": stack,
            "note": framework.note if framework else "",
            # "$ " marks a command line, so a UI can show it as code.
            "steps": steps,
            "written": [],
        }

    spec_rel = found.spec.relative_to(found.root).as_posix() if found.spec else (
        "openapi.json" if cmd else "openapi.yaml"
    )

    print("api-guard init\n")
    print(f"  spec        {spec_rel}" + ("" if found.spec else "   (not found yet)"))
    print(f"  framework   {framework.name if framework else 'not recognised'}")
    print(f"  compare to  origin/{found.default_branch}")
    targets = found.ci if ci == "auto" else ([] if ci == "none" else [ci])
    print(f"  CI          {', '.join(targets) or 'none detected'}\n")

    files: dict[str, str] = {
        "api-guard.yaml": _config(spec_rel, found.default_branch, cmd, framework),
        "waivers.yaml": _WAIVERS,
    }
    if "github" in targets:
        files[".github/workflows/api-guard.yml"] = _github(found, cmd)

    written, kept = [], []
    for rel, content in files.items():
        path = found.root / rel
        if path.exists() and not force:
            kept.append(rel)
            continue
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        written.append(rel)

    ignored = _gitignore(found.root, dry_run)

    verb = "would write" if dry_run else "wrote"
    for rel in written:
        print(f"  {verb:<12} {rel}")
    for rel in kept:
        print(f"  kept         {rel}   (exists; --force to replace)")
    if ignored:
        print(f"  {'would add' if dry_run else 'added':<12} {', '.join(ignored)} to .gitignore")
    if dry_run:
        print("\n--- api-guard.yaml ---\n" + files["api-guard.yaml"])

    print("\nWhat runs:")
    print("  breaking     on: your branch's spec vs the one on "
          f"{found.default_branch}, so changes that break clients are caught")
    if cmd:
        print("  freshness    on: the spec is regenerated from the code and compared, "
              "so a forgotten regenerate is caught")
    else:
        print("  freshness    off: no way to regenerate the spec was detected"
              + (f"\n               {framework.note}" if framework and framework.note else ""))
    print("  conformance  off: needs a running API; see `runtime` in api-guard.yaml")

    print("\nNext:")
    step = 1
    if not found.spec:
        if cmd:
            print(f"  {step}. Create the spec once:  {cmd} > {spec_rel}")
        else:
            print(f"  {step}. Add your OpenAPI spec at {spec_rel} (or change spec.path)")
        step += 1
    if "jenkins" in targets:
        print(f"  {step}. Add this stage to your Jenkinsfile:\n")
        print(_jenkins(cmd))
        step += 1
    print(f"  {step}. Try it:  docker run --rm -v \"$PWD:/work\" -w /work sohanbhadalkar/api-guard:1 check")
    print(f"  {step + 1}. Commit the new files.")
    return 0, {
        "status": "set_up",
        "spec": spec_rel,
        "spec_exists": found.spec is not None,
        "create_spec_cmd": f"{cmd} > {spec_rel}" if cmd and not found.spec else None,
        "framework": framework.name if framework else None,
        "generate_cmd": cmd,
        "base_branch": found.default_branch,
        "ci": targets,
        "checks": {"breaking": True, "freshness": bool(cmd), "conformance": False},
        "written": written,
        "kept": kept,
        "gitignore_added": ignored,
        "jenkins_stage": _jenkins(cmd) if "jenkins" in targets else None,
        "dry_run": dry_run,
    }


def _config(spec_rel: str, branch: str, cmd: str | None, framework) -> str:
    if cmd:
        generator = (
            f"  # Prints the spec from your code; turns the freshness check on.\n"
            f"  generate_cmd: {json.dumps(cmd)}\n"
        )
    else:
        note = f"  # {framework.note}\n" if framework and framework.note else ""
        generator = (
            "  # A command that prints your spec from the code would turn the\n"
            "  # freshness check on. None was detected.\n"
            f"{note}"
            "  # generate_cmd: \"...\"\n"
        )
    return f"""# api-guard settings. Written by `api-guard init` on {date.today().isoformat()}; edit freely.
#
#   breaking     always on: compares {spec_rel} on {branch} with your branch
#   freshness    {"on" if cmd else "off"}: is the committed spec still what the code produces?
#   conformance  off: does the running API keep its spec? (uncomment `runtime`)

spec:
  path: {spec_rel}
  base: "git:origin/{branch}"
{generator}
# runtime:
#   url: http://localhost:8000      # where your API runs in CI

policy:
  waivers: waivers.yaml
"""


_WAIVERS = """# Breaking changes that were made on purpose, and accepted.
#
# Empty is the healthy state. When a build is blocked by a change you meant to
# make, `api-guard approve` (or the Jenkins Approve button) prints the entry to
# paste here; you only supply who you are and why it's acceptable. Each entry
# expires (at most 90 days ahead), so old permissions can't approve new breaks.
#
# - fingerprint: "631dbccdc316"     # printed by the blocked build, never typed
#   reason: "PROD-142: both apps migrated to phone"
#   approved_by: your-name
#   expires: 2026-12-31
"""


def _github(found, cmd: str | None) -> str:
    steps = [
        "      - uses: actions/checkout@v4",
        "        with:",
        "          fetch-depth: 0   # the old spec is read from git history",
    ]
    action_with = ["          config: api-guard.yaml"]
    if cmd and found.framework.language == "python":
        install = (
            "pip install -r requirements.txt" if (found.root / "requirements.txt").is_file()
            else "pip install ."
        )
        steps += [
            "      - uses: actions/setup-python@v5",
            "        with:",
            "          python-version: '3.11'",
            f"      - run: {install}",
            "      - name: Generate the spec from the code",
            f"        run: {cmd} > {GENERATED_SPEC}",
        ]
        action_with.append(f"          generated-spec: {GENERATED_SPEC}")
    steps += ["      - uses: stimpy3/ApiGuard@main", "        with:", *action_with]
    body = "\n".join(steps)
    return f"""# Written by `api-guard init`. Checks every push and pull request for API
# changes that would break clients, and comments on the pull request.
name: api-guard

on:
  push:
    branches: [{found.default_branch}]
  pull_request:

permissions:
  contents: read
  pull-requests: write   # for the report comment

jobs:
  contract:
    runs-on: ubuntu-latest
    steps:
{body}
"""


def _jenkins(cmd: str | None) -> str:
    generate = f"        sh '{cmd} > {GENERATED_SPEC}'\n" if cmd and "'" not in cmd else ""
    if cmd and "'" in cmd:
        generate = f"        sh \"\"\"{cmd} > {GENERATED_SPEC}\"\"\"\n"
    flag = f" --generated-spec {GENERATED_SPEC}" if cmd else ""
    return (
        "    stage('API contract') {\n"
        "      steps {\n"
        f"{generate}"
        "        sh 'docker run --rm -v \"$PWD:/work\" -w /work sohanbhadalkar/api-guard:1 "
        f"check{flag}'\n"
        "      }\n"
        "    }\n"
        "\n  (If Jenkins itself runs in Docker, see the README's Jenkins section for --volumes-from.)"
    )


def _gitignore(root: Path, dry_run: bool) -> list[str]:
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [line for line in _GITIGNORE_LINES if line not in existing]
    if missing and not dry_run:
        prefix = "\n" if existing and existing[-1].strip() else ""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + "# api-guard output\n" + "\n".join(missing) + "\n")
    return missing

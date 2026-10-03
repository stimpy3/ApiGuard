"""Looking at a project to work out how api-guard should run in it.

Used by `api-guard init`, which writes the setup files, and by `check` when
there is no api-guard.yaml at all, which then runs on sensible defaults. Both
exist to cut setup friction: a user should not have to read documentation to
find out which three keys to write.

Everything here is read-only and deterministic — file names and dependency
lists, no model, no network. A detection that is unsure says so rather than
guessing, because a wrong generator command is worse than none: it would turn
the freshness check into a permanent false alarm.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Where specs usually live, most likely first.
_SPEC_NAMES = ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.yml", "swagger.json")
_SPEC_DIRS = ("", "docs", "api", "spec", "specs", "openapi", "src")
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", "target", ".tox"}


@dataclass
class Framework:
    name: str
    # A command that prints the spec to stdout, run from the project root in
    # the project's own environment. None when it cannot be known statically.
    generate_cmd: str | None = None
    # What the generator needs to run, for the CI setup.
    language: str = ""
    note: str = ""


@dataclass
class Project:
    root: Path
    spec: Path | None = None
    framework: Framework | None = None
    default_branch: str = "main"
    ci: list[str] = field(default_factory=list)  # "github", "jenkins"


def detect(root: Path) -> Project:
    root = root.resolve()
    return Project(
        root=root,
        spec=find_spec(root),
        framework=detect_framework(root),
        default_branch=_default_branch(root),
        ci=_ci_systems(root),
    )


def find_spec(root: Path) -> Path | None:
    """The committed OpenAPI file, by the usual names and places."""
    for folder in _SPEC_DIRS:
        for name in _SPEC_NAMES:
            candidate = root / folder / name
            if candidate.is_file() and _looks_like_openapi(candidate):
                return candidate
    return None


_TOP_LEVEL_YAML = re.compile(r'^["\']?(openapi|swagger)["\']?\s*:', re.MULTILINE)
_JSON_KEY = re.compile(r'"(openapi|swagger)"\s*:')


def _looks_like_openapi(path: Path) -> bool:
    """A top-level `openapi:` / `swagger:` key, anywhere in the file.

    Not just near the top: generators that sort keys put `components:` first,
    and in a real spec that runs to thousands of lines before `openapi:`.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:5_000_000]
    except OSError:
        return False
    if path.suffix == ".json":
        return bool(_JSON_KEY.search(text))
    return bool(_TOP_LEVEL_YAML.search(text))


# --- frameworks ----------------------------------------------------------------


def detect_framework(root: Path) -> Framework | None:
    for probe in (_fastapi, _django_spectacular, _spring_springdoc, _nest_swagger):
        found = probe(root)
        if found:
            return found
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _python_deps(root: Path) -> str:
    texts = [_read(p) for p in root.glob("requirements*.txt")]
    texts += [_read(root / name) for name in ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile")]
    return "\n".join(texts).lower()


def _python_files(root: Path):
    for path in root.rglob("*.py"):
        if not any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            yield path


_FASTAPI_APP = re.compile(r"^(\w+)\s*(?::\s*\w+\s*)?=\s*FastAPI\(", re.MULTILINE)


def _fastapi(root: Path) -> Framework | None:
    if "fastapi" not in _python_deps(root):
        return None
    for path in sorted(_python_files(root), key=lambda p: len(p.parts)):
        match = _FASTAPI_APP.search(_read(path))
        if match:
            module = ".".join(path.relative_to(root).with_suffix("").parts)
            variable = match.group(1)
            return Framework(
                name="FastAPI",
                language="python",
                generate_cmd=(
                    f"python -c \"import json; from {module} import {variable}; "
                    f"print(json.dumps({variable}.openapi()))\""
                ),
            )
    return Framework(
        name="FastAPI", language="python",
        note="FastAPI is a dependency, but no `app = FastAPI(...)` was found to export the spec from.",
    )


def _django_spectacular(root: Path) -> Framework | None:
    if "drf-spectacular" not in _python_deps(root) and "drf_spectacular" not in _python_deps(root):
        return None
    if not (root / "manage.py").is_file():
        return None
    return Framework(name="Django REST framework (drf-spectacular)", language="python",
                     generate_cmd="python manage.py spectacular")


def _spring_springdoc(root: Path) -> Framework | None:
    build = _read(root / "pom.xml") + _read(root / "build.gradle") + _read(root / "build.gradle.kts")
    if "springdoc" not in build:
        return None
    return Framework(
        name="Spring Boot (springdoc)", language="java",
        note=(
            "springdoc serves the spec from the running app at /v3/api-docs. Start the app "
            "in CI and set generate_cmd to: curl -fsS http://localhost:8080/v3/api-docs"
        ),
    )


def _nest_swagger(root: Path) -> Framework | None:
    if "@nestjs/swagger" not in _read(root / "package.json"):
        return None
    return Framework(
        name="NestJS (@nestjs/swagger)", language="node",
        note=(
            "NestJS builds the spec at runtime. Add a small script that creates the app "
            "and prints SwaggerModule.createDocument(...) as JSON, then set it as generate_cmd."
        ),
    )


# --- repository ----------------------------------------------------------------


def _default_branch(root: Path) -> str:
    try:
        done = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        if done.returncode == 0 and "/" in done.stdout:
            return done.stdout.strip().split("/", 1)[1]
        done = subprocess.run(
            ["git", "branch", "--list", "main", "master"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        names = [b.strip(" *") for b in done.stdout.splitlines() if b.strip()]
        if "main" not in names and "master" in names:
            return "master"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "main"


def _ci_systems(root: Path) -> list[str]:
    found = []
    if (root / ".github").is_dir():
        found.append("github")
    if (root / "Jenkinsfile").is_file():
        found.append("jenkins")
    return found

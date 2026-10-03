"""Less setup friction: content-based freshness, project detection, `init`,
running with no config, and the one-line headline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from api_guard import cli, project
from api_guard.checks import freshness
from api_guard.config import load
from api_guard.policy import WaiverOutcome, load_waivers
from api_guard.results import Change, CheckResult, Status
from api_guard.verdict import decide

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "users", "version": "1"},
    "paths": {"/users": {"get": {"responses": {"200": {"description": "ok"}}}}},
}


# --- freshness compares content, not bytes -------------------------------------


def test_json_from_the_code_matches_the_committed_yaml() -> None:
    """A generator printing JSON against a committed YAML file is the same contract."""
    generated = json.dumps(SPEC).encode()
    committed = yaml.safe_dump(SPEC, sort_keys=True).encode().replace(b"\n", b"\r\n")
    result = freshness.run(committed, generated=generated, root=Path("."))
    assert result.status is Status.PASSED


def test_a_real_difference_names_where() -> None:
    changed = json.loads(json.dumps(SPEC))
    changed["paths"]["/orders"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    result = freshness.run(yaml.safe_dump(SPEC).encode(), generated=json.dumps(changed).encode(),
                           root=Path("."))
    assert result.status is Status.FAILED
    assert "in the code, not in the committed spec: paths./orders" in result.detail


def test_generator_without_the_projects_packages_is_not_checked_rather_than_failed(tmp_path: Path) -> None:
    """The normal case in the Docker image on a laptop: report it, don't fail on it."""
    result = freshness.run(
        b"openapi: 3.1.0", generate_cmd="python -c \"import surely_not_installed_pkg\"", root=tmp_path
    )
    assert result.status is Status.SKIPPED
    assert "not checked here" in result.summary


def test_a_generator_that_genuinely_fails_is_still_an_error(tmp_path: Path) -> None:
    result = freshness.run(b"openapi: 3.1.0", generate_cmd="python -c \"raise SystemExit(3)\"", root=tmp_path)
    assert result.status is Status.ERROR


# --- project detection -----------------------------------------------------------


def _fastapi_project(root: Path) -> Path:
    (root / "requirements.txt").write_text("fastapi==0.115\nuvicorn\n", encoding="utf-8")
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "main.py").write_text("from fastapi import FastAPI\n\napp = FastAPI(title='x')\n",
                                          encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "openapi.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    (root / ".github").mkdir()
    return root


def test_detects_fastapi_and_writes_its_export_command(tmp_path: Path) -> None:
    found = project.detect(_fastapi_project(tmp_path))
    assert found.spec == (tmp_path / "docs" / "openapi.yaml").resolve()
    assert found.framework.name == "FastAPI"
    assert "from app.main import app" in found.framework.generate_cmd
    assert found.ci == ["github"]


def test_detects_django_spectacular(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("Django\ndrf-spectacular\n", encoding="utf-8")
    (tmp_path / "manage.py").write_text("", encoding="utf-8")
    assert project.detect_framework(tmp_path).generate_cmd == "python manage.py spectacular"


def test_spring_is_recognised_but_honest_about_needing_a_running_app(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<artifactId>springdoc-openapi-starter-webmvc-ui</artifactId>",
                                      encoding="utf-8")
    found = project.detect_framework(tmp_path)
    assert found.generate_cmd is None and "/v3/api-docs" in found.note


def test_spec_with_sorted_keys_is_found(tmp_path: Path) -> None:
    """Regression: generators that sort keys put `openapi:` after a long
    `components:` section; looking only at the top missed sample-api's spec."""
    spec = dict(SPEC, components={"schemas": {f"S{i}": {"type": "object"} for i in range(300)}})
    (tmp_path / "openapi.yaml").write_text(yaml.safe_dump(spec, sort_keys=True), encoding="utf-8")
    assert (tmp_path / "openapi.yaml").read_text(encoding="utf-8").startswith("components:")
    assert project.find_spec(tmp_path) == tmp_path / "openapi.yaml"


def test_a_yaml_file_that_is_not_openapi_is_not_taken_for_the_spec(tmp_path: Path) -> None:
    (tmp_path / "openapi.yaml").write_text("services:\n  web: {}\n", encoding="utf-8")
    assert project.find_spec(tmp_path) is None


# --- api-guard init ----------------------------------------------------------------


def test_init_writes_a_working_setup(tmp_path: Path, capsys) -> None:
    root = _fastapi_project(tmp_path)
    assert cli.main(["init", "--dir", str(root)]) == 0

    config = load(root / "api-guard.yaml")
    assert config.spec.path == Path("docs/openapi.yaml")
    assert config.spec.base == "git:origin/main"
    assert "from app.main import app" in config.spec.generate_cmd
    assert config.runtime is None, "conformance stays opt-in"

    assert load_waivers(root / "waivers.yaml") == [], "the waivers file starts empty and valid"

    workflow = yaml.safe_load((root / ".github/workflows/api-guard.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["contract"]["steps"]
    assert steps[-1]["uses"] == "stimpy3/ApiGuard@main"
    assert steps[-1]["with"]["generated-spec"] == "api-guard-generated.json"
    assert any("pip install -r requirements.txt" == s.get("run") for s in steps)

    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "api-guard-report/" in ignore and ".api-guard/" in ignore

    out = capsys.readouterr().out
    assert "freshness    on" in out and "conformance  off" in out


def test_init_never_overwrites_without_force(tmp_path: Path) -> None:
    root = _fastapi_project(tmp_path)
    (root / "api-guard.yaml").write_text("spec:\n  path: mine.yaml\n", encoding="utf-8")
    cli.main(["init", "--dir", str(root)])
    assert (root / "api-guard.yaml").read_text(encoding="utf-8") == "spec:\n  path: mine.yaml\n"

    cli.main(["init", "--dir", str(root), "--force"])
    assert "docs/openapi.yaml" in (root / "api-guard.yaml").read_text(encoding="utf-8")


def test_init_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _fastapi_project(tmp_path)
    cli.main(["init", "--dir", str(root), "--dry-run"])
    assert not (root / "api-guard.yaml").exists()
    assert not (root / ".gitignore").exists()


def test_init_gitignore_lines_are_added_once(tmp_path: Path) -> None:
    root = _fastapi_project(tmp_path)
    cli.main(["init", "--dir", str(root)])
    cli.main(["init", "--dir", str(root), "--force"])
    assert (root / ".gitignore").read_text(encoding="utf-8").count("api-guard-report/") == 1


def test_init_for_jenkins_prints_a_stage_instead_of_editing(tmp_path: Path, capsys) -> None:
    root = _fastapi_project(tmp_path)
    (root / "Jenkinsfile").write_text("pipeline {}\n", encoding="utf-8")
    cli.main(["init", "--dir", str(root), "--ci", "jenkins"])
    assert (root / "Jenkinsfile").read_text(encoding="utf-8") == "pipeline {}\n"
    out = capsys.readouterr().out
    assert "stage('API contract')" in out and "--generated-spec api-guard-generated.json" in out


def test_init_without_a_known_framework_turns_freshness_off_and_says_so(tmp_path: Path, capsys) -> None:
    (tmp_path / "openapi.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    cli.main(["init", "--dir", str(tmp_path)])
    assert load(tmp_path / "api-guard.yaml").spec.generate_cmd is None
    assert "freshness    off" in capsys.readouterr().out


# --- running with no config at all -------------------------------------------------


def test_check_runs_on_defaults_when_there_is_no_config(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "openapi.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_check(config, *args, **kwargs):
        seen["path"], seen["base"] = config.spec.path, config.spec.base
        return decide([], [], WaiverOutcome(), {})

    monkeypatch.setattr(cli, "_check", fake_check)
    assert cli.main(["check"]) == 0
    assert seen == {"path": Path("openapi.yaml"), "base": "git:origin/main"}


def test_no_config_and_no_spec_points_at_init(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["check"]) == 2
    assert "api-guard init" in capsys.readouterr().err


# --- one headline ------------------------------------------------------------------


def _result(*statuses: tuple[str, Status]):
    return decide([CheckResult(name=n, status=s, summary=f"{n} summary") for n, s in statuses],
                  [Change(id="x", fingerprint="abc123")], WaiverOutcome(), {})


def test_headline_says_what_matters_in_one_line() -> None:
    assert cli.headline(_result(("breaking", Status.PASSED), ("conformance", Status.SKIPPED))) == "API contract: OK"
    blocked = cli.headline(_result(("breaking", Status.FAILED), ("freshness", Status.FAILED)))
    assert blocked == ("API contract: BLOCKED - changes that would break clients; "
                       "the committed spec is out of date")
    assert "COULD NOT CHECK" in cli.headline(_result(("breaking", Status.ERROR)))

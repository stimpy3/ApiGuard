"""CLI options that change what gets compared."""

from __future__ import annotations

from pathlib import Path

from api_guard import cli
from api_guard.policy import WaiverOutcome
from api_guard.verdict import decide


def test_base_overrides_the_config(tmp_path: Path, monkeypatch) -> None:
    """On a build of main, origin/main is the commit itself; --base points the
    breaking check at the last deployed commit instead."""
    (tmp_path / "openapi.yaml").write_text("{}", encoding="utf-8")
    config = tmp_path / "api-guard.yaml"
    config.write_text('spec:\n  path: openapi.yaml\n  base: "git:origin/main"\n', encoding="utf-8")

    seen = {}

    def fake_check(cfg, *args, **kwargs):
        seen["base"] = cfg.spec.base
        return decide([], [], WaiverOutcome(), {})

    monkeypatch.setattr(cli, "_check", fake_check)
    assert cli.main(["check", "--config", str(config), "--base", "git:abc1234"]) == 0
    assert seen["base"] == "git:abc1234"

    assert cli.main(["check", "--config", str(config)]) == 0
    assert seen["base"] == "git:origin/main", "without --base the config wins"

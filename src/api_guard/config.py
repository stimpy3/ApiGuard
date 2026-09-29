"""Configuration model for `api-guard.yaml`.

The config is the whole reason this tool is reusable: a project tells api-guard
where its spec lives and how to regenerate it, and api-guard never needs to
know whether that project is FastAPI, Spring Boot or hand-written YAML.

Only `spec` is required. Omitting a section disables the check that depends on
it, so a team with a hand-maintained spec still gets breaking-change detection
without being forced to invent a `generate_cmd`.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigError(Exception):
    """Raised when `api-guard.yaml` is missing, malformed or self-contradictory.

    Surfaced as exit code 2 (tool error), never 1 (contract violation): a typo
    in the config is not the API breaking its consumers, and conflating the two
    sends developers hunting for a breaking change that does not exist.
    """


class Severity(StrEnum):
    """Severity of a single reported change.

    oasdiff encodes these as integers in its JSON output — verified against the
    real tool, see tests/fixtures/specs/README.md.
    """

    INFO = "INFO"
    WARN = "WARN"
    ERR = "ERR"

    @classmethod
    def from_oasdiff_level(cls, level: int) -> Severity:
        try:
            return _LEVELS[level]
        except KeyError:
            # Forward compatibility: an unrecognised level is treated as the
            # most severe rather than ignored. A gate should fail loudly on
            # something it does not understand, not wave it through.
            raise ValueError(f"unknown oasdiff severity level: {level}") from None

    @property
    def rank(self) -> int:
        return _RANKS[self]


_LEVELS: dict[int, Severity] = {1: Severity.INFO, 2: Severity.WARN, 3: Severity.ERR}
_RANKS: dict[Severity, int] = {Severity.INFO: 1, Severity.WARN: 2, Severity.ERR: 3}


class FailOn(StrEnum):
    """Threshold at which a change fails the build.

    Only ERR and WARN: oasdiff's own `--fail-on` rejects INFO, and a gate that
    fails on informational changes would fail on every release.
    """

    ERR = "ERR"
    WARN = "WARN"


class _Base(BaseModel):
    # Reject unknown keys: a silently-ignored typo like `fail_on: EROR` would
    # quietly weaken the gate, which is worse than refusing to start.
    model_config = ConfigDict(extra="forbid")


class SpecConfig(_Base):
    """Where the contract lives and how to rebuild it."""

    path: Path = Field(description="The committed spec, relative to the project root.")
    base: str = Field(
        default="git:origin/main",
        description=(
            "What to compare against: 'git:<ref>' reads the spec from that git "
            "ref, or give a file path or an https:// URL."
        ),
    )
    generate_cmd: str | None = Field(
        default=None,
        description=(
            "Shell command printing the spec to stdout. Enables the freshness "
            "check. Omit for hand-maintained specs."
        ),
    )


class RuntimeConfig(_Base):
    """How to exercise the running API. Omit the section to skip conformance."""

    url: str = Field(description="Base URL of the running API.")
    checks: list[str] = Field(
        default_factory=lambda: [
            "not_a_server_error",
            "response_schema_conformance",
            "status_code_conformance",
        ],
        description="Schemathesis checks to run.",
    )
    max_examples: int = Field(default=50, ge=1, description="Test cases per operation.")
    wait_for_schema: int = Field(
        default=30, ge=1, description="Seconds to wait for the API to come up."
    )
    deterministic: bool = Field(
        default=True,
        description=(
            "Generate the same test cases every run. On by default because this "
            "is a build gate: a failure that will not reproduce on a re-run is "
            "worse than no check, since people learn to click 'retry' until it "
            "goes green. Turn it off for exploratory fuzzing outside CI."
        ),
    )
    seed: int | None = Field(
        default=None,
        description="Fixed random seed. Another way to make a run reproducible.",
    )


class PolicyConfig(_Base):
    """How strict to be, and which breaking changes have been acknowledged."""

    fail_on: FailOn = Field(
        default=FailOn.ERR, description="Lowest severity that fails the build: ERR or WARN."
    )
    deprecation_days_stable: int = Field(
        default=180,
        ge=0,
        description="Minimum sunset notice for stable endpoints, in days.",
    )
    deprecation_days_beta: int = Field(
        default=30, ge=0, description="Minimum sunset notice for beta endpoints, in days."
    )
    severity_levels: Path | None = Field(
        default=None,
        description="Optional oasdiff severity-levels file for org-wide rule tuning.",
    )
    waivers: Path | None = Field(
        default=None,
        description="Optional waivers.yaml listing acknowledged breaking changes.",
    )
    max_waiver_days: int = Field(
        default=90,
        ge=1,
        description=(
            "Furthest ahead a waiver's expiry may be, counted from today. Keeps "
            "waivers short-lived instead of a permanent ignore list."
        ),
    )


class ReportConfig(_Base):
    """Where to write results, and in which formats."""

    dir: Path = Field(default=Path("api-guard-report"))
    formats: list[str] = Field(default_factory=lambda: ["markdown", "json", "junit"])

    _ALLOWED = {"markdown", "json", "junit", "html"}

    @model_validator(mode="after")
    def _check_formats(self) -> ReportConfig:
        unknown = set(self.formats) - self._ALLOWED
        if unknown:
            raise ValueError(
                f"unknown report format(s): {', '.join(sorted(unknown))}. "
                f"Supported: {', '.join(sorted(self._ALLOWED))}"
            )
        # json is what the MCP server, PR comments and any dashboard read, so a
        # run that omits it produces results nothing downstream can consume.
        if "json" not in self.formats:
            raise ValueError("report.formats must include 'json' — downstream tools read it")
        return self


class Config(_Base):
    """A parsed `api-guard.yaml`, plus the root it was loaded from."""

    spec: SpecConfig
    runtime: RuntimeConfig | None = None
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)

    # Populated by load(); not read from the YAML file itself.
    root: Path = Field(default=Path("."), exclude=True)

    @property
    def checks_freshness(self) -> bool:
        return self.spec.generate_cmd is not None

    @property
    def checks_conformance(self) -> bool:
        return self.runtime is not None

    def resolve(self, path: Path) -> Path:
        """Make a config-relative path absolute against the project root."""
        return path if path.is_absolute() else (self.root / path)


def load(config_path: Path) -> Config:
    """Read and validate `api-guard.yaml`.

    Paths inside the config are interpreted relative to the file's own
    directory, so running api-guard from any working directory behaves the same.
    """
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping, got {type(data).__name__}")

    try:
        config = Config(**data)
    except Exception as exc:
        raise ConfigError(f"invalid config {config_path}:\n{exc}") from exc

    config.root = config_path.resolve().parent
    return config

"""Tests for the waiver engine.

The waiver path is the one place where a breaking change is allowed through, so
it gets the most scrutiny. A bug that waives too much turns the gate off
silently, which is worse than having no gate at all — people would still trust
the green tick.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from api_guard.policy import PolicyError, Waiver, apply_waivers, load_waivers

TODAY = date(2026, 9, 21)
FUTURE = (TODAY + timedelta(days=90)).isoformat()
PAST = (TODAY - timedelta(days=1)).isoformat()

VALID = f"""
- fingerprint: "631dbccdc316"
  id: response-required-property-removed
  path: /users
  reason: "PROD-142 - email superseded by phone, both consumers migrated."
  approved_by: sohan
  expires: {FUTURE}
"""


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "waivers.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_a_valid_waiver(tmp_path: Path) -> None:
    waivers = load_waivers(write(tmp_path, VALID), today=TODAY)
    assert len(waivers) == 1
    assert waivers[0].fingerprint == "631dbccdc316"


def test_empty_file_is_fine(tmp_path: Path) -> None:
    """The common case: a repo with nothing currently waived."""
    assert load_waivers(write(tmp_path, ""), today=TODAY) == []


def test_expired_waiver_loads_but_is_never_applied(tmp_path: Path) -> None:
    """Expired is a warning, not a failure: the waiver is set aside, so the
    change it used to cover counts exactly as if it did not exist."""
    expired = load_waivers(write(tmp_path, VALID.replace(FUTURE, PAST)), today=TODAY)
    assert len(expired) == 1

    remaining, outcome = apply_waivers([change("631dbccdc316")], expired, today=TODAY)
    assert len(remaining) == 1, "an expired waiver must not let the change through"
    assert outcome.applied == []
    assert [w.fingerprint for w in outcome.expired] == ["631dbccdc316"]
    assert outcome.stale == [], "expired is reported as expired, not as stale"


def test_waiver_beyond_max_days_is_rejected(tmp_path: Path) -> None:
    """A far-off expiry would turn a waiver back into a permanent ignore list."""
    far = VALID.replace(FUTURE, (TODAY + timedelta(days=91)).isoformat())
    with pytest.raises(PolicyError, match="more than 90 days"):
        load_waivers(write(tmp_path, far), today=TODAY)


def test_max_days_is_configurable(tmp_path: Path) -> None:
    far = VALID.replace(FUTURE, (TODAY + timedelta(days=150)).isoformat())
    assert len(load_waivers(write(tmp_path, far), today=TODAY, max_days=180)) == 1


def test_waiver_expiring_today_still_works(tmp_path: Path) -> None:
    """Off-by-one guard: `expires` is the last day the waiver is valid."""
    today_waiver = VALID.replace(FUTURE, TODAY.isoformat())
    assert len(load_waivers(write(tmp_path, today_waiver), today=TODAY)) == 1


def test_missing_reason_is_rejected(tmp_path: Path) -> None:
    no_reason = "\n".join(
        line for line in VALID.splitlines() if not line.strip().startswith("reason:")
    )
    with pytest.raises(PolicyError, match="reason"):
        load_waivers(write(tmp_path, no_reason), today=TODAY)


def test_thin_reason_is_rejected(tmp_path: Path) -> None:
    """'temp' is not an acknowledgement; the audit trail has to mean something."""
    thin = VALID.replace(
        '"PROD-142 - email superseded by phone, both consumers migrated."', '"temp"'
    )
    with pytest.raises(PolicyError, match="reason"):
        load_waivers(write(tmp_path, thin), today=TODAY)


def test_missing_approver_is_rejected(tmp_path: Path) -> None:
    no_approver = "\n".join(
        line for line in VALID.splitlines() if not line.strip().startswith("approved_by:")
    )
    with pytest.raises(PolicyError, match="approved_by"):
        load_waivers(write(tmp_path, no_approver), today=TODAY)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    """A typo'd key must not be silently ignored — it would weaken the gate."""
    typo = VALID + '  expiress: "2027-01-01"\n'
    with pytest.raises(PolicyError):
        load_waivers(write(tmp_path, typo), today=TODAY)


def test_duplicate_fingerprints_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="duplicate"):
        load_waivers(write(tmp_path, VALID + VALID), today=TODAY)


def test_all_problems_reported_at_once(tmp_path: Path) -> None:
    """Fixing waivers one error per build run is miserable; report them together."""
    broken = f"""
- fingerprint: "aaa1"
  approved_by: sohan
  expires: {FUTURE}
- fingerprint: "bbb2"
  reason: "a perfectly adequate explanation here"
  expires: {FUTURE}
"""
    with pytest.raises(PolicyError) as exc:
        load_waivers(write(tmp_path, broken), today=TODAY)
    message = str(exc.value)
    assert "aaa1" in message and "bbb2" in message


# --- matching -------------------------------------------------------------


def waiver(fingerprint: str) -> Waiver:
    return Waiver(
        fingerprint=fingerprint,
        reason="an adequate explanation of the decision",
        approved_by="sohan",
        expires=date(2027, 1, 1),
    )


def change(fingerprint: str, level: int = 3) -> dict:
    return {"fingerprint": fingerprint, "level": level, "id": "x", "path": "/users"}


def test_matching_change_is_filtered_out() -> None:
    remaining, outcome = apply_waivers([change("aaa1")], [waiver("aaa1")])
    assert remaining == []
    assert [w.fingerprint for w in outcome.applied] == ["aaa1"]


def test_unwaived_change_survives() -> None:
    remaining, outcome = apply_waivers([change("aaa1")], [waiver("zzz9")])
    assert len(remaining) == 1
    assert outcome.applied == []


def test_waiver_is_specific_to_one_change() -> None:
    """The failure that matters: a waiver must not swallow unrelated breakages."""
    remaining, _ = apply_waivers([change("aaa1"), change("bbb2")], [waiver("aaa1")])
    assert [c["fingerprint"] for c in remaining] == ["bbb2"]


def test_unmatched_waiver_is_reported_as_stale() -> None:
    """Reported, not fatal: usually it means somebody fixed the problem properly."""
    _, outcome = apply_waivers([], [waiver("aaa1")])
    assert [w.fingerprint for w in outcome.stale] == ["aaa1"]


def test_change_without_fingerprint_is_never_waived() -> None:
    """Fail closed: an entry we cannot identify must not be silently dropped."""
    remaining, _ = apply_waivers([{"level": 3, "id": "x"}], [waiver("aaa1")])
    assert len(remaining) == 1

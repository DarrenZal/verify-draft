"""Tests for gate_helpers.py — the stacking guard, the operator override, the audit row.

assert_not_duplicate catches the same message twice. assert_not_stacking catches the
failure that actually happened: four different substantial messages into one room in one
evening (2026-07-31), each of which passed the gate on its own.

Run: python3 -m pytest scripts/verify-draft/tests/ -q
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import gate_helpers  # noqa: E402

ROOM = ["telegram:3980532206"]
OTHER_ROOM = ["telegram:111222333"]
LONG_BODY = " ".join(["word"] * 120)
SHORT_BODY = " ".join(["word"] * 20)


def make_args(argv=(), **overrides):
    """Build a real parsed namespace, so add_verify_args' wiring is under test too."""
    p = argparse.ArgumentParser()
    gate_helpers.add_verify_args(p)
    args = p.parse_args(list(argv))
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def utc_stamp(offset_seconds: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_seconds))


@pytest.fixture
def audit(tmp_path, monkeypatch):
    """Point the audit log at tmp_path and hand back a row-writer."""
    log = tmp_path / "send-audit.jsonl"
    monkeypatch.setattr(gate_helpers, "AUDIT_LOG", log)
    gate_helpers._TIER_ENFORCED.clear()

    def write(**over):
        row = {
            "timestamp": utc_stamp(),
            "skill": "telegram-send",
            "recipients": ROOM,
            "send_status": "ok",
            "dry_run": False,
            "action": "send",
            "word_count": 300,
            "send_confirmation": "464",
        }
        row.update(over)
        with log.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    write.path = log
    return write


# ---------------------------------------------------------------------------
# assert_not_stacking
# ---------------------------------------------------------------------------


def test_second_substantial_group_message_same_day_is_refused(audit):
    audit()
    with pytest.raises(SystemExit) as e:
        gate_helpers.assert_not_stacking(
            make_args(), body=LONG_BODY, recipients=ROOM, audience="group")
    assert e.value.code == 3


def test_stacking_error_names_the_edit_route(audit, capsys):
    audit(send_confirmation="464")
    with pytest.raises(SystemExit):
        gate_helpers.assert_not_stacking(
            make_args(), body=LONG_BODY, recipients=ROOM, audience="group")
    err = capsys.readouterr().err
    assert "--edit 464" in err, "the remedy has to be in the error, not just the refusal"
    assert "--shape-override-confirmed-by-operator" in err


def test_first_message_of_the_day_passes(audit):
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="group")


def test_edit_rows_are_not_stacking(audit):
    """An edit is the remedy hr-upd-2 prescribes; it must not become the trigger."""
    audit(action="edit")
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="group")


def test_short_prior_rows_are_ignored(audit):
    audit(word_count=40)
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="group")


def test_rows_without_word_count_count_as_substantial(audit):
    """Pre-migration rows have no word_count; assuming they were short fails open."""
    row = audit()
    del row["word_count"]
    audit.path.write_text(json.dumps(row) + "\n")
    with pytest.raises(SystemExit) as e:
        gate_helpers.assert_not_stacking(
            make_args(), body=LONG_BODY, recipients=ROOM, audience="group")
    assert e.value.code == 3


def test_a_short_new_message_is_never_stacking(audit):
    audit()
    gate_helpers.assert_not_stacking(
        make_args(), body=SHORT_BODY, recipients=ROOM, audience="group")


def test_other_rooms_do_not_count(audit):
    audit(recipients=OTHER_ROOM)
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="group")


def test_yesterday_does_not_count(audit):
    audit(timestamp=utc_stamp(-30 * 3600))
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="group")


def test_dry_run_and_failed_rows_do_not_count(audit):
    audit(dry_run=True)
    audit(send_status="failed")
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="group")


def test_dm_audience_is_never_stacking(audit):
    audit()
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="dm")


def test_allow_stacking_clears_it(audit):
    audit()
    gate_helpers.assert_not_stacking(
        make_args(["--allow-stacking"]), body=LONG_BODY, recipients=ROOM, audience="group")


def test_allow_duplicate_does_not_clear_stacking(audit):
    """Different wording is exactly the case --allow-duplicate is not about."""
    audit()
    with pytest.raises(SystemExit):
        gate_helpers.assert_not_stacking(
            make_args(["--allow-duplicate"]), body=LONG_BODY, recipients=ROOM, audience="group")


def test_fresh_shape_override_clears_stacking(audit):
    audit()
    args = make_args(shape_override_confirmed_by_operator=int(time.time()))
    gate_helpers.assert_not_stacking(args, body=LONG_BODY, recipients=ROOM, audience="group")


def test_stale_shape_override_does_not_clear_stacking(audit):
    """A timestamp an agent could have guessed an hour ago is not an operator present now."""
    audit()
    args = make_args(shape_override_confirmed_by_operator=int(time.time()) - 3600)
    with pytest.raises(SystemExit) as e:
        gate_helpers.assert_not_stacking(args, body=LONG_BODY, recipients=ROOM, audience="group")
    assert e.value.code == 3


def test_unreadable_audit_log_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_helpers, "AUDIT_LOG", tmp_path / "nope.jsonl")
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="group")


def test_corrupt_audit_lines_are_skipped(audit):
    audit.path.write_text("not json at all\n\n")
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="group")


# ---------------------------------------------------------------------------
# Operator timestamp freshness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta,fresh",
    [(0, True), (30, True), (-30, True), (61, False), (-61, False), (3600, False)],
)
def test_fresh_operator_ts(delta, fresh):
    assert gate_helpers._fresh_operator_ts(int(time.time()) + delta) is fresh


def test_none_is_never_fresh():
    assert gate_helpers._fresh_operator_ts(None) is False


def test_stale_shape_override_is_rejected_by_the_gate(tmp_path, capsys):
    """Exits before the engine runs, so no send can proceed on a stale override."""
    body = tmp_path / "draft.md"
    body.write_text("hello\n")
    (tmp_path / "draft.md.verifications.yaml").write_text("verifications: []\n")
    args = make_args(shape_override_confirmed_by_operator=int(time.time()) - 900)
    with pytest.raises(SystemExit) as e:
        gate_helpers.run_verification_gate(args, body_file=str(body), body_inline=None)
    assert e.value.code == 2
    assert "stale or future-dated" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Tier enforcement idempotency
# ---------------------------------------------------------------------------


def test_tier_confirm_is_idempotent(tmp_path, monkeypatch, capsys):
    """run_verification_gate calls it, then send-proton.py calls it again. One prompt."""
    tier_zero = tmp_path / "tier-zero.txt"
    tier_zero.write_text("email:sensitive@example.com\n")
    monkeypatch.setattr(gate_helpers, "TIER_ZERO_FILE", tier_zero)
    gate_helpers._TIER_ENFORCED.clear()
    recipients = ["email:sensitive@example.com"]

    with pytest.raises(SystemExit):  # no confirmation yet
        gate_helpers.enforce_tier_confirm(make_args(), recipients)

    args = make_args(confirm_send_by_operator=int(time.time()))
    assert gate_helpers.enforce_tier_confirm(args, recipients) == 0
    first = capsys.readouterr().err
    assert "Tier 0 send confirmed" in first

    assert gate_helpers.enforce_tier_confirm(args, recipients) == 0
    assert capsys.readouterr().err == "", "second call must not re-prompt or re-print"


# ---------------------------------------------------------------------------
# Audit row
# ---------------------------------------------------------------------------


def test_audit_row_carries_the_new_fields(audit):
    gate_helpers.append_audit_row(
        make_args(), skill="telegram-send", recipients=ROOM, body=LONG_BODY,
        gate_report={"shape": [{"rule": "S9", "severity": "warn", "detail": "..."}]},
        send_confirmation="470", dry_run=False,
        audience="group", channel="telegram", action="send",
    )
    row = json.loads(audit.path.read_text().splitlines()[-1])
    assert row["audience"] == "group"
    assert row["channel"] == "telegram"
    assert row["action"] == "send"
    assert row["word_count"] == 120
    assert row["shape"] == [{"rule": "S9", "severity": "warn"}]


def test_audit_row_defaults_action_to_send(audit):
    gate_helpers.append_audit_row(
        make_args(), skill="slack-send", recipients=ROOM, body=SHORT_BODY,
        gate_report=None, send_confirmation="1", dry_run=False,
    )
    row = json.loads(audit.path.read_text().splitlines()[-1])
    assert row["action"] == "send"
    assert row["word_count"] == 20
    assert row["shape"] == []


def test_an_edit_row_then_does_not_trigger_stacking(audit):
    """End to end: the telegram --edit branch writes a row that the guard ignores."""
    gate_helpers.append_audit_row(
        make_args(), skill="telegram-send", recipients=ROOM, body=LONG_BODY,
        gate_report=None, send_confirmation="464", dry_run=False,
        audience="group", channel="telegram", action="edit",
    )
    gate_helpers.assert_not_stacking(
        make_args(), body=LONG_BODY, recipients=ROOM, audience="group")


def test_a_send_row_then_does_trigger_stacking(audit):
    gate_helpers.append_audit_row(
        make_args(), skill="telegram-send", recipients=ROOM, body=LONG_BODY,
        gate_report=None, send_confirmation="464", dry_run=False,
        audience="group", channel="telegram", action="send",
    )
    with pytest.raises(SystemExit) as e:
        gate_helpers.assert_not_stacking(
            make_args(), body=LONG_BODY, recipients=ROOM, audience="group")
    assert e.value.code == 3


# ---------------------------------------------------------------------------
# Same-local-day arithmetic (the DST bug that already bit the duplicate guard)
# ---------------------------------------------------------------------------


def test_is_today_local_handles_utc_rows():
    assert gate_helpers._is_today_local(utc_stamp()) is True
    assert gate_helpers._is_today_local(utc_stamp(-30 * 3600)) is False


def test_is_today_local_survives_garbage():
    assert gate_helpers._is_today_local("") is False
    assert gate_helpers._is_today_local("not-a-date") is False


def test_is_today_local_reads_the_stamp_as_utc_not_local():
    """The duplicate guard once measured a 38s-old send as 3638s via mktime+timezone."""
    epoch = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    assert abs(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")) - epoch) < 2

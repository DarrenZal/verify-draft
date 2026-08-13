"""Tests for verify_draft.py — the claim pass (regression lock) + the shape checks.

The engine shipped with zero tests, which is how `return blocks[:100]`-class defects
survive. Two jobs here:

  1. BACK-COMPAT LOCK. Every existing fixture, run with no --audience, must exit exactly
     what it exited before Phase 2. The shape class is additive or it is a regression.

  2. SHAPE CHECKS. The 2026-07-31 memo shape must be mechanically unsendable to a group.

Run: python3 -m pytest scripts/verify-draft/tests/ -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import verify_draft  # noqa: E402

FIXTURES = HERE / "fixtures"
RESOLVE_PROFILE = HERE.parent.parent / "comms-profiles" / "resolve_profile.py"


def run(capsys, *argv) -> tuple[int, dict]:
    """Call main() in-process and return (exit_code, parsed JSON report)."""
    code = verify_draft.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def rules(report: dict, severity: str) -> set:
    return {f["rule"] for f in report.get("shape", []) if f["severity"] == severity}


def write_draft(tmp_path: Path, body: str, sidecar: str = "verifications: []\n") -> Path:
    draft = tmp_path / "draft.md"
    draft.write_text(body)
    (tmp_path / "draft.md.verifications.yaml").write_text(sidecar)
    return draft


# ---------------------------------------------------------------------------
# 1. Back-compat: no --audience → pre-Phase-2 behavior, exactly
# ---------------------------------------------------------------------------

# Expected exit code per fixture. Each fixture isolates one rule; see README.
BASELINE_EXITS = {
    "correct_draft.md": 0,
    "internal_tooling_no_access.md": 2,
    "mismatched_source_draft.md": 0,
    "no_claims_draft.md": 0,
    "placeholder_unfinished.md": 2,
    "unsourced_parenthetical.md": 2,
}


@pytest.mark.parametrize("name,expected", sorted(BASELINE_EXITS.items()))
def test_backcompat_no_audience_flag(capsys, name, expected):
    """Every fixture keeps its exit code when --audience is not passed."""
    code, report = run(capsys, "--draft", str(FIXTURES / name))
    assert code == expected, f"{name} changed exit code {expected} -> {code}"
    assert report["shape"] == [], "shape checks must not run without --audience"


def test_backcompat_covers_every_fixture_on_disk():
    """A new fixture added without a baseline entry fails here rather than silently."""
    on_disk = {p.name for p in FIXTURES.glob("*.md") if not p.name.endswith(".verifications.yaml")}
    shape_fixtures = {"shape_memo_group.md", "shape_good_group.md"}
    assert on_disk - shape_fixtures == set(BASELINE_EXITS)


def test_memo_without_audience_flag_passes_shape_free(capsys):
    """The memo fixture is only a failure once you say who it is going to."""
    code, report = run(capsys, "--draft", str(FIXTURES / "shape_memo_group.md"))
    assert code == 0
    assert report["shape"] == []


# ---------------------------------------------------------------------------
# 2. The incident shape
# ---------------------------------------------------------------------------


def test_memo_to_group_blocks_on_s1_s3_s5_s8(capsys):
    """The 2026-07-31 shape: long, no ask, em-dashed, path-leaking. Mechanically refused."""
    code, report = run(
        capsys, "--draft", str(FIXTURES / "shape_memo_group.md"),
        "--audience", "group", "--channel", "telegram",
    )
    assert code == 2
    assert {"S1", "S3", "S5", "S8"} <= rules(report, "block")
    assert report["passed"] is False


def test_good_group_message_passes(capsys):
    """Short, ask in the first line, no em-dash, no paths, and its one claim is sourced."""
    code, report = run(
        capsys, "--draft", str(FIXTURES / "shape_good_group.md"),
        "--audience", "group", "--channel", "telegram",
    )
    assert code == 0, report
    assert report["shape"] == []
    assert report["unverified"] == []


def test_shape_blocks_short_circuit_before_claim_detection(capsys):
    """A shape block is reported alone: fix the form before arguing about sources."""
    _, report = run(
        capsys, "--draft", str(FIXTURES / "shape_memo_group.md"),
        "--audience", "group", "--channel", "telegram",
    )
    assert report["claims"] == []


def test_shape_off_skips_the_checks(capsys):
    """--shape off is what the operator override threads through; it must actually skip."""
    code, report = run(
        capsys, "--draft", str(FIXTURES / "shape_memo_group.md"),
        "--audience", "group", "--channel", "telegram", "--shape", "off",
    )
    assert code == 0
    assert report["shape"] == []


# ---------------------------------------------------------------------------
# S1 / S2 / S3 — the ask, and where it sits
# ---------------------------------------------------------------------------


def test_s1_blocks_when_sidecar_declares_no_ask(capsys, tmp_path):
    draft = write_draft(tmp_path, "Short note to the room about nothing in particular.\n")
    code, report = run(capsys, "--draft", str(draft), "--audience", "group")
    assert code == 2
    assert rules(report, "block") == {"S1"}


def test_s1_satisfied_by_ask_none(capsys, tmp_path):
    draft = write_draft(
        tmp_path,
        "Short note to the room, no action needed from anyone.\n",
        "ask: none\nverifications: []\n",
    )
    code, report = run(capsys, "--draft", str(draft), "--audience", "group")
    assert code == 0, report


def test_s1_blocks_on_empty_ask_key(capsys, tmp_path):
    """`ask:` with nothing after it is not the conscious assertion `ask: none` is."""
    draft = write_draft(tmp_path, "Short note.\n", "ask:\nverifications: []\n")
    code, report = run(capsys, "--draft", str(draft), "--audience", "group")
    assert code == 2
    assert rules(report, "block") == {"S1"}


def test_s2_blocks_when_the_ask_is_buried(capsys, tmp_path):
    ask = "Can you re-ask the lawyers before anyone contacts the campaign?"
    body = (
        "Here is where things stand after the last two weeks of work on the build.\n\n"
        "The first thing worth saying is that the two halves now talk to each other.\n\n"
        "The second thing is that the numbers moved in the direction we hoped for.\n\n"
        f"{ask}\n"
    )
    draft = write_draft(tmp_path, body, f'ask:\n  text: "{ask}"\nverifications: []\n')
    code, report = run(capsys, "--draft", str(draft), "--audience", "group")
    assert code == 2
    assert "S2" in rules(report, "block")


def test_s2_passes_when_the_ask_leads(capsys, tmp_path):
    ask = "Can you re-ask the lawyers before anyone contacts the campaign?"
    body = f"{ask}\n\nIt is the only item here with a real deadline attached to it.\n"
    draft = write_draft(tmp_path, body, f'ask:\n  text: "{ask}"\nverifications: []\n')
    code, report = run(capsys, "--draft", str(draft), "--audience", "group")
    assert code == 0, report


def test_s3_cap_is_tighter_with_ask_none(capsys, tmp_path):
    """150 words: fine as a real ask, too long as an FYI (200 vs 120)."""
    ask = "Can we decide the hosting question on the call?"
    body = ask + "\n\n" + " ".join(["filler"] * 140) + "\n"
    assert 121 <= len(body.split()) <= 200

    draft = write_draft(tmp_path, body, f'ask:\n  text: "{ask}"\nverifications: []\n')
    code, _ = run(capsys, "--draft", str(draft), "--audience", "group")
    assert code == 0

    write_draft(tmp_path, body, "ask: none\nverifications: []\n")
    code, report = run(capsys, "--draft", str(draft), "--audience", "group")
    assert code == 2
    assert rules(report, "block") == {"S3"}
    assert "cap is 120" in [f["detail"] for f in report["shape"] if f["rule"] == "S3"][0]


# ---------------------------------------------------------------------------
# S4 / S5 / S8 — severity is a function of audience
# ---------------------------------------------------------------------------


def test_dm_downgrades_length_and_paths_to_warnings(capsys):
    """The same memo to one person: S4 + S8 warn, and neither blocks.

    Channel is email here because S5 (em-dash) blocks for DMs too and this fixture has
    one — see test_dm_still_blocks_on_em_dash. Email is the one shape-exempt genre for
    that rule, which makes it the clean way to isolate the severity downgrade.
    """
    code, report = run(
        capsys, "--draft", str(FIXTURES / "shape_memo_group.md"),
        "--audience", "dm", "--channel", "email",
    )
    assert code == 0, report
    assert rules(report, "block") == set()
    assert {"S4", "S8"} <= rules(report, "warn")
    assert any("shape S8 (warn)" in w for w in report["warnings"])


def test_dm_still_blocks_on_em_dash(capsys):
    """S5 is audience-wide: it has to read like a person wrote it in a DM too."""
    code, report = run(
        capsys, "--draft", str(FIXTURES / "shape_memo_group.md"),
        "--audience", "dm", "--channel", "telegram",
    )
    assert code == 2
    assert rules(report, "block") == {"S5"}


def test_s5_exempts_email(capsys, tmp_path):
    draft = write_draft(tmp_path, "A note — with an em-dash in it.\n", "ask: none\nverifications: []\n")
    assert run(capsys, "--draft", str(draft), "--audience", "dm", "--channel", "email")[0] == 0
    assert run(capsys, "--draft", str(draft), "--audience", "dm", "--channel", "telegram")[0] == 2


def test_s8_ignores_paths_inside_urls(capsys, tmp_path):
    """A link is the fix, not the failure — the same path inside a URL must not block."""
    ask = "Can you read the write-up before the call?"
    body = (
        f"{ask}\n\nIt is at "
        "https://github.com/example/repo/blob/main/docs/design/note.md and it is short.\n"
    )
    draft = write_draft(tmp_path, body, f'ask:\n  text: "{ask}"\nverifications: []\n')
    code, report = run(capsys, "--draft", str(draft), "--audience", "group")
    assert code == 0, report

    body_bare = f"{ask}\n\nIt is at docs/design/note.md and it is short.\n"
    draft2 = write_draft(tmp_path, body_bare, f'ask:\n  text: "{ask}"\nverifications: []\n')
    code, report = run(capsys, "--draft", str(draft2), "--audience", "group")
    assert code == 2
    assert "S8" in rules(report, "block")


# ---------------------------------------------------------------------------
# S6 / S7 — channel mechanics
# ---------------------------------------------------------------------------


def test_s6_single_asterisk_italics_block_on_telegram(capsys, tmp_path):
    ask = "Can you confirm the time?"
    draft = write_draft(
        tmp_path, f"{ask} It is *important* that we lock it.\n",
        f'ask:\n  text: "{ask}"\nverifications: []\n',
    )
    assert "S6" in rules(run(capsys, "--draft", str(draft), "--audience", "group",
                             "--channel", "telegram")[1], "block")
    assert "S6" in rules(run(capsys, "--draft", str(draft), "--audience", "group",
                             "--channel", "signal")[1], "block")
    # Slack renders single asterisks as bold; the rule is Telethon/Signal-specific.
    assert run(capsys, "--draft", str(draft), "--audience", "group", "--channel", "slack")[0] == 0


def test_s6_leaves_double_asterisk_bold_alone(capsys, tmp_path):
    ask = "Can you confirm the time?"
    draft = write_draft(
        tmp_path, f"{ask} It is **important** that we lock it.\n",
        f'ask:\n  text: "{ask}"\nverifications: []\n',
    )
    assert run(capsys, "--draft", str(draft), "--audience", "group", "--channel", "telegram")[0] == 0


def test_s7_blocks_past_the_telegram_hard_cap(capsys, tmp_path):
    ask = "Can you skim this?"
    body = ask + "\n\n" + ("abcdefghij " * 500)  # ~5500 chars
    assert len(body) > 4096
    draft = write_draft(tmp_path, body, f'ask:\n  text: "{ask}"\nverifications: []\n')
    _, report = run(capsys, "--draft", str(draft), "--audience", "group", "--channel", "telegram")
    assert "S7" in rules(report, "block")
    # Same body on Slack: over-long, but not atomically rejected, so not this rule.
    _, report = run(capsys, "--draft", str(draft), "--audience", "group", "--channel", "slack")
    assert "S7" not in rules(report, "block")


# ---------------------------------------------------------------------------
# S9 + config
# ---------------------------------------------------------------------------


def test_s9_jargon_warns_but_never_blocks(capsys, tmp_path):
    ask = "Can you look at the numbers?"
    body = f"{ask}\n\nThe silhouette is 0.53 and PR #15 is still a draft.\n"
    draft = write_draft(tmp_path, body, f'ask:\n  text: "{ask}"\nverifications: []\n')
    code, report = run(capsys, "--draft", str(draft), "--audience", "group")
    assert code == 0
    assert "S9" in rules(report, "warn")


def test_shape_budgets_come_from_config(tmp_path, monkeypatch):
    cfg = tmp_path / "verify-config.toml"
    cfg.write_text('[shape]\ngroup_max_words = 12\njargon = ["frobnicate"]\n')
    monkeypatch.setenv("VERIFY_CONFIG_PATH", str(cfg))
    conf = verify_draft.shape_config()
    assert conf["group_max_words"] == 12
    assert conf["jargon"] == ["frobnicate"]
    assert conf["group_max_words_fyi"] == 120, "unset keys keep the code default"


def test_config_default_when_env_points_nowhere(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIFY_CONFIG_PATH", str(tmp_path / "absent.toml"))
    assert verify_draft.shape_config()["group_max_words"] == 200


# ---------------------------------------------------------------------------
# S10 — the group profile hash
# ---------------------------------------------------------------------------

CHAT_ID = "telegram:999000111"


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Groups").mkdir(parents=True)
    (vault / "People").mkdir()
    (vault / "Groups" / "Test Room.md").write_text(
        '---\n"@type": Group\nname: Test Room\nchat_ids:\n  - '
        f"{CHAT_ID}\nmembers:\n  - \"[[People/Someone]]\"\nnorms: []\n---\n"
    )
    monkeypatch.setenv("COMMS_CHANNELS_VAULT", str(vault))
    return vault


def current_hash(chat_id: str, channel: str | None = None) -> str:
    cmd = [sys.executable, str(RESOLVE_PROFILE), "--chat-id", chat_id, "--json"]
    if channel:
        cmd += ["--channel", channel]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)["profile_hash"]


def _group_draft(tmp_path: Path, audience_block: str) -> Path:
    ask = "Can you pick a time?"
    return write_draft(
        tmp_path, f"{ask}\n\nAny afternoon works on my end.\n",
        f'ask:\n  text: "{ask}"\n{audience_block}verifications: []\n',
    )


@pytest.mark.skipif(not RESOLVE_PROFILE.exists(), reason="comms-profiles layer not installed")
def test_s10_blocks_a_stale_profile_hash(capsys, tmp_path, fake_vault):
    draft = _group_draft(
        tmp_path,
        'audience:\n  resolved: ["Someone"]\n  profile_hash: deadbeefdeadbeef\n  type: ask\n',
    )
    code, report = run(
        capsys, "--draft", str(draft), "--audience", "group",
        "--channel", "telegram", "--recipients", CHAT_ID,
    )
    assert code == 2
    assert "S10" in rules(report, "block")
    detail = [f["detail"] for f in report["shape"] if f["rule"] == "S10"][0]
    assert "resolve_profile.py" in detail and CHAT_ID in detail, "must name the command to run"


@pytest.mark.skipif(not RESOLVE_PROFILE.exists(), reason="comms-profiles layer not installed")
def test_s10_blocks_when_no_hash_is_carried(capsys, tmp_path, fake_vault):
    draft = _group_draft(tmp_path, "")
    code, report = run(
        capsys, "--draft", str(draft), "--audience", "group",
        "--channel", "telegram", "--recipients", CHAT_ID,
    )
    assert code == 2
    assert "S10" in rules(report, "block")


@pytest.mark.skipif(not RESOLVE_PROFILE.exists(), reason="comms-profiles layer not installed")
def test_s10_passes_on_the_current_hash(capsys, tmp_path, fake_vault):
    want = current_hash(CHAT_ID, "telegram")
    draft = _group_draft(
        tmp_path,
        f'audience:\n  resolved: ["Someone"]\n  profile_hash: {want}\n  type: unspecified\n',
    )
    code, report = run(
        capsys, "--draft", str(draft), "--audience", "group",
        "--channel", "telegram", "--recipients", CHAT_ID,
    )
    assert code == 0, report


@pytest.mark.skipif(not RESOLVE_PROFILE.exists(), reason="comms-profiles layer not installed")
def test_s10_silently_skips_when_no_groups_note_matches(capsys, tmp_path, fake_vault):
    """No Groups note carries this id, so there is no room profile to be stale against."""
    draft = _group_draft(tmp_path, "")
    code, report = run(
        capsys, "--draft", str(draft), "--audience", "group",
        "--channel", "telegram", "--recipients", "telegram:404404404",
    )
    assert code == 0, report
    assert "S10" not in rules(report, "block")


@pytest.mark.skipif(not RESOLVE_PROFILE.exists(), reason="comms-profiles layer not installed")
def test_s10_does_not_run_without_recipients(capsys, tmp_path, fake_vault):
    draft = _group_draft(tmp_path, "")
    code, _ = run(capsys, "--draft", str(draft), "--audience", "group", "--channel", "telegram")
    assert code == 0


# ---------------------------------------------------------------------------
# Sidecar parsing
# ---------------------------------------------------------------------------


def test_sidecar_extras_are_additive(tmp_path):
    """ask:/audience: parse out, and a sidecar without them is unchanged."""
    old = tmp_path / "old.yaml"
    old.write_text("verifications: []\n")
    vers, warns, extras = verify_draft.load_sidecar(old)
    assert vers == [] and warns == [] and extras == {}

    new = tmp_path / "new.yaml"
    new.write_text('ask:\n  text: "do the thing"\naudience:\n  profile_hash: abc\nverifications: []\n')
    _, _, extras = verify_draft.load_sidecar(new)
    assert extras["ask"] == {"text": "do the thing"}
    assert extras["audience"]["profile_hash"] == "abc"


def test_missing_sidecar_returns_none_and_empty_extras(tmp_path):
    vers, warns, extras = verify_draft.load_sidecar(tmp_path / "nope.yaml")
    assert vers is None and extras == {}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, (False, "missing", None)),
        ({"ask": "none"}, (True, "none", None)),
        ({"ask": "NONE"}, (True, "none", None)),
        ({"ask": {"text": "decide the vehicle"}}, (True, "text", "decide the vehicle")),
        ({"ask": "decide the vehicle"}, (True, "text", "decide the vehicle")),
        ({"ask": None}, (True, "invalid", None)),
        ({"ask": {"text": "  "}}, (True, "invalid", None)),
    ],
)
def test_parse_ask(raw, expected):
    assert verify_draft.parse_ask(raw) == expected

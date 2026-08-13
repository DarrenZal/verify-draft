"""gate_helpers.py — shared verification-gate plumbing for send scripts.

Used by send-proton.py, send-slack.py, send-signal.py, send-telegram.py.
Each send script:
  1. Imports `add_verify_args(parser)` to add the gate's CLI surface
  2. Calls `run_verification_gate(args, body_file=..., body_inline=...)` before send
  3. Calls `append_audit_row(args, skill=..., recipients=..., body=..., gate_report=..., ...)` after send (or after gate block/skip)

See: ~/.claude/plans/trustable-email-stack-verification-gate.md
"""
from __future__ import annotations

import calendar
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

AUDIT_LOG = Path.home() / ".claude" / "local" / "send-audit.jsonl"
VERIFY_SCRIPT = Path(__file__).resolve().parent / "verify_draft.py"
ALLOWLIST_FILE = Path.home() / ".claude" / "local" / "verify-allowlist.txt"
TIER_ZERO_FILE = Path.home() / ".claude" / "local" / "tier-zero-recipients.txt"
TIER_TWO_FILE = Path.home() / ".claude" / "local" / "tier-two-recipients.txt"


def _read_recipient_list(path: Path) -> set:
    """Read one-identifier-per-line file, stripping comments + blanks."""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def all_recipients_allowlisted(recipients: list) -> bool:
    """Return True iff every recipient is on the allowlist (D7: every-one-or-none)."""
    if not recipients:
        return False
    allowlist = _read_recipient_list(ALLOWLIST_FILE)
    return all(r in allowlist for r in recipients)


def resolve_tier(recipients: list) -> int:
    """Return the effective tier for a send: max(tier_of(r) for r in recipients). Default 1.

    Tier 0 = sensitive (operator confirm required after gate). Tier 2 = auto-send.
    """
    if not recipients:
        return 1
    tier_zero = _read_recipient_list(TIER_ZERO_FILE)
    tier_two = _read_recipient_list(TIER_TWO_FILE)
    if any(r in tier_zero for r in recipients):
        return 0
    if all(r in tier_two for r in recipients):
        return 2
    return 1


def add_verify_args(parser) -> None:
    """Add gate-related CLI flags to an argparse parser."""
    parser.add_argument(
        "--verify",
        choices=["strict", "warn", "off"],
        default="strict",
        help="Verification gate mode (default: strict). strict = block on unverified claims.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Request override; exits with instructions to re-run with --skip-verify-confirmed-by-operator <UNIX_TS>.",
    )
    parser.add_argument(
        "--skip-verify-confirmed-by-operator",
        type=int,
        default=None,
        metavar="UNIX_TS",
        help="Operator-pasted timestamp (must be within 60s of now) to bypass verification gate.",
    )
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="Permit a byte-identical resend to the same recipients within 15 min. "
             "Without this, an identical send is refused (exit 3) — see assert_not_duplicate.",
    )
    parser.add_argument(
        "--allow-stacking",
        action="store_true",
        help="Permit a second substantial group message on the same day. Without this, "
             "stacking is refused (exit 3) — see assert_not_stacking. Deliberately NOT "
             "covered by --allow-duplicate: a different-worded second memo is the failure.",
    )
    parser.add_argument(
        "--shape-override-confirmed-by-operator",
        type=int,
        default=None,
        metavar="UNIX_TS",
        help="Operator-pasted timestamp (must be within 60s of now) disabling the shape "
             "checks for this send. Same contract as --skip-verify-confirmed-by-operator: "
             "an agent cannot fill this in, so an agent cannot turn the shape checks off.",
    )
    parser.add_argument(
        "--confirm-send-by-operator",
        type=int,
        default=None,
        metavar="UNIX_TS",
        help="Operator-pasted timestamp (≤60s) confirming a Tier-0 send. Required for sensitive recipients (see ~/.claude/local/tier-zero-recipients.txt). Distinct from --skip-verify (which bypasses the gate; this confirms a gated send to a sensitive recipient).",
    )


# ── Duplicate-send guard ──────────────────────────────────────────────────
# Added 2026-07-31 after three DMs were sent twice. Root cause was not the gate
# but the operator loop around it: a gated send takes up to ~2 minutes (Layer 3
# shells out to `claude -p`), and both a "nothing in the channel yet" check and a
# "no output file yet" check were read as *terminal* states seconds after launch.
# Each was actually mid-flight, so the send was relaunched and duplicated.
#
# Discipline cannot fix this, because the ambiguity is real and the operator (human
# or agent) cannot see inside a running gate. So make the send idempotent instead:
# every completed send already writes draft_sha256 + recipients to the audit log,
# and nothing read it back. Now it does.

DUPLICATE_WINDOW_SECONDS = 900  # 15 min — long enough to cover a slow gate + retry


def _recent_identical_send(*, body: str, recipients: list, window: int = DUPLICATE_WINDOW_SECONDS):
    """Return the audit row for an identical (body, recipients) send inside the window.

    Identity is sha256(body) + the sorted recipient list, which is exactly what a
    relaunch of the same command produces. Returns None when there is no match, or
    when the log is unreadable — this guard must never be the reason a send fails.
    """
    if not body:
        return None
    try:
        if not AUDIT_LOG.exists():
            return None
        want_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        want_to = sorted(recipients or [])
        now = time.time()
        # Read the tail only; the log grows without bound.
        lines = AUDIT_LOG.read_text(errors="replace").splitlines()[-400:]
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("dry_run") or row.get("send_status") != "ok":
                continue
            if row.get("draft_sha256") != want_sha:
                continue
            if sorted(row.get("recipients") or []) != want_to:
                continue
            try:
                # timegm treats the struct as UTC, which is what the 'Z' means.
                # NOT mktime()-time.timezone: mktime reads it as local, and
                # time.timezone is the STANDARD-time offset, so under DST that
                # combination is silently off by one hour — which made a 38-second-old
                # send measure as 3638s and slip straight past a 900s window.
                sent = calendar.timegm(time.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%SZ"))
            except Exception:
                continue
            if now - sent <= window:
                return row
        return None
    except Exception:
        return None  # fail open: never block a send because the guard misbehaved


def assert_not_duplicate(args, *, body: str, recipients: list) -> None:
    """Refuse a byte-identical resend to the same recipients inside the window.

    Override with --allow-duplicate when a repeat is genuinely intended (a nudge
    with the same wording, a resend after the recipient deleted it).
    """
    if getattr(args, "allow_duplicate", False):
        return
    prior = _recent_identical_send(body=body, recipients=recipients)
    if prior is None:
        return
    print(
        "ERROR: identical message already delivered to these recipients.\n"
        f"  sent at   : {prior.get('timestamp')}\n"
        f"  recipients: {prior.get('recipients')}\n"
        f"  confirmation: {prior.get('send_confirmation')}\n"
        f"  draft sha : {(prior.get('draft_sha256') or '')[:16]}\n"
        "\n"
        "  A gated send takes up to ~2 minutes; if the previous attempt looked\n"
        "  hung or truncated, it very likely still completed. Check the channel\n"
        "  before assuming otherwise.\n"
        "  If you really do mean to send it again, pass --allow-duplicate.",
        file=sys.stderr,
    )
    sys.exit(3)


# ── Stacking guard ────────────────────────────────────────────────────────
# Added 2026-08-05. assert_not_duplicate catches the SAME message sent twice; this
# catches DIFFERENT messages stacked on the same room on the same day, which is the
# failure that actually happened: four substantial notes to one project group inside a
# single evening, and a recipient the next morning saying they had a hard time following
# large AI-generated updates. Each message passed the gate individually. Nothing looked
# at the pile.
#
# House rule hr-upd-1 says at most one substantial group message per day, consolidated;
# hr-upd-2 says edit the recent unread one rather than stacking. This is that pair,
# promoted from prose to a mechanical check.

STACKING_MIN_WORDS = 80  # below this it's a note, not a memo; stacking those is fine


def _word_count(text: str) -> int:
    return len((text or "").split())


def _fresh_operator_ts(value: Optional[int], *, window: int = 60) -> bool:
    """True iff `value` is an operator timestamp within `window` seconds of now."""
    if value is None:
        return False
    return abs(int(time.time()) - int(value)) <= window


def _is_today_local(ts_utc: str) -> bool:
    """Audit timestamps are UTC ('...Z'); 'same day' means the operator's calendar day."""
    try:
        epoch = calendar.timegm(time.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return False
    row, now = time.localtime(epoch), time.localtime()
    return (row.tm_year, row.tm_yday) == (now.tm_year, now.tm_yday)


def _prior_substantial_group_send(*, recipients: list):
    """Most recent same-day, same-room, substantial, non-edit send. None when clear."""
    try:
        if not AUDIT_LOG.exists():
            return None
        want = set(recipients or [])
        if not want:
            return None
        lines = AUDIT_LOG.read_text(errors="replace").splitlines()[-400:]
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("dry_run") or row.get("send_status") != "ok":
                continue
            # An edit is the REMEDY for stacking, not an instance of it.
            if (row.get("action") or "send") == "edit":
                continue
            if not (want & set(row.get("recipients") or [])):
                continue
            wc = row.get("word_count")
            # Rows written before word_count existed count as substantial: assuming a
            # pre-migration row was short is the assumption that fails open.
            if isinstance(wc, int) and wc < STACKING_MIN_WORDS:
                continue
            if not _is_today_local(row.get("timestamp") or ""):
                continue
            return row
        return None
    except Exception:
        return None  # fail open: never block a send because the guard misbehaved


def assert_not_stacking(args, *, body: str, recipients: list, audience: Optional[str]) -> None:
    """Refuse a second substantial group message into the same room on the same day."""
    if audience != "group":
        return
    if getattr(args, "allow_stacking", False):
        return
    if _fresh_operator_ts(getattr(args, "shape_override_confirmed_by_operator", None)):
        return
    words = _word_count(body)
    if words < STACKING_MIN_WORDS:
        return
    prior = _prior_substantial_group_send(recipients=recipients)
    if prior is None:
        return
    ts = int(time.time())
    print(
        "ERROR: a substantial message already went to this room today.\n"
        f"  sent at   : {prior.get('timestamp')} (UTC)\n"
        f"  recipients: {prior.get('recipients')}\n"
        f"  message id: {prior.get('send_confirmation')}\n"
        f"  words     : {prior.get('word_count', 'unrecorded')} (this one: {words})\n"
        "\n"
        "  Two long messages in one day is the shape people stop reading. Either:\n"
        f"    consolidate into the existing message  --edit {prior.get('send_confirmation')}\n"
        "    (an edit is not stacking and is never blocked by this guard), or\n"
        "    cut this one below 80 words, or\n"
        f"    override deliberately: --shape-override-confirmed-by-operator {ts}\n"
        "    (operator pastes a fresh timestamp; an agent must not fill it in), or\n"
        "    --allow-stacking if a second full message is genuinely the right call.",
        file=sys.stderr,
    )
    sys.exit(3)


# Tier enforcement is idempotent per recipient set: run_verification_gate calls it
# internally, and send-proton.py also calls it directly at its own call site. Both
# paths must work, and the operator must not be prompted twice for one send.
_TIER_ENFORCED: dict = {}


def _print_soft_findings(report: dict) -> None:
    """Print non-blocking findings on a PASSING send.

    A warn that only lands in a JSON blob nobody reads is a warn that does not exist —
    that is how the prose lessons died. Anything the gate noticed but did not block on
    goes to stderr where the operator sees it.
    """
    if not isinstance(report, dict):
        return
    shape_warns = [f for f in (report.get("shape") or []) if f.get("severity") == "warn"]
    reviewer = report.get("reviewer") or {}
    mismatches = reviewer.get("mismatches") or [] if reviewer.get("verdict") == "fail" else []
    if not shape_warns and not mismatches:
        return
    print("-" * 72, file=sys.stderr)
    print("GATE PASSED, with findings that did not block:", file=sys.stderr)
    for f in shape_warns:
        print(f"  shape {f.get('rule')} (warn): {f.get('detail')}", file=sys.stderr)
    if mismatches:
        print(f"  Layer 3 flagged {len(mismatches)} semantic mismatch(es) in warn mode:", file=sys.stderr)
        for m in mismatches:
            print(f"    • claim : {m.get('claim','')!r}", file=sys.stderr)
            print(f"      source: {m.get('source_ref','')!r}", file=sys.stderr)
            print(f"      reason: {m.get('mismatch_reason','')}", file=sys.stderr)
    print("-" * 72, file=sys.stderr)


def run_verification_gate(
    args,
    *,
    body_file: Optional[str],
    body_inline: Optional[str],
    recipients: Optional[list] = None,
    audience: Optional[str] = None,
    channel: Optional[str] = None,
):
    """Run the verification gate on the body about to be sent.

    Returns (passed: bool, gate_report: dict). On failure, calls sys.exit(2) directly.

    `recipients` (optional, prefixed per D8) enables allowlist + tier resolution.
    If passed and all recipients are on the allowlist, the gate is skipped entirely
    (warning logged). If not passed, allowlist is not consulted (back-compat).

    `audience` / `channel` turn on the shape checks in the engine. Omitting them is the
    pre-Phase-2 behavior exactly.
    """
    # Tier enforcement runs first when the caller knows who it is writing to: a Tier-0
    # recipient must be confirmed regardless of which branch below returns, including the
    # allowlist short-circuit. Idempotent, so send-proton.py's own later call is a no-op.
    if recipients is not None:
        enforce_tier_confirm(args, recipients)

    # Allowlist short-circuit (D8/D7): all recipients allowlisted → skip gate
    if recipients is not None and all_recipients_allowlisted(recipients):
        print(
            f"WARNING: all {len(recipients)} recipient(s) on allowlist; verification gate skipped.",
            file=sys.stderr,
        )
        return True, {"allowlisted": True, "recipients": recipients, "skipped": True}

    # --skip-verify (no value) → operator-override instructions, exit
    if args.skip_verify and args.skip_verify_confirmed_by_operator is None:
        ts = int(time.time())
        print(
            "ERROR: Operator override required. The verification gate cannot be self-bypassed.\n"
            f"  Re-run with: --skip-verify-confirmed-by-operator {ts}\n"
            "  (timestamp must be within 60s of execution — paste from your shell, do NOT let an agent fill it)",
            file=sys.stderr,
        )
        sys.exit(2)

    # Operator-supplied timestamp → validate
    if args.skip_verify_confirmed_by_operator is not None:
        now = int(time.time())
        delta = abs(now - args.skip_verify_confirmed_by_operator)
        if delta > 60:
            print(
                f"ERROR: override timestamp is stale or future-dated (delta={delta}s, max 60s).\n"
                f"  Re-generate with: --skip-verify-confirmed-by-operator $(date +%s)",
                file=sys.stderr,
            )
            sys.exit(2)
        print(
            f"WARNING: verification gate bypassed by operator override (ts={args.skip_verify_confirmed_by_operator}).",
            file=sys.stderr,
        )
        return True, {"bypassed": True, "override_ts": args.skip_verify_confirmed_by_operator}

    # mode=off → skip
    if args.verify == "off":
        print("WARNING: --verify=off; gate skipped.", file=sys.stderr)
        return True, {"mode": "off", "skipped": True}

    # Inline body cannot be sidecar-gated → require explicit override
    if body_file is None and body_inline is not None:
        print(
            "ERROR: inline message body cannot be gate-verified (no sidecar convention).\n"
            "  Either: (a) use --message-file <path> with sibling <path>.verifications.yaml, or\n"
            "  (b) pass --verify=off and accept the audit-log mark, or\n"
            "  (c) pass --skip-verify-confirmed-by-operator <fresh-UNIX-ts>",
            file=sys.stderr,
        )
        sys.exit(2)

    # Body-file path → run the engine
    if body_file is None:
        print("ERROR: no body source supplied to the gate (this is a bug)", file=sys.stderr)
        sys.exit(2)

    body_path = Path(body_file).expanduser().resolve()
    if not body_path.exists():
        print(f"ERROR: body-file {body_path} does not exist", file=sys.stderr)
        sys.exit(2)

    if not VERIFY_SCRIPT.exists():
        print(
            f"ERROR: verify_draft.py not found at {VERIFY_SCRIPT}. Install the verify-draft engine first.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Default: reviewer on (warn-mode by config; see D9). Layer 3 fires; failures
    # only block in strict mode per verify-config.toml.
    cmd = [
        "python3", str(VERIFY_SCRIPT),
        "--draft", str(body_path),
        "--mode", args.verify,
        "--reviewer", "on",
    ]
    if audience:
        cmd += ["--audience", audience]
    if channel:
        cmd += ["--channel", channel]
    if recipients:
        cmd += ["--recipients", ",".join(recipients)]

    # Shape checks are on unless the OPERATOR turns them off with a fresh timestamp.
    shape_ts = getattr(args, "shape_override_confirmed_by_operator", None)
    if shape_ts is not None:
        if not _fresh_operator_ts(shape_ts):
            print(
                f"ERROR: --shape-override-confirmed-by-operator is stale or future-dated "
                f"(delta={abs(int(time.time()) - int(shape_ts))}s, max 60s).\n"
                f"  Re-generate with: --shape-override-confirmed-by-operator $(date +%s)",
                file=sys.stderr,
            )
            sys.exit(2)
        print(
            f"WARNING: shape checks disabled by operator override (ts={shape_ts}).",
            file=sys.stderr,
        )
        cmd += ["--shape", "off"]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(
            f"ERROR: verify_draft.py produced non-JSON output (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}",
            file=sys.stderr,
        )
        sys.exit(2)

    if proc.returncode == 0:
        _print_soft_findings(report)
        return True, report

    # Gate failed; print human-readable summary then refuse
    print("=" * 72, file=sys.stderr)
    print("VERIFICATION GATE FAILED — send blocked", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    for w in report.get("warnings", []):
        print(f"  warning: {w}", file=sys.stderr)
    shape_blocks = [f for f in (report.get("shape") or []) if f.get("severity") == "block"]
    if shape_blocks:
        print(f"\n  Shape — {len(shape_blocks)} rule(s) blocked this send:", file=sys.stderr)
        for f in shape_blocks:
            print(f"    • [{f.get('rule')}] {f.get('detail')}", file=sys.stderr)
        print(
            "\n  These are form defects, not sourcing defects: no sidecar entry can satisfy them.\n"
            "  Rewrite the message. If the shape is genuinely right and the check is wrong,\n"
            f"  the operator (not an agent) can pass --shape-override-confirmed-by-operator {int(time.time())}.",
            file=sys.stderr,
        )
    unver = report.get("unverified", [])
    if unver:
        print(f"\n  Layer 2 — {len(unver)} unverified claim(s):", file=sys.stderr)
        for c in unver:
            print(f"    • [{c['detector']}] {c['text']!r}", file=sys.stderr)
    reviewer = report.get("reviewer") or {}
    if reviewer.get("verdict") == "fail":
        mismatches = reviewer.get("mismatches", [])
        print(f"\n  Layer 3 (adversarial reviewer) — {len(mismatches)} semantic mismatch(es):", file=sys.stderr)
        for m in mismatches:
            print(f"    • claim: {m.get('claim','')!r}", file=sys.stderr)
            print(f"      source: {m.get('source_ref','')!r}", file=sys.stderr)
            print(f"      reason: {m.get('mismatch_reason','')}", file=sys.stderr)
    sidecar = report.get("sidecar_path")
    print(
        f"\n  To fix: extend {sidecar} with sources that actually support each claim,\n"
        f"   or revise the draft to match what the sources say.\n"
        f"  To bypass (operator only): pass --skip-verify-confirmed-by-operator $(date +%s)",
        file=sys.stderr,
    )
    sys.exit(2)


def enforce_tier_confirm(args, recipients: list) -> int:
    """Check tier requirements after gate passes. Returns the effective tier.

    For Tier 0 sends (sensitive recipients): require --confirm-send-by-operator <ts>.
    For Tier 2 sends: no extra confirmation; proceed.
    For Tier 1 (default): print a prompt; in non-TTY contexts, require the same flag.

    Idempotent: called once from inside run_verification_gate and again directly by
    send-proton.py. The second call for the same recipient set returns the memoized tier
    without re-printing, so one send never prompts the operator twice.
    """
    key = tuple(sorted(recipients or []))
    if key in _TIER_ENFORCED:
        return _TIER_ENFORCED[key]
    tier = resolve_tier(recipients)

    if tier == 0:
        if args.confirm_send_by_operator is None:
            ts = int(time.time())
            print(
                "=" * 72 + "\n"
                f"TIER 0 SEND — sensitive recipient(s): {', '.join(recipients)}\n"
                + "=" * 72 + "\n"
                "Operator confirmation required before this send proceeds.\n"
                f"  Re-run with: --confirm-send-by-operator {ts}\n"
                "  (timestamp must be within 60s of execution — paste from your shell)",
                file=sys.stderr,
            )
            sys.exit(2)
        now = int(time.time())
        if abs(now - args.confirm_send_by_operator) > 60:
            print(
                f"ERROR: --confirm-send-by-operator timestamp is stale or future-dated (delta={abs(now - args.confirm_send_by_operator)}s, max 60s).\n"
                f"  Re-generate with: --confirm-send-by-operator $(date +%s)",
                file=sys.stderr,
            )
            sys.exit(2)
        print(
            f"Tier 0 send confirmed by operator (ts={args.confirm_send_by_operator}).",
            file=sys.stderr,
        )

    # Tier 1 / 2: no extra confirmation in v1. Tier 1 prompts are deferred to v2 (TTY-only).
    _TIER_ENFORCED[key] = tier
    return tier


def append_audit_row(
    args,
    *,
    skill: str,
    recipients: list,
    body: str,
    gate_report: Optional[dict],
    send_confirmation: Optional[str],
    dry_run: bool,
    subject: Optional[str] = None,
    tier: Optional[int] = None,
    send_status: str = "ok",
    send_error: Optional[str] = None,
    audience: Optional[str] = None,
    channel: Optional[str] = None,
    action: str = "send",
) -> None:
    """Append one row to ~/.claude/local/send-audit.jsonl.

    `send_status` is one of: "ok" (delivered), "dry_run" (gate passed, no send
    attempted), "failed" (send attempted but errored), "gate_blocked" (gate refused
    the send — typically not called from this path since gate exits sys.exit(2)).
    `send_error` carries the failure reason when status="failed".

    `action` is "send" or "edit". `word_count` and `shape` are derived here rather than
    passed, so no caller can forget them — assert_not_stacking reads both back, and an
    unrecorded word_count is treated as substantial.
    """
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest() if body else None
    if tier is None:
        tier = resolve_tier(recipients)
    row = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skill": skill,
        "recipients": recipients,
        "subject": subject,
        "draft_sha256": body_sha,
        "verifications_block": gate_report,
        "layer3_verdict": (gate_report or {}).get("reviewer", {}).get("verdict", "skipped") if gate_report else "skipped",
        "layer3_mismatches": (gate_report or {}).get("reviewer", {}).get("mismatches") if gate_report else None,
        "tier": tier,
        "audience": audience,
        "channel": channel,
        "action": action,
        "word_count": _word_count(body),
        "shape": [
            {"rule": f.get("rule"), "severity": f.get("severity")}
            for f in ((gate_report or {}).get("shape") or [])
        ] if gate_report else [],
        "send_confirmation": send_confirmation,
        "send_status": send_status,
        "send_error": send_error,
        "dry_run": dry_run,
        "operator_override": {
            "used": args.skip_verify_confirmed_by_operator is not None,
            "timestamp_flag": args.skip_verify_confirmed_by_operator,
            "tier_confirm_ts": args.confirm_send_by_operator,
            "reason": None,
        },
    }
    with AUDIT_LOG.open("a") as fh:
        fh.write(json.dumps(row) + "\n")

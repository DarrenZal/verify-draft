#!/usr/bin/env python3
"""Reference sender: post or preview a Slack message, gated by verify-draft.

This is ONE REAL IMPLEMENTATION, not a framework. It has been gating live sends
since 2026-07-31. Copy the ~30 lines of gate wiring; the rest is one person's
conventions and you should expect to replace them.

The transferable parts, in order of importance:
  * `--send` REQUIRES `--message-file`, because an inline string has nowhere to
    carry a sidecar. Dry-run accepts `--message` so drafting stays fast.
  * The gate import is guarded and fails at SEND time, not import time, so a
    missing engine cannot silently disable enforcement.
  * `assert_not_duplicate` / `assert_not_stacking` run before transmission.
  * One audit row per successful send, so gate behaviour is reviewable later.

Token handling: never printed. Prefers SLACK_BOT_TOKEN / SLACK_USER_TOKEN from
the environment, falling back to a configured Slack MCP token in ~/.claude.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


API_BASE = "https://slack.com/api"


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def load_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_USER_TOKEN")
    if token:
        return token

    claude_json = Path.home() / ".claude.json"
    if not claude_json.exists():
        die("SLACK_BOT_TOKEN/SLACK_USER_TOKEN unset and ~/.claude.json not found")

    try:
        data = json.loads(claude_json.read_text())
        token = (
            data.get("mcpServers", {})
            .get("slack", {})
            .get("env", {})
            .get("SLACK_BOT_TOKEN")
        )
    except Exception as exc:  # pragma: no cover - defensive local config handling
        die(f"failed to read Slack token from ~/.claude.json: {exc}")

    if not token:
        die("no Slack token found in environment or ~/.claude.json mcpServers.slack.env")
    return token


def slack_call(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    clean_payload = {}
    for key, value in (payload or {}).items():
        if isinstance(value, bool):
            clean_payload[key] = "true" if value else "false"
        elif value is not None:
            clean_payload[key] = str(value)
    body = urllib.parse.urlencode(clean_payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/{method}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        die(f"Slack HTTP error calling {method}: {exc.code}")
    except urllib.error.URLError as exc:
        die(f"Slack network error calling {method}: {exc}")

    if not result.get("ok"):
        die(f"Slack API {method} failed: {result.get('error', 'unknown_error')}")
    return result


def parse_channel(value: str) -> str:
    # Slack app URL form: https://app.slack.com/client/T.../C...
    match = re.search(r"/client/[A-Z0-9]+/([CDG][A-Z0-9]+)", value)
    if match:
        return match.group(1)

    # Archive URL form: https://workspace.slack.com/archives/C.../p...
    match = re.search(r"/archives/([CDG][A-Z0-9]+)", value)
    if match:
        return match.group(1)

    # Raw channel, group, or DM id.
    match = re.fullmatch(r"[CDG][A-Z0-9]{8,}", value.strip())
    if match:
        return value.strip()

    die(f"could not parse Slack channel from: {value}")


def parse_thread_ts(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{10}\.\d{6}", value):
        return value
    match = re.search(r"/p(\d{10})(\d{6})", value)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match = re.fullmatch(r"p?(\d{10})(\d{6})", value)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    die(f"could not parse Slack thread timestamp from: {value}")


def users_list(token: str) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    cursor = ""
    while True:
        payload: dict[str, Any] = {"limit": 1000}
        if cursor:
            payload["cursor"] = cursor
        result = slack_call(token, "users.list", payload)
        members.extend(result.get("members", []))
        cursor = result.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return members


def user_label(member: dict[str, Any]) -> str:
    profile = member.get("profile", {}) or {}
    parts = [
        profile.get("display_name"),
        profile.get("real_name"),
        member.get("real_name"),
        member.get("name"),
    ]
    return " / ".join([p for p in parts if p])


def resolve_users(token: str, queries: list[str]) -> list[tuple[str, list[dict[str, Any]]]]:
    if not queries:
        return []
    members = users_list(token)
    resolved: list[tuple[str, list[dict[str, Any]]]] = []
    for query in queries:
        q = query.lower()
        matches = []
        for member in members:
            if member.get("deleted") or member.get("is_bot"):
                continue
            profile = member.get("profile", {}) or {}
            haystack = " ".join(
                str(v or "")
                for v in [
                    member.get("name"),
                    member.get("real_name"),
                    profile.get("display_name"),
                    profile.get("real_name"),
                    profile.get("email"),
                ]
            ).lower()
            if q in haystack:
                matches.append(member)
        resolved.append((query, matches))
    return resolved


# Verification gate (verify_draft). Wired 2026-07-31 — previously the gate existed
# but nothing invoked it, so it was convention only. See gate_helpers.run_verification_gate.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from gate_helpers import (add_verify_args, run_verification_gate,
                              append_audit_row, assert_not_duplicate,
                              assert_not_stacking)
    _GATE = True
except ImportError:  # engine not installed — fail loudly at send time, not import time
    _GATE = False


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or send a Slack message.")
    parser.add_argument("--channel", required=True, help="Slack channel id or URL")
    parser.add_argument("--message", help="Message text (inline). Allowed for dry-run; "
                                          "--send requires --message-file so the gate can verify a sidecar.")
    parser.add_argument("--message-file", help="Path to the draft; expects a sibling "
                                               "<path>.verifications.yaml. Required for --send.")
    parser.add_argument("--thread-ts", help="Thread timestamp or Slack message URL")
    parser.add_argument("--resolve-user", action="append", default=[], help="User name/email to resolve")
    parser.add_argument("--send", action="store_true", help="Actually post the message")
    parser.add_argument("--unfurl-links", action="store_true", help="Let Slack unfurl links")
    if _GATE:
        add_verify_args(parser)
    args = parser.parse_args()

    # Resolve the body from --message-file (gateable) or --message (inline, dry-run only).
    if args.message_file:
        body_path = Path(args.message_file).expanduser()
        if not body_path.exists():
            print(f"ERROR: --message-file {body_path} does not exist", file=sys.stderr)
            raise SystemExit(2)
        args.message = body_path.read_text()
    elif not args.message:
        print("ERROR: supply --message-file (preferred) or --message", file=sys.stderr)
        raise SystemExit(2)

    token = load_token()
    channel_id = parse_channel(args.channel)
    thread_ts = parse_thread_ts(args.thread_ts)

    auth = slack_call(token, "auth.test")
    info = slack_call(token, "conversations.info", {"channel": channel_id})
    channel = info.get("channel", {})

    print(f"Workspace: {auth.get('team')} ({auth.get('team_id')})")
    print(f"Posting as: {auth.get('user')} ({auth.get('user_id')})")
    print(
        "Channel: "
        f"#{channel.get('name', channel_id)} ({channel_id}), "
        f"private={channel.get('is_private')}, member={channel.get('is_member')}"
    )
    if thread_ts:
        print(f"Thread: {thread_ts}")

    for query, matches in resolve_users(token, args.resolve_user):
        print(f"\nUser matches for {query!r}:")
        if not matches:
            print("  none")
        for member in matches[:10]:
            print(f"  <@{member.get('id')}>  {user_label(member)}")
        if len(matches) > 10:
            print(f"  ... {len(matches) - 10} more")

    print("\nMessage:")
    print(args.message)

    if not args.send:
        print("\nDRY RUN ONLY. Re-run with --send to post.")
        return

    # ── Verification gate ────────────────────────────────────────────────
    # Runs AFTER the dry-run return above, so previewing is never gated —
    # only an actual send is.
    gate_report = None
    # Slack DM conversation ids start with D; everything else (C public, G private) is a room.
    audience = "dm" if channel_id.startswith("D") else "group"
    recipients = [f"slack:{channel_id}"]
    if _GATE:
        # Refuse a byte-identical resend before spending ~2min in the gate.
        assert_not_duplicate(args, body=args.message, recipients=recipients)
        assert_not_stacking(args, body=args.message, recipients=recipients, audience=audience)
        gate_report = run_verification_gate(
            args, body_file=args.message_file,
            body_inline=(None if args.message_file else args.message),
            recipients=recipients, audience=audience, channel="slack",
        )[1]
    else:
        print("ERROR: verify-draft engine not importable; refusing to send ungated.\n"
              "  Expected gate_helpers.py under scripts/verify-draft/.", file=sys.stderr)
        raise SystemExit(2)

    payload: dict[str, Any] = {
        "channel": channel_id,
        "text": args.message,
        "unfurl_links": bool(args.unfurl_links),
        "unfurl_media": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    posted = slack_call(token, "chat.postMessage", payload)
    ts = posted.get("ts")
    print(f"\nSENT channel={posted.get('channel')} ts={ts}")

    if _GATE:
        try:
            append_audit_row(
                args, skill="slack-send",
                recipients=recipients,
                body=args.message, gate_report=gate_report,
                send_confirmation=str(ts), dry_run=False,
                subject=f"#{channel.get('name', channel_id)}", send_status="ok",
                audience=audience, channel="slack", action="send",
            )
        except Exception as e:  # auditing must never break a completed send
            print(f"WARNING: audit row not written: {e}", file=sys.stderr)

    if ts:
        try:
            permalink = slack_call(
                token, "chat.getPermalink", {"channel": posted.get("channel"), "message_ts": ts}
            )
            print(f"Permalink: {permalink.get('permalink')}")
        except SystemExit:
            pass


if __name__ == "__main__":
    main()

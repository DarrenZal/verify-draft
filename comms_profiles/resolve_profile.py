#!/usr/bin/env python3
"""Resolve an audience -> the AUDIENCE BRIEF to draft from.

The draft-time half of the comms-profiles layer (sibling of
comms-channels/resolve_channel.py). Given people and/or a chat id, composes:

  1. VOICE       — the sender voice card (write AS the sender), always loaded
  2. HARD LIMITS — numeric budgets (house rules; min over any member overrides)
  3. THE ROOM    — one line-block per member from their People-note
                   `communication:` rules ([inferred] rules flagged as guesses)
  4. GROUP NORMS — from the Groups/<name>.md note matching the chat id
  5. TYPE RULES  — house rules for this communication type (update|ask|correction|share)
  6. CONFLICTS   — surfaced, not silently resolved
  7. footer      — profile_hash + a ready-to-paste sidecar `audience:` block

profile_hash is a sha256 over the canonical JSON of every resolved rule, so
editing any profile changes the hash and stales previously generated briefs.

Usage:
  resolve_profile.py --chat-id telegram:100200300 --type ask
  resolve_profile.py --person "Ada Lovelace" --person "Alan Turing" --type update
  resolve_profile.py --chat-id ... --json          # machine mode (for the gate)

Convention doc: docs/comms-profiles-convention.md
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _frontmatter import split_note  # noqa: E402

VAULT = os.environ.get("COMMS_CHANNELS_VAULT", os.path.expanduser("~/Documents/Notes"))
HOUSE_RULES = os.environ.get(
    "COMMS_HOUSE_RULES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "house-rules.example.yaml"),
)
# Whose voice is being written in. Set COMMS_SENDER_NAME to the note filename in
# <vault>/People/ that holds your own voice card.
SENDER_NOTE = os.environ.get("COMMS_SENDER_NAME", "Sender")
COMM_TYPES = ("update", "ask", "correction", "share")


def _load_yaml(path):
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def find_person_note(name):
    """People note by name or alias (case-insensitive); exact beats fuzzy."""
    q = name.strip().lower()
    best = None
    for p in glob.glob(os.path.join(VAULT, "People", "*.md")):
        fm, _, _ = split_note(p)
        if not fm:
            continue
        names = [str(fm.get("name", ""))] + [str(a) for a in (fm.get("aliases") or [])]
        low = [n.lower() for n in names if n]
        if q in low:
            return p, fm
        if best is None and any(q in n or n in q for n in low):
            best = (p, fm)
    return best or (None, None)


def find_group_note_by_chat(chat_id):
    for p in glob.glob(os.path.join(VAULT, "Groups", "*.md")):
        fm, _, _ = split_note(p)
        if chat_id in (fm.get("chat_ids") or []):
            return p, fm
    return None, None


def _wikilink_name(link):
    m = re.match(r"\[\[(?:People/)?([^\]|]+?)(?:\|[^\]]*)?\]\]", str(link).strip())
    return m.group(1) if m else str(link).strip()


def _person_block(name):
    path, fm = find_person_note(name)
    if not path:
        return {"person": name, "found": False, "rules": [], "register": None}
    comm = fm.get("communication") or {}
    return {
        "person": fm.get("name", name),
        "found": True,
        "note": os.path.relpath(path, VAULT),
        "rules": comm.get("rules") or [],
        "register": comm.get("register"),
    }


def resolve(people, chat_id, comm_type, channel):
    house = _load_yaml(HOUSE_RULES) if os.path.exists(HOUSE_RULES) else {}
    limits = dict(house.get("limits") or {})

    group = None
    members = list(people or [])
    if chat_id:
        gpath, gfm = find_group_note_by_chat(chat_id)
        if gpath:
            group = {
                "name": gfm.get("name"),
                "note": os.path.relpath(gpath, VAULT),
                "norms": gfm.get("norms") or [],
                "project": gfm.get("project"),
            }
            for link in gfm.get("members") or []:
                nm = _wikilink_name(link)
                if nm.lower() != SENDER_NOTE.lower() and nm not in members:
                    members.append(nm)
        else:
            group = {"name": None, "note": None, "norms": [],
                     "warning": f"no Groups/*.md note carries chat_id {chat_id!r}"}

    voice = _person_block(SENDER_NOTE)
    room = [_person_block(m) for m in members]

    def _channel_ok(rule):
        ctx = str(rule.get("context") or "")
        return (not channel) or (channel in ctx) or not any(
            ch in ctx for ch in ("telegram", "signal", "slack", "email"))

    house_voice = [r for r in (house.get("voice") or []) if _channel_ok(r)]
    house_format = [r for r in (house.get("format") or []) if _channel_ok(r)]
    type_rules = [r for r in (house.get(comm_type) or []) if _channel_ok(r)] if comm_type else []

    # conflicts: pairs worth surfacing rather than silently resolving
    conflicts = []
    all_member_rules = [(b["person"], r) for b in room for r in b["rules"]]
    wants_detail = [p for p, r in all_member_rules if "detail" in str(r.get("rule", "")).lower()
                    and "strip" in str(r.get("rule", "")).lower()]
    finds_long_hard = [p for p, r in all_member_rules
                       if any(k in str(r.get("rule", "")).lower() for k in ("long", "short", "high level"))]
    if wants_detail and finds_long_hard:
        conflicts.append(
            f"{'/'.join(sorted(set(wants_detail)))} wants detail kept, but long messages lose "
            f"{'/'.join(sorted(set(finds_long_hard)))}: resolve with a short body + link to the detail "
            f"on their preferred surface.")

    payload = {
        "sender_voice": voice,
        "limits": limits,
        "room": room,
        "group": group,
        "house_voice": house_voice,
        "house_format": house_format,
        "type": comm_type,
        "type_rules": type_rules,
        "channel": channel,
        "conflicts": conflicts,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    payload["profile_hash"] = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return payload


def _fmt_rule(r, show_conf=True):
    conf = str(r.get("confidence", ""))
    tag = " [inferred — a guess, do not treat as their stated preference]" \
        if (show_conf and conf and conf != "confirmed") else ""
    return f"  - {r.get('rule')}{tag}"


def render_brief(p):
    out = []
    out.append("=== AUDIENCE BRIEF ===")
    v = p["sender_voice"]
    out.append("\n-- VOICE: write as the sender --")
    if v["found"] and (v["register"] or v["rules"]):
        if v["register"]:
            out.append(f"  {v['register']}")
        out.extend(_fmt_rule(r, show_conf=False) for r in v["rules"])
    else:
        out.append("  (no voice card yet: mine your own past messages in this thread and mirror them)")
    for r in p["house_voice"]:
        out.append(f"  - {r.get('rule')}")
    if p["limits"]:
        out.append("\n-- HARD LIMITS --")
        for k, val in p["limits"].items():
            out.append(f"  {k}: {val}")
    if p["room"]:
        out.append("\n-- THE ROOM (write to all of them, not one) --")
        for b in p["room"]:
            out.append(f"  {b['person']}:" if b["found"] else
                       f"  {b['person']}: (no People-note profile — profile_update.py can add one)")
            if b.get("register"):
                out.append(f"    how they write: {b['register']}")
            out.extend("  " + _fmt_rule(r) for r in b["rules"])
    g = p.get("group")
    if g:
        if g.get("warning"):
            out.append(f"\n-- GROUP: WARNING: {g['warning']} --")
        elif g.get("norms") is not None:
            out.append(f"\n-- GROUP NORMS ({g.get('name')}) --")
            out.extend(_fmt_rule(r) for r in g.get("norms") or [])
    if p["house_format"]:
        out.append("\n-- FORMAT --")
        out.extend(f"  - {r.get('rule')}" for r in p["house_format"])
    if p["type_rules"]:
        out.append(f"\n-- TYPE: {p['type']} --")
        out.extend(f"  - {r.get('rule')}" for r in p["type_rules"])
    if p["conflicts"]:
        out.append("\n-- CONFLICTS (resolve consciously) --")
        out.extend(f"  ! {c}" for c in p["conflicts"])
    out.append(f"\nprofile_hash: {p['profile_hash']}")
    out.append("\nsidecar block (paste into the draft's .verifications.yaml):")
    out.append("audience:")
    out.append(f"  resolved: {json.dumps([b['person'] for b in p['room']] or 'sender-only')}")
    out.append(f"  profile_hash: {p['profile_hash']}")
    out.append(f"  type: {p['type'] or 'unspecified'}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person", action="append", default=[], help="recipient name (repeatable)")
    ap.add_argument("--chat-id", help="platform-prefixed chat id, e.g. telegram:100200300")
    ap.add_argument("--type", choices=COMM_TYPES, help="communication type")
    ap.add_argument("--channel", choices=["telegram", "signal", "slack", "email"], help="filter channel-specific rules")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if not args.person and not args.chat_id:
        ap.error("need --person and/or --chat-id")
    p = resolve(args.person, args.chat_id, args.type, args.channel)
    print(json.dumps(p, indent=2, ensure_ascii=False) if args.json else render_brief(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())

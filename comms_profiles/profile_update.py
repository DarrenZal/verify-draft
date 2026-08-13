#!/usr/bin/env python3
"""The single write path for communication profiles (harvest loop).

Adds/updates one rule at a time on a People note's `communication:` block or a
Groups note's `norms:` list — surgical frontmatter rewrite via the same
_frontmatter helpers channel_confirm.py uses (body preserved byte-for-byte,
yaml width pinned). NEVER edit profile YAML by hand or with regex.

Confidence invariant (mirrors channels): `inferred` rules appear in briefs
flagged as guesses and are never enforceable; only `confirmed` rules (operator-
or recipient-evidenced) may ever back a mechanical gate check.

Usage:
  profile_update.py person "Ada Lovelace" --id ada-3 \
      --rule "Short body, link the detail rather than inlining it" --kind format \
      --confidence confirmed --source recipient \
      --evidence "2026-03-04, in thread: asked for the summary up top and the rest linked" \
      [--context project-x]
  profile_update.py person "Sender" --register "chat: lowercase-casual, ..."
  profile_update.py group "Platform Team" --id plat-2 --rule ... --evidence ...
  profile_update.py confirm "Ada Lovelace" ada-2           # inferred -> confirmed
  profile_update.py show "Ada Lovelace"

Exit codes: 0 ok, 1 not found, 2 bad args.
"""
import argparse
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "comms-channels"))
from _frontmatter import split_note, write_frontmatter  # noqa: E402

VAULT = os.environ.get("COMMS_CHANNELS_VAULT", os.path.expanduser("~/Documents/Notes"))
KINDS = ("style", "format", "aversion", "ask-pattern", "channel", "vocabulary")
SOURCES = ("operator", "recipient", "observed", "mined")


def _find_note(kind, name):
    folder = "People" if kind == "person" else "Groups"
    q = name.strip().lower()
    for p in glob.glob(os.path.join(VAULT, folder, "*.md")):
        fm, _, _ = split_note(p)
        # filename stem is the vault's real identity; some notes lack a name: field
        names = [os.path.splitext(os.path.basename(p))[0], str(fm.get("name", ""))] \
            + [str(a) for a in (fm.get("aliases") or [])]
        if q in [n.lower() for n in names if n]:
            return p, fm
    return None, None


def _today():
    return datetime.date.today().isoformat()


def cmd_upsert(args):
    note_kind = "person" if args.cmd == "person" else "group"
    path, fm = _find_note(note_kind, args.name)
    if not path:
        print(f"ERROR: no {note_kind} note named {args.name!r} in the vault", file=sys.stderr)
        return 1
    _, _, body = split_note(path)

    if note_kind == "person":
        comm = fm.setdefault("communication", {})
        rules = comm.setdefault("rules", [])
        comm["updated"] = _today()
        if args.register is not None:
            comm["register"] = args.register
    else:
        rules = fm.setdefault("norms", [])

    if args.id:
        if not args.rule or not args.evidence:
            print("ERROR: --rule and --evidence are required with --id", file=sys.stderr)
            return 2
        new = {
            "id": args.id,
            "rule": args.rule,
            "kind": args.kind,
            "confidence": args.confidence,
            "source": args.source,
            "evidence": args.evidence,
        }
        if args.context:
            new["context"] = args.context
        if args.mechanical:
            new["mechanical"] = args.mechanical
        existing = [i for i, r in enumerate(rules) if r.get("id") == args.id]
        if existing:
            rules[existing[0]] = new
            action = "updated"
        else:
            rules.append(new)
            action = "added"
    elif args.register is not None:
        action = "register-set"
    else:
        print("ERROR: nothing to do (need --id or --register)", file=sys.stderr)
        return 2

    write_frontmatter(path, fm, body)
    print(json.dumps({"note": os.path.relpath(path, VAULT), "action": action,
                      "rule_id": args.id, "rules_total": len(rules)}))
    return 0


def cmd_confirm(args):
    path, fm = _find_note("person", args.name)
    if not path:
        path, fm = _find_note("group", args.name)
    if not path:
        print(f"ERROR: no note named {args.name!r}", file=sys.stderr)
        return 1
    _, _, body = split_note(path)
    rules = (fm.get("communication") or {}).get("rules") if "communication" in fm else fm.get("norms")
    if not rules:
        print("ERROR: no rules on that note", file=sys.stderr)
        return 1
    hits = [r for r in rules if r.get("id") == args.rule_id]
    if not hits:
        print(f"ERROR: no rule with id {args.rule_id!r}", file=sys.stderr)
        return 1
    hits[0]["confidence"] = "confirmed"
    if "communication" in fm:
        fm["communication"]["updated"] = _today()
    write_frontmatter(path, fm, body)
    print(json.dumps({"note": os.path.relpath(path, VAULT), "action": "confirmed", "rule_id": args.rule_id}))
    return 0


def cmd_show(args):
    path, fm = _find_note("person", args.name)
    if not path:
        path, fm = _find_note("group", args.name)
    if not path:
        print(f"ERROR: no note named {args.name!r}", file=sys.stderr)
        return 1
    block = fm.get("communication") if "communication" in fm else {"norms": fm.get("norms")}
    print(json.dumps(block or {}, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for kind in ("person", "group"):
        s = sub.add_parser(kind)
        s.add_argument("name")
        s.add_argument("--id")
        s.add_argument("--rule")
        s.add_argument("--kind", choices=KINDS, default="style")
        s.add_argument("--confidence", choices=("confirmed", "inferred"), default="inferred")
        s.add_argument("--source", choices=SOURCES, default="mined")
        s.add_argument("--evidence")
        s.add_argument("--context")
        s.add_argument("--mechanical", help="gate check name, only when promoted to L2")
        s.add_argument("--register", help="(person) set the register description")
    c = sub.add_parser("confirm")
    c.add_argument("name")
    c.add_argument("rule_id")
    sh = sub.add_parser("show")
    sh.add_argument("name")
    args = ap.parse_args(argv)
    if args.cmd in ("person", "group"):
        return cmd_upsert(args)
    if args.cmd == "confirm":
        return cmd_confirm(args)
    return cmd_show(args)


if __name__ == "__main__":
    sys.exit(main())

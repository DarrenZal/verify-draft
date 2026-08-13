#!/usr/bin/env python3
"""Surgical frontmatter read/write for People notes.

Writers here rewrite ONLY the YAML frontmatter block (between the first two
`---` fences) and preserve the note body byte-for-byte. This honors the vault
file-safety rule: never rewrite the whole file, never touch the body.
"""
import os

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def split_note(path):
    """Return (frontmatter_dict, raw_fm_text, body_text). body includes no leading fence."""
    txt = open(path, encoding="utf-8").read()
    if not txt.startswith("---"):
        return {}, "", txt
    end = txt.find("\n---", 3)
    if end < 0:
        return {}, "", txt
    raw_fm = txt[3:end].lstrip("\n")
    body = txt[end + 4:]          # skip the "\n---"
    fm = {}
    if yaml:
        try:
            fm = yaml.safe_load(raw_fm) or {}
        except Exception:
            fm = {}
    return fm, raw_fm, body


def write_frontmatter(path, fm, body):
    """Rewrite the note with new frontmatter dict + unchanged body. Block YAML, keys unsorted."""
    if yaml is None:
        raise RuntimeError("PyYAML required to write frontmatter")
    # width=4096 matches process-note-gate's vault_yaml.py: PyYAML's default 80-col
    # folding of long plain scalars has corrupted vault frontmatter before
    # (orphaned continuation lines in mentionedIn); never let values fold.
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False, width=4096)
    new = "---\n" + dumped + "---" + body
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, path)

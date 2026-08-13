#!/usr/bin/env python3
"""verify_draft.py — gate external-facing drafts against unverified factual claims.

Reads a draft file + YAML verifications sidecar; detects attributive claims via
regex (Layer 2 mechanical pass); confirms each detected claim has a matching
entry with a populated `source` block in the sidecar.

Output: JSON report to stdout. Exit 0 = pass, 2 = unverified claims (strict mode),
3 = malformed input.

Sidecar contract and rationale: see README.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    sys.stderr.write("ERROR: PyYAML required. Install with: pip install pyyaml\n")
    sys.exit(3)

# ---------------------------------------------------------------------------
# Detection regexes (Layer 2 mechanical pass)
# ---------------------------------------------------------------------------

ATTRIB_VERBS = r"(?:said|confirmed|told|wrote|noted|mentioned|flagged|explained|stated|reported|announced|claimed)"
ATTRIB_PREP = r"(?:from|via|per|by|according\s+to|taken\s+from)"
# Title-case noun-phrase (potential proper noun)
PROPER_NOUN = r"[A-Z][A-Za-z0-9'\-]+(?:\s+[A-Z][A-Za-z0-9'\-]+){0,3}"

# Operator-private tooling and storage. Naming any of these to a third party either
# (a) asserts shared infrastructure the recipient does not have, or (b) points them at
# an artifact they cannot open. Either way it needs a conscious sidecar justification —
# usually the honest resolution is to rewrite the sentence, not to justify it.
# Legitimate uses exist (e.g. telling a co-dev about `process-note` in a dev thread);
# those cost one sidecar line.
INTERNAL_TOOLING = "|".join(
    [
        r"morning\s+brief(?:s|ing)?",
        r"personal[-\s]koi",
        r"koi[-\s](?:backend|processor)",
        r"claude[-\s]mem",
        r"verify[_-]draft",
        r"process[-\s]note",
        r"meeting[-\s]notes\s+skill",
        r"whats[-\s]next",
        r"session[-\s]rename",
        r"(?:the|my|our)\s+vault",
        r"vault\s+(?:note|record|path)",
        r"Obsidian",
        r"scratchpad",
        r"task\s+registry",
        r"comms\s+outbox",
        r"LaunchAgent|launchd",
        r"entity\s+resolution\s+issues",
        # slash-commands: /tasks, /end, /whats-next, /process-note …
        r"/(?:tasks|end|whats-next|process-note|meeting-notes|new-plan|review-plan|find-session)",
    ]
)

# ── placeholder hard-block ───────────────────────────────────────────────────
#
# A placeholder in a draft is a MECHANICAL error, not a factual claim, so it can never be
# satisfied by a sidecar entry and it blocks in warn mode too. Only mode=off skips it.
#
# Why this exists: on 2026-07-29 an invoice cover note to a client went through this gate with
# `passed: true, unverified: []` while still containing the literal token «AMOUNT» in place of
# the money figure. Every factual claim had a source; the draft was simply unfinished. The gate
# had no concept of "unfinished", only of "unsourced".
#
# Patterns are deliberately high-precision — a false block on a real send is expensive:
#   «AMOUNT»          guillemets around ALL-CAPS (the observed failure)
#   {{name}}          mustache/handlebars templating
#   [TBD] <INSERT X>  bracketed or angled fill-me markers
#   TODO: / FIXME     never legitimate in a message actually being sent
#   ____              fill-in-the-blank rules
#   Lorem ipsum       boilerplate left in
# Bare "TBD" is deliberately NOT matched — "timing TBD" is legitimate English in a real message.
PLACEHOLDER_PATTERNS = [
    ("guillemet_caps", re.compile(r"«\s*[A-Z][A-Z0-9_ ]{1,40}\s*»")),
    ("mustache", re.compile(r"\{\{[^}\n]{1,60}\}\}")),
    ("bracketed_marker", re.compile(r"\[(?:TBD|TODO|FIXME|PLACEHOLDER|XXX+)\]", re.IGNORECASE)),
    ("angled_marker", re.compile(r"<(?:TBD|TODO|FIXME|PLACEHOLDER|INSERT\b[^>\n]{0,40})>", re.IGNORECASE)),
    ("todo_marker", re.compile(r"\b(?:TODO|FIXME)\b:?")),
    ("blank_rule", re.compile(r"_{3,}")),
    ("lorem", re.compile(r"\bLorem ipsum\b", re.IGNORECASE)),
]


def detect_placeholders(text: str) -> list[dict]:
    """Return every placeholder occurrence. Non-empty => the draft is unfinished, full stop."""
    found: list[dict] = []
    for name, rx in PLACEHOLDER_PATTERNS:
        for m in rx.finditer(text):
            found.append({
                "pattern": name,
                "text": m.group(0)[:60],
                "location": f"offset {m.start()}:{m.end()}",
            })
    return sorted(found, key=lambda f: int(f["location"].split()[1].split(":")[0]))


REGEXES = [
    # "(taken from VLC)" / "(from X)" / "(per Y)" / "(via Z)" — parenthetical attribution
    (
        "paren_attribution",
        re.compile(
            rf"\(([^)]*\b{ATTRIB_PREP}\s+[A-Z][^)]*)\)",
            re.IGNORECASE,
        ),
    ),
    # "X said …" / "X confirmed …" — attributive verb after proper noun
    (
        "verb_attribution",
        re.compile(rf"\b({PROPER_NOUN})\s+{ATTRIB_VERBS}\b", re.UNICODE),
    ),
    # "per X" / "according to X" / "via X" inline (not just in parens). Capture full phrase.
    # Use IGNORECASE for prepositions ("Per Alice" at sentence start works) but force
    # case-sensitive PROPER_NOUN via inline `(?-i:...)` so "from your direct" stays a
    # false negative (lowercase "your" can't be a proper noun).
    (
        "inline_attribution",
        re.compile(
            rf"\b({ATTRIB_PREP}\s+(?-i:{PROPER_NOUN}))\b",
            re.IGNORECASE,
        ),
    ),
    # Explicit dates: ISO 8601 (2026-05-16), written ("May 16, 2026"), numeric (5/16/2026)
    (
        "date_iso",
        re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    ),
    (
        "date_written",
        re.compile(
            r"\b((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:,\s+\d{4})?)\b"
        ),
    ),
    # Prices ($1,234 / $5,499 / $8K)
    (
        "price",
        re.compile(r"(\$\d[\d,]*(?:\.\d+)?[KMB]?)\b"),
    ),
    # Versions: require `v` prefix (v4.6, v2025.1) OR 3+ dot-separated components (1.2.3)
    # to avoid catching bare "5.1" / "4.0" which are usually numbers in prose
    (
        "version",
        re.compile(r"\b(v\d+(?:\.\d+)+|\d+\.\d+\.\d+(?:\.\d+)*)\b"),
    ),
    # Quoted strings adjacent to attribution: "...word..." after "wrote/said/noted"
    (
        "quoted_attribution",
        re.compile(rf"{ATTRIB_VERBS}[^\"\n]{{0,40}}[\"“]([^\"”]+)[\"”]", re.IGNORECASE),
    ),
    # Operator-private tooling named in an external draft. Two distinct failure modes:
    #   (a) implying the recipient shares infrastructure they have no access to
    #       ("tasks are registered so they'll surface in the morning briefs" — the
    #        morning brief is the author's own daily brief; the recipient has no such thing);
    #   (b) pointing a recipient at an artifact they cannot open ("it's in the vault
    #       record — worth a read"), which reads as a deliverable but is unreachable.
    # Both shipped in team Telegram msg 454 on 2026-07-29 and the operator had to
    # hand-edit the message after sending. See INTERNAL_TOOLING below.
    (
        "internal_tooling",
        re.compile(rf"\b({INTERNAL_TOOLING})\b", re.IGNORECASE),
    ),
]

# Phrases inside parens that are obviously NOT attribution (suppress false positives)
PAREN_NEGATIVES = {
    "from time to time",
    "from now on",
    "from scratch",
    "from there",
    "from here",
    "from a distance",
}


@dataclass
class Claim:
    text: str
    detector: str
    location: str  # offset:length or section name
    source_present: bool = False
    source_value: Optional[dict] = None


@dataclass
class Report:
    passed: bool
    mode: str
    draft_path: str
    sidecar_path: Optional[str]
    claims: list[Claim] = field(default_factory=list)
    unverified: list[Claim] = field(default_factory=list)
    placeholders: list[dict] = field(default_factory=list)
    shape: list[dict] = field(default_factory=list)  # {rule, severity, detail}
    warnings: list[str] = field(default_factory=list)
    reviewer: Optional[dict] = None  # Layer 3 result placeholder


# ---------------------------------------------------------------------------
# Sidecar loading
# ---------------------------------------------------------------------------

SOURCE_TYPES = {"file", "url", "quote", "message_id", "conversation_ref"}

# Additive sidecar keys read by the shape checks. Unknown top-level keys stay ignored,
# so an older sidecar parses unchanged and a newer one is not rejected by an older gate.
SIDECAR_EXTRA_KEYS = ("ask", "audience")


def load_sidecar(path: Path) -> tuple[Optional[list], list[str], dict]:
    """Return (verifications_list, warnings, extras). None list = file missing.

    `extras` carries the shape-check keys (`ask:`, `audience:`) when present. They are
    additive: a sidecar that omits both parses exactly as it did before, and the claim
    pass never consults them. Only the shape checks (and only under --audience) do.
    """
    warnings: list[str] = []
    extras: dict = {}
    if not path.exists():
        return None, [], extras
    try:
        with path.open("r") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as e:
        warnings.append(f"sidecar YAML parse error: {e}")
        return [], warnings, extras
    if data is None:
        # Empty file → treat as empty verifications
        return [], warnings, extras
    if not isinstance(data, dict):
        warnings.append("sidecar root must be a mapping with `verifications:` key")
        return [], warnings, extras
    for key in SIDECAR_EXTRA_KEYS:
        if key in data:
            extras[key] = data[key]
    vers = data.get("verifications")
    if vers is None:
        warnings.append("sidecar missing top-level `verifications:` key")
        return [], warnings, extras
    if not isinstance(vers, list):
        warnings.append("sidecar `verifications` must be a list")
        return [], warnings, extras
    # Validate each entry has at minimum {claim, source}
    cleaned = []
    for i, v in enumerate(vers):
        if not isinstance(v, dict):
            warnings.append(f"sidecar verifications[{i}] must be a mapping")
            continue
        claim = v.get("claim")
        source = v.get("source")
        if not isinstance(claim, str) or not claim.strip():
            warnings.append(f"sidecar verifications[{i}] missing or empty `claim`")
            continue
        if not isinstance(source, dict):
            warnings.append(f"sidecar verifications[{i}] missing `source` mapping")
            cleaned.append({"claim": claim, "source": None, "raw": v})
            continue
        stype = source.get("type")
        sref = source.get("ref")
        sevidence = source.get("evidence", "")
        if stype not in SOURCE_TYPES:
            warnings.append(
                f"sidecar verifications[{i}] source.type='{stype}' invalid; must be one of {sorted(SOURCE_TYPES)}"
            )
            cleaned.append({"claim": claim, "source": None, "raw": v})
            continue
        if not isinstance(sref, str) or not sref.strip():
            warnings.append(f"sidecar verifications[{i}] source.ref must be a non-empty string")
            cleaned.append({"claim": claim, "source": None, "raw": v})
            continue
        # URL sources require non-empty evidence in v1 (per D3)
        if stype == "url" and not (isinstance(sevidence, str) and sevidence.strip()):
            warnings.append(
                f"sidecar verifications[{i}] source.type='url' requires non-empty `evidence` (URL auto-fetch deferred to v2)"
            )
            cleaned.append({"claim": claim, "source": None, "raw": v})
            continue
        cleaned.append(
            {
                "claim": claim,
                "source": {"type": stype, "ref": sref, "evidence": sevidence},
                "raw": v,
            }
        )
    return cleaned, warnings, extras


# ---------------------------------------------------------------------------
# Detection pass
# ---------------------------------------------------------------------------


def detect_claims(text: str) -> list[Claim]:
    """Detect attributive claims via regex. Dedup by normalized claim text."""
    claims: list[Claim] = []
    seen_norm: set[str] = set()
    for detector, regex in REGEXES:
        for m in regex.finditer(text):
            span = m.span()
            try:
                claim_text = m.group(1).strip()
            except IndexError:
                claim_text = m.group(0).strip()
            # Filter parenthetical false-positives
            if detector == "paren_attribution":
                if any(neg in claim_text.lower() for neg in PAREN_NEGATIVES):
                    continue
            # Dedup by normalized text — same factual claim caught by multiple
            # detectors should only appear once
            norm = _normalize_for_match(claim_text)
            if not norm or norm in seen_norm:
                continue
            # Suppress "inline_attribution" matches whose phrase is wholly
            # contained inside a previously-seen paren_attribution claim
            # (paren wins because it's more specific)
            if detector == "inline_attribution":
                if any(norm in seen for seen in seen_norm):
                    continue
            seen_norm.add(norm)
            claims.append(
                Claim(
                    text=claim_text,
                    detector=detector,
                    location=f"offset {span[0]}:{span[1]}",
                )
            )
    return claims


# ---------------------------------------------------------------------------
# Claim ↔ sidecar matching
# ---------------------------------------------------------------------------


def _normalize_for_match(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _jaro_winkler(s1: str, s2: str) -> float:
    """Lightweight Jaro-Winkler implementation (no external dep)."""
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    match_distance = max(len(s1), len(s2)) // 2 - 1
    if match_distance < 0:
        match_distance = 0
    s1_matches = [False] * len(s1)
    s2_matches = [False] * len(s2)
    matches = 0
    transpositions = 0
    for i in range(len(s1)):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len(s2))
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = 0
    for i in range(len(s1)):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    transpositions //= 2
    jaro = (
        matches / len(s1)
        + matches / len(s2)
        + (matches - transpositions) / matches
    ) / 3
    # Winkler prefix bonus
    prefix = 0
    for a, b in zip(s1[:4], s2[:4]):
        if a == b:
            prefix += 1
        else:
            break
    return jaro + prefix * 0.1 * (1 - jaro)


def _claim_matches_verification(claim_text: str, ver_claim: str, threshold: float = 0.85) -> bool:
    a = _normalize_for_match(claim_text)
    b = _normalize_for_match(ver_claim)
    if not a or not b:
        return False
    # Exact substring after normalization → trivial match
    if a in b or b in a:
        return True
    return _jaro_winkler(a, b) >= threshold


# ---------------------------------------------------------------------------
# Shape checks — is this the right SHAPE of message for this room?
# ---------------------------------------------------------------------------
#
# The claim pass asks "is every fact sourced?". It passed all four messages sent to one
# project group in a single evening: 1,341 words across four memos, in which the venue for
# the decision being requested was never named once and the ask was never actually made.
# A recipient replied the next morning that they had a hard time following large
# AI-generated updates. Every fact in them was sourced. The defect was shape, and no
# amount of sourcing would have caught it.
#
# So these rules are about form, not truth: they are decidable from the draft text, the
# channel and the recipient list alone — countable or regexable, no judgment — which is the
# promotion bar for an L2 mechanical check in docs/comms-profiles-convention.md. They are
# UNIVERSAL: they would have blocked those messages to any group, with no profile loaded.
#
# They only run when --audience is passed. No flag → no shape checks → exactly the old
# behavior, so every existing caller is unaffected.

SHAPE_DEFAULTS = {
    "group_max_words": 200,      # longer than this is a memo, not a message
    "group_max_words_fyi": 120,  # with no ask at all it has even less business being long
    "dm_warn_words": 350,
    "telegram_hard_chars": 4096,  # Telegram rejects atomically past this (sends AND edits)
}

# Terms that read as fluent English to the author and as noise to the room. Warn only:
# some of these are legitimate in a dev thread, and a block would train people to override.
JARGON_DEFAULT = [
    "ARI", "silhouette", "sidecar", "registry", "backbone", "dyad", "systemd", "nginx", "PR #",
]

COMM_TYPES = ("update", "ask", "correction", "share")

# `*ital*` renders as a literal asterisk in Telethon/Signal — italics are `__underscores__`.
# Shipped 9 times in msg 352 (2026-07-18) and recurred on 2026-07-20 after being "a known lesson",
# which is exactly the L0-prose-lesson failure mode this class exists to end.
ASTERISK_ITALIC_RX = re.compile(r"(?<!\*)\*[^*\s][^*\n]*\*(?!\*)")

URL_RX = re.compile(r"(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)

# A path is only a leak OUTSIDE a URL — links are the fix, not the failure. URLs are stripped
# before scanning, so `https://github.com/o/r/blob/main/docs/design/x.md` is fine and a bare
# `docs/design/x.md` is not: the recipient cannot open the second one.
_PATH_BOUNDARY = r"(?:^|(?<=[\s(\[<`\"']))"
REPO_PATH_PATTERNS = [
    ("extension", re.compile(_PATH_BOUNDARY + r"[\w~][\w./-]*\.(?:md|py|ts|tsx|json|yaml|sh)\b", re.MULTILINE)),
    ("repo_dir", re.compile(_PATH_BOUNDARY + r"(?:~/|\.{0,2}/)?(?:docs|comms|scripts)/[\w./-]+", re.MULTILINE)),
    ("home_dir", re.compile(_PATH_BOUNDARY + r"~/[\w./-]+", re.MULTILINE)),
]

RESOLVE_PROFILE = Path(__file__).resolve().parent.parent / "comms-profiles" / "resolve_profile.py"


def _config_path() -> Path:
    """Engine config path. VERIFY_CONFIG_PATH overrides (tests; never write the real file)."""
    override = os.environ.get("VERIFY_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "local" / "verify-config.toml"


def _read_config() -> dict:
    cfg_path = _config_path()
    if not cfg_path.exists():
        return {}
    try:
        import tomllib
        with cfg_path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def shape_config() -> dict:
    """Numeric budgets + jargon list: code defaults, overridden by [shape] in the config."""
    cfg = dict(SHAPE_DEFAULTS)
    cfg["jargon"] = list(JARGON_DEFAULT)
    section = (_read_config().get("shape") or {})
    for key in SHAPE_DEFAULTS:
        if isinstance(section.get(key), int):
            cfg[key] = section[key]
    if isinstance(section.get("jargon"), list):
        cfg["jargon"] = [str(t) for t in section["jargon"]]
    return cfg


def _word_count(text: str) -> int:
    return len(text.split())


def _head(text: str, chars: int = 280) -> str:
    """The part a recipient sees before deciding whether to keep reading on a phone."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[:2]) + "\n" + text[:chars]


def _text_present(needle: str, haystack: str, threshold: float = 0.85) -> bool:
    """Fuzzy containment, same normalization + similarity the claim pass uses."""
    a = _normalize_for_match(needle)
    b = _normalize_for_match(haystack)
    if not a or not b:
        return False
    if a in b:
        return True
    n = len(a)
    if len(b) < n // 2:
        return False
    step = max(1, n // 8)
    for i in range(0, max(1, len(b) - n + 1), step):
        if _jaro_winkler(a, b[i:i + n]) >= threshold:
            return True
    return False


def parse_ask(extras: dict) -> tuple[bool, str, Optional[str]]:
    """Return (declared, kind, text) for the sidecar `ask:` key.

    kind is one of: missing | none | text | invalid. `ask: none` is the conscious
    "this is an FYI" assertion, mirroring `verifications: []` — the point is that
    silence never satisfies the rule, only a deliberate statement does.
    """
    if "ask" not in extras:
        return False, "missing", None
    raw = extras["ask"]
    if isinstance(raw, str):
        if raw.strip().lower() == "none":
            return True, "none", None
        if raw.strip():
            return True, "text", raw.strip()
        return True, "invalid", None
    if isinstance(raw, dict):
        text = raw.get("text")
        if isinstance(text, str) and text.strip():
            return True, "text", text.strip()
        return True, "invalid", None
    return True, "invalid", None


def _repo_path_leaks(text: str) -> list[str]:
    stripped = URL_RX.sub(" ", text)
    hits: list[str] = []
    for _name, rx in REPO_PATH_PATTERNS:
        for m in rx.finditer(stripped):
            tok = m.group(0).rstrip(".,;:)")
            if tok and tok not in hits:
                hits.append(tok)
    return hits


def _jargon_regex(term: str):
    t = term.strip()
    if t.endswith("#"):
        prefix = t[:-1].strip()
        return re.compile(
            r"\b" + re.escape(prefix) + r"\s*#\s*\d+",
            0 if prefix.isupper() else re.IGNORECASE,
        )
    return re.compile(r"\b" + re.escape(t) + r"\b", 0 if t.isupper() else re.IGNORECASE)


def _jargon_hits(text: str, terms: list) -> list[str]:
    hits: list[str] = []
    for term in terms:
        if _jargon_regex(term).search(text) and term not in hits:
            hits.append(term)
    return hits


def _run_resolve_profile(chat_id: str, comm_type: Optional[str], channel: Optional[str]):
    """Return (payload, human_command) or (None, human_command) when it can't be resolved."""
    human = [str(RESOLVE_PROFILE), "--chat-id", chat_id]
    if comm_type:
        human += ["--type", comm_type]
    if channel:
        human += ["--channel", channel]
    if not RESOLVE_PROFILE.exists():
        return None, human
    try:
        proc = subprocess.run(
            [sys.executable] + human + ["--json"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None, human
    if proc.returncode != 0:
        return None, human
    try:
        return json.loads(proc.stdout), human
    except json.JSONDecodeError:
        return None, human


def _check_profile_hash(recipients: list, extras: dict, channel: Optional[str]) -> Optional[str]:
    """S10: a group send into a room with a Groups note must carry that room's current hash.

    Staleness IS a mismatch: editing any profile changes the hash, so a brief generated
    before the edit no longer describes the room the message is going to. Returns an
    error string on failure, None when it passes or when there is nothing to check.
    """
    aud = extras.get("audience") if isinstance(extras.get("audience"), dict) else {}
    comm_type = aud.get("type")
    if comm_type not in COMM_TYPES:
        comm_type = None
    for chat_id in recipients:
        payload, human = _run_resolve_profile(chat_id, comm_type, channel)
        if not payload:
            continue
        group = payload.get("group") or {}
        if not group.get("note"):
            continue  # no Groups/*.md note carries this chat id → nothing to enforce
        want = payload.get("profile_hash")
        have = aud.get("profile_hash")
        if have == want:
            return None
        cmd = " ".join(human)
        if not have:
            return (
                f"group send to {group.get('name') or chat_id} but the sidecar carries no "
                f"`audience.profile_hash`. Run:\n    {cmd}\n"
                "  then paste the emitted `audience:` block into the sidecar and draft FROM the brief."
            )
        return (
            f"stale `audience.profile_hash` for {group.get('name') or chat_id}: sidecar has "
            f"{have!r}, current is {want!r} — a profile changed since the brief was generated. Run:\n"
            f"    {cmd}\n  re-read the brief, and paste the new `audience:` block."
        )
    return None


def evaluate_shape(
    text: str,
    *,
    audience: Optional[str],
    channel: Optional[str],
    extras: dict,
    recipients: Optional[list] = None,
    cfg: Optional[dict] = None,
) -> list[dict]:
    """Return [{rule, severity, detail}]. severity: block → exit 2; warn → recorded + printed."""
    cfg = cfg or shape_config()
    findings: list[dict] = []

    def add(rule: str, severity: str, detail: str) -> None:
        findings.append({"rule": rule, "severity": severity, "detail": detail})

    is_group = audience == "group"
    is_dm = audience == "dm"
    words = _word_count(text)
    declared, kind, ask_text = parse_ask(extras)

    # S1 — a group message must state what it is for.
    if is_group and kind in ("missing", "invalid"):
        detail = (
            "sidecar declares no `ask:`. Add either `ask: {text: \"<the request, verbatim from "
            "the draft>\"}` or the literal `ask: none` to assert consciously that this is an FYI."
        )
        if kind == "invalid":
            detail = "sidecar `ask:` is present but empty/malformed; use `ask: {text: \"...\"}` or `ask: none`."
        add("S1", "block", detail)

    # S2 — and it must state it where the recipient will actually see it.
    if is_group and kind == "text" and ask_text:
        if not _text_present(ask_text, _head(text)):
            add(
                "S2", "block",
                f"the declared ask ({ask_text[:80]!r}) is not in the first 2 lines or first 280 "
                "chars of the draft. On a phone that is the whole message; move it to the top.",
            )

    # S3 — length.
    if is_group:
        cap = cfg["group_max_words_fyi"] if kind == "none" else cfg["group_max_words"]
        if words > cap:
            why = " (no ask declared, so the cap is tighter)" if kind == "none" else ""
            add(
                "S3", "block",
                f"{words} words to a group, cap is {cap}{why}. Cut it, or move the detail to a "
                "linked doc and send the short version.",
            )
    if is_dm and words > cfg["dm_warn_words"]:
        add("S4", "warn", f"{words} words in a DM (soft cap {cfg['dm_warn_words']}).")

    # S5 — em-dash. Email is exempt: it is a memo-tolerant genre.
    if (is_group or is_dm) and channel != "email":
        n = text.count("—")
        if n:
            add("S5", "block", f"{n} em-dash(es) (U+2014). It has to read like a person wrote it.")

    # S6/S7 — channel mechanics.
    if channel in ("telegram", "signal"):
        hits = ASTERISK_ITALIC_RX.findall(text)
        if hits:
            add(
                "S6", "block",
                f"{len(hits)} single-asterisk italic(s) — these render as literal asterisks. "
                "Use __underscores__ for italics (** for bold is fine).",
            )
    if channel == "telegram" and len(text) > cfg["telegram_hard_chars"]:
        add(
            "S7", "block",
            f"{len(text)} chars; Telegram rejects past {cfg['telegram_hard_chars']} and the "
            "rejection is atomic (an edit fails the same way).",
        )

    # S8 — a path the recipient cannot open.
    if is_group or is_dm:
        leaks = _repo_path_leaks(text)
        if leaks:
            add(
                "S8", "block" if is_group else "warn",
                "repo/file path(s) the recipient cannot open: "
                + ", ".join(repr(x) for x in leaks[:6])
                + ". Use a full clickable URL or carry the substance inline.",
            )

    # S9 — jargon.
    if is_group:
        jargon = _jargon_hits(text, cfg["jargon"])
        if jargon:
            add(
                "S9", "warn",
                "terms only you may parse: " + ", ".join(jargon) + ". Say them in the room's words.",
            )

    # S10 — the room's profile must be the one the draft was written from.
    if is_group and recipients:
        problem = _check_profile_hash(recipients, extras, channel)
        if problem:
            add("S10", "block", problem)

    return findings


# ---------------------------------------------------------------------------
# Layer 3: adversarial reviewer (via `claude -p --bare`)
# ---------------------------------------------------------------------------

REVIEWER_SYSTEM_PROMPT = """\
You are an adversarial fact-checker with NO PRIOR CONTEXT. You have only:
  (1) a draft message about to be sent to a third party
  (2) a list of factual claims the author identified, each with a cited source
  (3) the actual content of each cited source (for `file` and `quote` types) or
      the author's evidence summary (for `url`, `message_id`, `conversation_ref`)

Your job: for each claim, verify whether the cited source ACTUALLY SUPPORTS the claim.
Flag any mismatch where:
  - the source does NOT support the claim
  - the claim adds detail not present in the source
  - the claim contradicts the source
  - the source is too vague to confirm the claim

You must respond with a SINGLE JSON object matching this schema EXACTLY:
  {"verdict": "pass" | "fail", "mismatches": [{"claim": "...", "source_ref": "...", "mismatch_reason": "..."}]}

`verdict: "pass"` means every claim is supported by its source.
`verdict: "fail"` means one or more claims have unsupported / contradicted / over-reaching content.
`mismatches` is empty on pass, populated on fail.

Output ONLY the JSON object. No prose, no markdown, no commentary."""


def _build_reviewer_prompt(draft_text: str, verifications: list[dict], source_base_dir: Path) -> str:
    """Bundle draft + verifications + source contents into one prompt string."""
    parts = []
    parts.append("=== DRAFT ABOUT TO BE SENT ===\n")
    parts.append(draft_text)
    parts.append("\n=== CLAIMS + CITED SOURCES ===\n")
    for i, v in enumerate(verifications):
        src = v.get("source") or {}
        parts.append(f"\n--- Claim {i+1} ---")
        parts.append(f"Claim: {v.get('claim', '')}")
        parts.append(f"Source type: {src.get('type', '?')}")
        parts.append(f"Source ref: {src.get('ref', '')}")
        if src.get("evidence"):
            parts.append(f"Author's evidence summary: {src['evidence']}")
        # For file sources, load + inline (truncated to keep prompt small)
        if src.get("type") == "file":
            ref = src.get("ref", "")
            file_path = Path(ref).expanduser()
            if not file_path.is_absolute():
                file_path = source_base_dir / ref
            try:
                # Read as bytes first; detect binary (null bytes or non-utf8)
                raw = file_path.read_bytes()
                if b"\x00" in raw[:8192]:
                    parts.append(
                        f"[Source is a binary file: {file_path} ({len(raw)} bytes). "
                        f"Not readable as text — rely on the author's evidence summary above to verify the claim.]"
                    )
                else:
                    try:
                        content = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        parts.append(
                            f"[Source is not utf-8 text: {file_path} ({len(raw)} bytes). "
                            f"Rely on author's evidence summary.]"
                        )
                        continue
                    # Strip any stray control chars (defense in depth)
                    content = content.replace("\x00", "")
                    if len(content) > 4000:
                        content = content[:4000] + f"\n[... truncated, total length {len(content)} chars]"
                    parts.append(f"Source content (read from {file_path}):\n```\n{content}\n```")
            except OSError as e:
                parts.append(f"[Source file unreadable: {e}]")
        elif src.get("type") == "quote":
            parts.append(f"Quoted content: {src.get('ref', '')}")
        elif src.get("type") == "url":
            parts.append("[URL not auto-fetched in v1 — evaluate against author's evidence summary above]")
    parts.append("\n=== END ===\n")
    parts.append("Now produce the JSON verdict. ONLY the JSON object, nothing else.")
    return "\n".join(parts)


def run_reviewer(
    draft_text: str,
    verifications: list[dict],
    source_base_dir: Path,
    model: str = "claude-sonnet-4-6",
    timeout: int = 120,
) -> dict:
    """Invoke `claude -p --bare` as adversarial reviewer. Returns reviewer JSON
    (`{verdict, mismatches}`) or `{verdict: "skipped", reason: "..."}` on error."""
    if not verifications:
        return {"verdict": "pass", "mismatches": [], "note": "no claims to review"}

    prompt = _build_reviewer_prompt(draft_text, verifications, source_base_dir)
    # Defense-in-depth: strip null bytes (subprocess argv cannot contain them).
    # Should be unnecessary after the per-source binary-detection, but cheap.
    prompt = prompt.replace("\x00", "")
    # Note: NOT using --bare because it requires ANTHROPIC_API_KEY in env (skips
    # OAuth/keychain auth). The adversarial isolation comes from the system prompt
    # + fresh subprocess context, not from --bare. We do pass --disable-slash-commands
    # to prevent the subagent from triggering skills mid-review.
    cmd = [
        "claude", "-p",
        "--disable-slash-commands",
        "--model", model,
        "--append-system-prompt", REVIEWER_SYSTEM_PROMPT,
        "--output-format", "json",
        prompt,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"verdict": "skipped", "reason": f"reviewer subprocess timed out after {timeout}s"}
    except FileNotFoundError:
        return {"verdict": "skipped", "reason": "claude CLI not found on PATH"}
    if proc.returncode != 0:
        return {"verdict": "skipped", "reason": f"claude -p exited {proc.returncode}: {proc.stderr[:200]}"}

    raw = proc.stdout.strip()
    # claude -p --output-format json wraps the assistant message in an envelope.
    # The assistant text may also be wrapped in a ```json ... ``` fence.
    try:
        envelope = json.loads(raw)
        message = envelope.get("result") if isinstance(envelope, dict) and "result" in envelope else envelope
        if isinstance(message, str):
            # Strip code-fence wrapping if present
            stripped = message.strip()
            if stripped.startswith("```"):
                # Drop opening fence (```json, ```JSON, ```, etc.)
                stripped = re.sub(r"^```\w*\s*\n?", "", stripped)
                # Drop closing fence
                stripped = re.sub(r"\n?```\s*$", "", stripped)
            verdict_json = json.loads(stripped.strip())
        else:
            verdict_json = message
    except (json.JSONDecodeError, AttributeError) as e:
        return {"verdict": "skipped", "reason": f"could not parse reviewer output: {e}; raw={raw[:200]}"}

    # Validate shape
    if not isinstance(verdict_json, dict) or "verdict" not in verdict_json:
        return {"verdict": "skipped", "reason": f"reviewer returned malformed JSON: {verdict_json}"}
    return verdict_json


# An `internal_tooling` hit is only excusable if the sidecar asserts that the RECIPIENT
# can actually reach the thing. A sidecar entry merely documenting that the artifact
# exists is NOT sufficient — that is precisely how "it's in the vault record — worth a
# read" shipped to a project team alongside a sidecar entry describing the internal
# appendix. Existence was sourced; reachability was never the question asked.
ACCESS_AFFIRMATION = re.compile(
    r"recipient\s+(?:has|have)\s+access"
    r"|(?:has|have|was|were)\s+been?\s*(?:already\s+)?shared\s+with"
    r"|shared\s+with\s+(?:them|the\s+\w+)"
    r"|they\s+can\s+(?:open|access|reach|see|read)"
    r"|is\s+a\s+collaborator"
    r"|recipient[-\s]accessible",
    re.IGNORECASE,
)


def _access_asserted(source: dict) -> bool:
    """True if the sidecar source explicitly asserts the recipient can reach the artifact."""
    blob = " ".join(
        str(source.get(k, "")) for k in ("evidence", "ref", "type")
    )
    return bool(ACCESS_AFFIRMATION.search(blob))


def crossref(claims: list[Claim], verifications: list[dict]) -> tuple[list[Claim], list[Claim]]:
    """Mark each claim source_present + return (all_claims, unverified)."""
    unverified: list[Claim] = []
    for claim in claims:
        matched = False
        for v in verifications:
            if _claim_matches_verification(claim.text, v["claim"]):
                if v.get("source"):
                    # internal_tooling needs an access assertion, not just a source.
                    if claim.detector == "internal_tooling" and not _access_asserted(v["source"]):
                        matched = False
                        break
                    claim.source_present = True
                    claim.source_value = v["source"]
                    matched = True
                    break
                else:
                    # Matched a claim entry but source was malformed
                    matched = False  # still treat as unverified
                    break
        if not matched:
            unverified.append(claim)
    return claims, unverified


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_sidecar_path(draft_path: Path) -> Path:
    return draft_path.with_name(draft_path.name + ".verifications.yaml")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="verify_draft",
        description="Gate external-facing drafts against unverified factual claims.",
    )
    p.add_argument("--draft", required=True, help="Path to the draft file (markdown/text body to send)")
    p.add_argument(
        "--verifications",
        help="Path to YAML sidecar. Default: <draft>.verifications.yaml",
    )
    p.add_argument(
        "--mode",
        choices=["strict", "warn", "off"],
        default="strict",
        help="strict (default): unverified claims block send; warn: log only; off: skip entirely",
    )
    p.add_argument(
        "--reviewer",
        choices=["on", "off"],
        default="off",
        help="Layer 3 adversarial reviewer (deferred; currently off-only in v1.0)",
    )
    p.add_argument(
        "--source-base-dir",
        help="Base directory for resolving relative `file` sources (default: dirname of draft)",
    )
    p.add_argument(
        "--audience",
        choices=["group", "dm", "email"],
        default=None,
        help="Who this is going to. Omit for no shape checks (exactly the pre-Phase-2 behavior).",
    )
    p.add_argument(
        "--channel",
        choices=["telegram", "signal", "slack", "email"],
        default=None,
        help="Transport, for channel-specific shape rules (asterisk italics, hard char cap).",
    )
    p.add_argument(
        "--shape",
        choices=["on", "off"],
        default="on",
        help="Shape checks (default on when --audience is given). The send scripts only pass "
             "'off' behind a fresh operator timestamp — an agent cannot disable these itself.",
    )
    p.add_argument(
        "--recipients",
        default=None,
        help="Comma-separated platform-prefixed ids (telegram:123, slack:C01, signal:...). "
             "Enables S10 (the group-profile hash check).",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON report to stdout (default: pretty)")
    args = p.parse_args(argv)

    draft_path = Path(args.draft).expanduser().resolve()
    if not draft_path.exists():
        print(json.dumps({"error": f"draft not found: {draft_path}"}, indent=2))
        return 3

    sidecar_path = (
        Path(args.verifications).expanduser().resolve()
        if args.verifications
        else _default_sidecar_path(draft_path)
    )

    text = draft_path.read_text()

    # Mode: off → exit 0 immediately (gate skipped)
    if args.mode == "off":
        report = Report(passed=True, mode="off", draft_path=str(draft_path), sidecar_path=str(sidecar_path))
        report.warnings.append("mode=off; gate skipped")
        print(json.dumps(asdict(report), indent=2))
        return 0

    verifications, sidecar_warnings, sidecar_extras = load_sidecar(sidecar_path)

    report = Report(
        passed=False,
        mode=args.mode,
        draft_path=str(draft_path),
        sidecar_path=str(sidecar_path) if sidecar_path else None,
    )
    report.warnings.extend(sidecar_warnings)

    # HARD BLOCK: an unfinished draft. Not a claim, so no sidecar can satisfy it, and it blocks
    # in warn mode too — sending «AMOUNT» to a client is wrong regardless of gate mode.
    placeholders = detect_placeholders(text)
    if placeholders:
        report.placeholders = placeholders
        report.passed = False
        report.warnings.append(
            f"BLOCKED: {len(placeholders)} placeholder(s) — the draft is unfinished. "
            "Placeholders cannot be satisfied by a sidecar entry; fill them in and re-run."
        )
        print(json.dumps(asdict(report), indent=2))
        return 2

    # SHAPE CHECKS: form, not truth. Like placeholders, a shape block is mechanical — no
    # sidecar entry can satisfy "this is 350 words to a group with no ask in it". Runs only
    # when --audience is supplied, so callers that don't pass it are byte-for-byte unaffected.
    if args.audience and args.shape == "on":
        recipients = [r.strip() for r in (args.recipients or "").split(",") if r.strip()]
        findings = evaluate_shape(
            text,
            audience=args.audience,
            channel=args.channel,
            extras=sidecar_extras,
            recipients=recipients,
        )
        report.shape = findings
        blocks = [f for f in findings if f["severity"] == "block"]
        for f in findings:
            if f["severity"] == "warn":
                report.warnings.append(f"shape {f['rule']} (warn): {f['detail']}")
        if blocks:
            report.passed = False
            report.warnings.append(
                "BLOCKED: "
                + ", ".join(f["rule"] for f in blocks)
                + f" — wrong shape for audience={args.audience}"
                + (f"/channel={args.channel}" if args.channel else "")
                + ". Shape defects cannot be sourced away; fix the draft."
            )
            print(json.dumps(asdict(report), indent=2))
            return 2

    # Detect claims
    claims = detect_claims(text)

    # Handle missing sidecar (per D2)
    if verifications is None:
        # No sidecar at all
        if claims:
            report.warnings.append(
                f"no sidecar at {sidecar_path}; {len(claims)} claim(s) detected but cannot verify"
            )
            report.claims = claims
            report.unverified = claims
            if args.mode == "strict":
                report.passed = False
                print(json.dumps(asdict(report), indent=2))
                return 2
            else:  # warn
                report.passed = True
                print(json.dumps(asdict(report), indent=2))
                return 0
        else:
            # No claims, no sidecar — per D2, strict mode REQUIRES explicit empty sidecar
            report.warnings.append(
                f"no sidecar at {sidecar_path}; no claims detected, but strict mode requires explicit `verifications: []` sidecar"
            )
            if args.mode == "strict":
                report.passed = False
                print(json.dumps(asdict(report), indent=2))
                return 2
            else:
                report.passed = True
                print(json.dumps(asdict(report), indent=2))
                return 0

    # Sidecar present (possibly empty). Crossref.
    all_claims, unverified = crossref(claims, verifications)
    report.claims = all_claims
    report.unverified = unverified

    # Determine Layer 3 mode from config (default warn per D9) unless overridden
    layer3_mode = _read_layer3_mode_from_config()  # "warn" | "strict" | "off"
    if args.reviewer == "off":
        layer3_mode = "off"

    # Run Layer 3 reviewer if enabled
    if args.reviewer == "on" and layer3_mode != "off":
        source_base = (
            Path(args.source_base_dir).expanduser().resolve()
            if args.source_base_dir
            else draft_path.parent
        )
        reviewer_result = run_reviewer(text, verifications, source_base)
        report.reviewer = reviewer_result
    else:
        report.reviewer = {"verdict": "skipped", "reason": f"reviewer={args.reviewer}, layer3_mode={layer3_mode}"}

    # Decide pass/fail based on Layer 2 + Layer 3
    layer2_blocking = bool(unverified) and args.mode == "strict"
    layer3_blocking = (
        report.reviewer.get("verdict") == "fail"
        and layer3_mode == "strict"
        and args.mode != "off"
    )

    if layer2_blocking or layer3_blocking:
        report.passed = False
        print(json.dumps(asdict(report), indent=2))
        return 2

    report.passed = True
    print(json.dumps(asdict(report), indent=2))
    return 0


def _read_layer3_mode_from_config() -> str:
    """Read [layer3] mode from the engine config (see _config_path); default 'warn'."""
    return (_read_config().get("layer3", {}) or {}).get("mode", "warn")


if __name__ == "__main__":
    sys.exit(main())

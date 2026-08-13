# verify-draft

A send-time gate for messages drafted with AI assistance.

It reads a draft and a sidecar file listing every factual claim the draft makes
and where each one came from. If a claim has no source, the send is refused.

```
$ verify_draft.py --draft note.md
{"passed": false, "unverified": [{"text": "(taken from VLC)", "detector": "paren_attribution"}]}
$ echo $?
2
```

## Why it exists

Fluent text generation invents confident-sounding factual asides from nearby
context. The failure is rarely the main argument — it's a parenthetical. A
message says something was *"(taken from X)"* because X was mentioned earlier in
the conversation, and the aside is wrong, and nobody notices because everything
around it is correct.

Prompting yourself to be careful does not fix this. The aside feels like
recall, not invention. So the check has to be mechanical and it has to sit at
the point of transmission, where the cost of being wrong is highest.

## How it works

**Layer 1 — placeholders.** Any `[TBD]`, `TODO:`, `{{mustache}}`, `<INSERT ...>`
or `___` means the draft is unfinished. Full stop, no argument.

**Layer 2 — claim detection.** Regex detectors find attributive claims:
parenthetical attributions, attributive verbs after proper nouns, ISO dates,
version strings, prices, and references to internal tooling. Each detected span
must match an entry in the sidecar that carries a populated `source` block.

**Layer 3 — shape.** Form rules that no amount of sourcing can satisfy, scaled
to audience and channel: length caps, whether the ask appears in the first
screenful, em-dash density, and repo or file paths the recipient cannot open.
A shape failure short-circuits before claim detection, on the reasoning that
you fix the form before arguing about sources.

## The sidecar contract

A draft at `note.md` expects `note.md.verifications.yaml`:

```yaml
verifications:
  - claim: "the suite reported 214 tests with zero failures"
    location: "paragraph 2"          # optional
    source:
      type: file                     # file | url | quote | message_id | conversation_ref
      ref: "build/reports/tests/index.html"
      evidence: "Verbatim: 'Test summary: 214 tests, 0 failures, 3 skipped'."
```

Three rules earn their keep:

- **`evidence` is required on `type: url`.** URLs are not fetched. The author
  must open the page and summarise why it supports the claim. Without this, a
  sidecar becomes a list of links nobody read.
- **A draft with no factual claims still needs `verifications: []`.** An
  explicit assertion that there is nothing to check, rather than a missing file
  that could equally mean the author forgot.
- **Naming an internal tool requires asserting the *recipient* can reach it.**
  Documenting that an artifact exists is not the same as the reader being able
  to open it. The rule exists because "it's in the vault record — worth a read"
  once shipped to people with no vault.

## Install

```
pip install pyyaml
python3 verify_draft.py --draft path/to/draft.md
```

Exit codes: `0` pass · `2` unverified claims or shape failure · `3` malformed input.

Useful flags: `--audience group|dm` and `--channel telegram|slack|email` turn on
Layer 3; `--shape off` skips it; `--verify warn` reports without blocking.

## Wiring it into a send path

The gate is only load-bearing if it sits *inside* the thing that transmits.
Wire it into the send script, not into a prompt or a checklist:

```python
report = subprocess.run([GATE, "--draft", draft_path], capture_output=True)
if report.returncode != 0:
    sys.exit("blocked by verify-draft")
```

A convention that lives in documentation gets skipped under load, and the skip
is silent. This was true here too: the gate was described as enforced for two
months before anyone checked, and `git log -S` found it wired into nothing.

## Tests

```
python3 -m pytest tests/ -q
```

70 tests. Fixtures are synthetic; each isolates one rule, and
`BASELINE_EXITS` in `tests/test_verify_draft.py` pins the expected exit code
for every fixture on disk so a new fixture cannot be added without declaring
what it should do.

## What this is not

Not a fact-checker. It never evaluates whether a source *supports* a claim —
only whether the author supplied one. It cannot catch a confidently wrong
citation. What it catches is the claim nobody thought to source, which
empirically is where the errors live.

## Licence

MIT.

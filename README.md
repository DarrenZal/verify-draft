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

## The audience layer

The gate says *don't assert what you can't source*. It does not say *sound like
yourself to this particular person*. That second job belongs to
`comms_profiles/`.

`resolve_profile.py` takes a chat id or a list of people and composes an
**audience brief**: your own voice card, the numeric limits, one block per
recipient drawn from their profile, the group's norms, and the rules for this
kind of message. Two properties make it more than a preferences file:

- **Conflicts are surfaced, not resolved.** If one recipient wants brevity and
  another wants failure modes spelled out, the brief says so and leaves the
  judgement to you. Silently averaging them produces a message that satisfies
  neither.
- **Guesses are labelled.** A rule mined from behaviour renders as
  `[inferred — a guess, do not treat as their stated preference]` until someone
  explicitly confirms it. `profile_update.py confirm` is the only promotion path.

It also emits a `profile_hash` over every resolved rule, so editing a profile
stales any brief generated before the change.

```
COMMS_CHANNELS_VAULT=./example-vault \
COMMS_HOUSE_RULES=./house-rules.example.yaml \
COMMS_SENDER_NAME=Sender \
  python3 comms_profiles/resolve_profile.py --chat-id telegram:100200300 --type update
```

`example-vault/` holds a runnable four-note example. Profiles are plain Markdown
with YAML frontmatter and live **outside** this repo — point
`COMMS_CHANNELS_VAULT` at your own directory. No person's preferences are
compiled into the code.

## The promotion ladder

Rules climb three tiers, and the criteria for climbing are the interesting part:

| Tier | What it is | How it's enforced |
|---|---|---|
| **L0** | A lesson written in prose | Loaded by luck, enforced by nothing |
| **L1** | A structured profile rule | Enters every audience brief |
| **L2** | A mechanical gate check | Blocks the send |

A rule earns L2 only when it is **decidable from the draft text alone**, has
**recurred twice or caused a real incident**, and has a **low false-positive
rate**. That last criterion matters: a check that cries wolf gets disabled, and
a disabled check is worse than no check because people believe it is running.

`house-rules.example.yaml` shows the schema, including the `mechanical:` field
that binds a rule to its gate check. **Its evidence strings are invented.** The
real ones quote named people verbatim and are not publishable, and a rewrite
that replaced them with plausible-sounding fiction would corrupt the one
invariant the schema exists to teach: every rule traces to a real event.

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

Flags:

| Flag | Values | Does |
|---|---|---|
| `--audience` | `group` `dm` `email` | turns the shape layer on, and scales its limits |
| `--channel` | `telegram` `signal` `slack` `email` | same, plus channel-specific caps |
| `--shape` | `on` `off` | skip the shape layer |
| `--mode` | `strict` `warn` `off` | whether unsourced claims **block** or just report |
| `--reviewer` | `on` `off` | run the adversarial reviewer (below); off by default |
| `--recipients` | comma list | enables S10 and the tier-zero check |

One naming trap worth knowing before you read the config: **this document numbers
shape as Layer 3, but the code reserves the name `layer3` for the adversarial
reviewer.** So `[layer3] mode` in the config file governs the reviewer, and
`[shape]` governs the shape rules. Different things, adjacent names.

Everything with a path is env-overridable, and nothing personal is compiled in:

| Variable | What it points at | Default |
|---|---|---|
| `COMMS_CHANNELS_VAULT` | your profile notes | `~/Documents/Notes` |
| `COMMS_HOUSE_RULES` | the global rule file | `house-rules.example.yaml` |
| `COMMS_SENDER_NAME` | your own voice-card note | `Sender` |
| `VERIFY_DRAFT_AUDIT_LOG` | one row per successful send | `~/.verify-draft/send-audit.jsonl` |
| `VERIFY_DRAFT_TIER_ZERO` | recipients needing explicit confirmation | `~/.verify-draft/tier-zero-recipients.txt` |

## Wiring it in

The gate is only load-bearing if it sits *inside* the thing that transmits.
A convention living in documentation gets skipped under load, and the skip is
silent. That was true here: this gate was described as enforced for two months
before anyone checked, and `git log -S` found it wired into nothing.

So there is exactly one place it belongs — **the send script**:

```python
# in send_slack.py / send_telegram.py / wherever the API call lives
if args.send:
    gate = subprocess.run(
        [GATE, "--draft", args.message_file,
         "--audience", audience, "--channel", "slack"],
        capture_output=True, text=True,
    )
    if gate.returncode != 0:
        sys.stderr.write(gate.stderr)
        sys.exit("blocked by verify-draft")
    transmit(...)
```

Two details that matter: require `--message-file` for a real send so a sidecar
can exist at all (an inline `--message` string has nowhere to carry sources),
and let dry-run bypass the gate entirely so drafting stays fast.

`examples/send_slack.py` is a real sender wired this way — the one that has been
gating live sends since 2026-07-31. It is included as evidence rather than as a
framework: copy the ~30 lines of gate wiring, expect to replace the rest. Note it
resolves the channel against the Slack API even to preview, so running it needs a
token; the gate wiring itself is readable without one.

### If you use Claude Code

Four mechanisms, and only one of them enforces anything. Be clear about which
is which, because it is easy to build the comfortable ones and skip the real one.

| Layer | What it does | Enforces? |
|---|---|---|
| **The send script** | runs the gate before transmitting | **yes — this is the whole thing** |
| **A skill** (`.claude/skills/…`) | tells the model how to author a sidecar, which detectors bite | no |
| **`CLAUDE.md`** | the standing rule that every draft needs a sidecar | no |
| **A hook** | blocks a tool call, and CAN inspect an MCP tool's arguments | **yes, for MCP sends** |

**When the send is an MCP tool rather than a shell command, the hook is the right
instrument.** A `PreToolUse` hook receives the full tool input, body included, so it
can find the matching draft on disk, run the gate, and refuse. That closes the hole
you otherwise get the moment a mail/chat MCP server is installed alongside your gated
send scripts: the model reaches for the tool, not the script, and nothing stops it.
Make the disable switch an environment variable rather than a tool argument, so the
model cannot turn off its own gate.

A minimal skill is enough — a `SKILL.md` describing the sidecar schema, the
detectors that fire most often (written dates, parenthetical attributions,
attributive verbs, internal tooling), and the instruction to write the sidecar
*before* offering to send. The model then arrives at the send with sources
already gathered rather than reverse-engineering them after being blocked.

In `CLAUDE.md`, one paragraph: every outbound draft is paired with
`<draft>.verifications.yaml`; the gate is strict by default; you cannot bypass
it yourself. That last clause matters — an override flag that the model can set
is not an override, it is a suggestion.

### The adversarial reviewer needs the CLI

Run it with `--reviewer on`. It shells out to `claude -p --bare` to ask whether
each cited source actually supports its claim — the one check that is semantic
rather than mechanical. It needs the `claude` binary on `PATH`, and it degrades
to `verdict: skipped` on timeout rather than blocking, so a send that looks hung
is usually the reviewer thinking.

Whether a `fail` verdict *blocks* is not a flag: it comes from `[layer3] mode`
in `~/.claude/local/verify-config.toml` (`VERIFY_CONFIG_PATH` overrides it),
defaulting to `warn`. Leave it on `warn` for a while before trusting it to block.

It earns its keep on the sidecar rather than the prose. The mechanical layers
only ask whether a `source` block is populated; the reviewer opens the ref. In
practice its most common catch is a row whose `ref` cannot be resolved at all —
two paths joined by "and", a stale path, a link nobody opened — which passes
every mechanical check while providing exactly no evidence.

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

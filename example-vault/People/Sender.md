---
name: Sender
communication:
  register: "plain sentences, short paragraphs, no throat-clearing openers"
  rules:
    - id: voice-1
      rule: "State the uncomfortable part rather than softening it."
      kind: voice
      confidence: confirmed
      source: operator
      evidence: "Own voice card, set deliberately."
---

# Sender

Your own voice card. `resolve_profile.py` always loads this note first, under
VOICE, so drafts read as you rather than in a generic register.

Point `COMMS_SENDER_NAME` at the `name:` value here.

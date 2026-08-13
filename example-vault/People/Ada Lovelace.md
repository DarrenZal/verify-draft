---
name: Ada Lovelace
aliases: [Ada]
communication:
  register: "warm but compressed; assumes technical context"
  rules:
    - id: ada-1
      rule: "Summary first, detail linked. Long inline context does not get read."
      kind: format
      confidence: confirmed
      source: recipient
      evidence: "2026-03-04, in thread: asked for the summary up top and the rest linked."
    - id: ada-2
      rule: "Prefers a direct ask over an implied one."
      kind: ask
      confidence: inferred
      source: mined
      evidence: "Three threads where an implied ask went unanswered and an explicit one did not."
---

# Ada Lovelace

Example person profile. `ada-2` is `inferred`, so the brief renders it flagged
as a guess. Only `profile_update.py confirm` promotes it to `confirmed`.

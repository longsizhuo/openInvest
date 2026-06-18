# Lookup strategy: grep first, find_docs.py as fallback

The single most important thing about Job 2: **`find_docs.py` is the fallback, not the
default.** Reach for grep first. Escalate to the script only when grep can't tell you which
doc is authoritative.

## The decision tree

```
Need to find the doc/answer for some term?
│
├─ 1. grep the literal term first  (rg "<term>" docs/)        ← always, zero script overhead
│
├─ 2. Did grep decide it?
│      • hits in exactly one file, OR
│      • hits in a heading / frontmatter (that doc OWNS the topic)
│      → read that doc. DONE.                                  ← the common, cheap path
│
└─ 3. Is grep ambiguous?
       • hits scattered across ≥3 files, OR
       • hits only in prose (no doc clearly owns it), OR
       • 0 hits because of a synonym ("rate limit" vs "throttling")
       → run.sh find "<term>"                                  ← ranks the owning doc first
```

## Why it's structured this way

The benchmark that motivated this skill:

| scenario | grep alone | find_docs.py |
|---|---|---|
| **literal keyword, one clear owner** | optimal | *worse* — adds one Bash call (~+4s). |
| **keyword scattered across big files** | 6 calls / ~29s (ls + multiple greps + read §3 to confirm) | 3 calls / ~18s (ranks the right doc first) |

So the win is **not** "the script is faster." On easy queries grep already wins, and the
script is pure overhead. The win is that on *ambiguous* queries the script cuts calls and
wall-clock roughly in half — and you only get that by **not** calling it on the easy ones.

Corollary: **don't run grep and `find` in parallel "to be safe."** That re-adds the overhead
you were trying to avoid on every query. It's conditional escalation, not two legs running
together.

## What the script actually does better than grep

grep matches **strings**; `find_docs.py` matches **intent**. It reads each doc's frontmatter
and ranks by *ownership*:

1. `schema_source` symbol exact match (the doc explains that code) — strongest
2. `documents.endpoints` (path match, method optional)
3. `documents.config_keys` (dotted key)
4. `documents.symbols`
5. `intent` / `tags`
6. substring over title + pointers — weakest

So a chapter tagged `intent: API Contract` ranks first for `GET /api/holdings` even if the
literal string is buried three sections deep — something grep's flat line-matching can't do.

## When token cost matters
This strategy saves **calls and wall-clock, not tokens** — as long as docs are monolithic,
the agent still reads the whole big file once it lands on it. The token win is a *separate*
lever: split a large doc into per-topic files (each with its own frontmatter) so a hit pulls
in only the small relevant file. That splitting is out of scope here; this skill just gets
you to the right file fast.

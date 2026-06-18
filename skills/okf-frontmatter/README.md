# okf-frontmatter

A skill for maintaining openInvest's docs under the
[Open Knowledge Format (OKF)](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/)
— and for finding the *authoritative* doc/schema fast, instead of grepping through
thousands of lines of prose.

openInvest's docs live in `docs/wiki/` (numbered chapters) and `docs/wiki/adr/`
(decision records). Two jobs:

1. **Maintain docs the OKF way.** Every doc carries a small YAML frontmatter block as
   the single source of truth (`type`, `title`, `tags`, `intent`, `schema_source`,
   `documents`). Schema detail *links to the code* (`schema_source: file.py:Symbol`)
   instead of being copied into prose — so docs stop drifting and stop ballooning into
   thousand-line markdown.
2. **Look docs up fast** (`find_docs.py`). Given a code symbol, an API endpoint, a
   config key, or a keyword, it ranks the doc that *owns* the topic by frontmatter
   intent, and can resolve a doc's `schema_source` straight to the code.

The lookup is deliberately **grep-first**: the script is the *fallback* for when grep is
ambiguous, not a replacement for it. See [`references/lookup-strategy.md`](references/lookup-strategy.md).

## Commands

```bash
./scripts/run.sh find <symbol|GET /api/x|a.b.config_key|keyword>   # locate the owning doc
./scripts/run.sh schema <doc-relpath>                             # resolve schema_source to code
./scripts/run.sh index [--cache]                                  # dump the frontmatter index (JSON)
./scripts/run.sh lint [--ci]                                      # OKF compliance + drift check
./scripts/run.sh new <type> <name>                               # print a frontmatter skeleton
```

`run.sh` resolves the repo root from its own location (or `INVEST_HOME`) and prefers the
project `.venv` (PyYAML); no extra dependencies otherwise — pure stdlib fallback.

## Layout

```
SKILL.md                       agent-facing guide (the two jobs + the lookup decision tree)
scripts/find_docs.py           stdlib engine: index | find | schema | lint | new
scripts/run.sh                 thin wrapper
references/okf-spec.md          what OKF is
references/conventions.md       the frontmatter schema + maintenance rules
references/lookup-strategy.md   grep-first / script-fallback decision tree
```

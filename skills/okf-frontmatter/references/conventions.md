# openInvest OKF conventions

## What gets frontmatter
The OKF knowledge base is everything matched by `find_docs.py`'s globs:
- `docs/wiki/*.md` — numbered chapters (`type: wiki-chapter`) + `README.md` (`type: index`)
- `docs/wiki/adr/*.md` — decision records (`type: adr`)
- `docs/*.md` — loose docs (`type: reference` / `report` / `readme`)

Gitignored, machine-regenerated reports are **excluded** (the script skips
`docs/verdict_accuracy.md` and `docs/path_calibration.md`). Don't add frontmatter to those.

## Frontmatter schema

```yaml
---
type: wiki-chapter        # REQUIRED. wiki-chapter|adr|index|reference|report|readme
title: Web API 参考        # human title (usually the H1). May be non-English — it describes the doc.
tags: [api, rest]         # coarse categories
intent: API Contract      # ONE short phrase the lookup ranks on. Pick the doc's primary job.
schema_source:            # list of relpath:Symbol pointers to the authoritative code
  - connectors/web_api/models.py:PortfolioResponse
documents:                # concrete things this doc covers (drives `find`)
  endpoints:
    - GET /api/holdings
  config_keys:
    - verdict.risk_profile
  symbols:
    - PortfolioManager
---
```

ADRs add a lifecycle block:
```yaml
status: accepted          # proposed | accepted | superseded
date: 2026-06-17
supersedes: []            # ADR ids this one replaces, e.g. [010]
superseded_by: []
```

### Field rules
- **`type`** is the only hard requirement. `lint` errors without it.
- **`schema_source`** is the heart of the "don't duplicate" rule. If a doc explains a
  Pydantic model / dataclass / config dict, point at it (`file.py:Symbol`) rather than
  re-typing its fields. `lint` errors if the symbol vanishes — that's the drift alarm.
  Resolve it any time with `run.sh schema <doc>`.
- **`documents`** lists the concrete handles a reader would search for: REST endpoints
  (`GET /api/x` — method optional, the path is what matches), dotted config keys, and bare
  code symbol names. These make `find` land on the *owning* doc.
- **`intent`** is a single human phrase (`API Contract`, `决策参数`, `部署`, `数据模型`…).
  It is the tie-breaker the lookup ranks on when a keyword is ambiguous. One per doc.
- **`status`** (ADR) replaces the prose `**状态**` line as the machine-readable truth. Leave
  the prose line alone; don't dual-maintain — frontmatter wins.

## Maintenance rules (carried over from docs/wiki/README.md, now machine-checkable)
1. The wiki is the source of **"why"** — implementation detail lives in code. Keep prose to
   design intent; link to `schema_source` for the rest.
2. New big feature → update (or add) one chapter. New architectural decision → new ADR.
3. An accepted ADR is immutable. To overturn it, add a new ADR with `supersedes: [NNN]` and
   set the old one's `superseded_by`.
4. Run `run.sh lint` before committing doc changes. CI runs `lint --ci` (errors fail the
   build; un-migrated docs do not).

## Scaffolding
`run.sh new <type> <name>` prints a ready-to-fill skeleton. Example:
```bash
run.sh new adr 018-some-decision >> docs/wiki/adr/018-some-decision.md
```

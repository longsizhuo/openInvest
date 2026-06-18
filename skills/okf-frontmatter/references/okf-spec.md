# The Open Knowledge Format (OKF) — the 1-page version

OKF is a vendor-neutral spec (Google, v0.1, June 2026) for representing organizational
knowledge as **directories of small markdown files, each with YAML frontmatter**. Original
announcement: <https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/>

## The problem it solves
Knowledge ends up scattered across incompatible systems — metadata catalogs, wikis, drives,
code comments, people's heads. OKF says: write it as plain markdown + frontmatter you can
**version-control next to the code**, produce it without SDKs, consume it without
integration, and share it across tools.

## The format
- Every document is a markdown file. It **must** have a `type` in its frontmatter; everything
  else is optional. Common optional fields: `title`, `description`, `tags`, `timestamp`,
  `resource`.
- **Relationships are ordinary markdown links** — `[customers](/tables/customers.md)`. The
  set of links across all docs *is* the knowledge graph.
- **File paths are identity.** Directory structure gives hierarchy; an optional `index.md`
  per directory provides a summary + navigation.
- It is **schema-agnostic**: OKF standardizes the envelope (frontmatter + links), not the
  content model. You decide what `type`s mean.

Example from the spec:
```yaml
---
type: BigQuery Table
title: Orders
description: One row per completed customer order.
tags: [sales, revenue]
---
# Schema
| Column | Type | Description |
|--------|------|-------------|
| `order_id` | STRING | Globally unique order id. |
| `customer_id` | STRING | FK to [customers](/tables/customers.md). |
```

## Why it lets you stop hand-maintaining huge docs
The structured frontmatter is the **single source of truth**. Because it's machine-readable,
tooling *generates* the things you used to hand-maintain: directory indexes, navigation,
cross-reference checks, and search. Authors maintain small structured docs; discovery is
derived, not written.

## How openInvest applies it
We keep OKF's envelope (`type` + frontmatter + markdown-link graph) and add two
repo-specific fields so a doc can point at the **code** that is its real source of truth
instead of duplicating it:
- `schema_source: [path/to/file.py:Symbol]` — the authoritative Pydantic model / dataclass /
  config dict.
- `documents: {endpoints, config_keys, symbols}` — the concrete things the doc covers.

`find_docs.py` turns this into a doc↔code index. See `conventions.md` for the exact schema.

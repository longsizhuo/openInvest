#!/usr/bin/env python3
"""find_docs.py — OKF (Open Knowledge Format) doc index / lookup for openInvest.

Why this exists
---------------
openInvest's docs (docs/wiki chapters + docs/wiki/adr) used to be hand-maintained
prose. Under OKF, every doc carries a small YAML frontmatter block that acts as the
single source of truth: `type`, `title`, `tags`, `intent`, plus pointers to the
authoritative code (`schema_source`) and the things it documents (`documents`).

This script turns that frontmatter into a queryable index so an agent can jump
straight to the authoritative doc/schema instead of grepping through thousands of
lines. It is the FALLBACK in the lookup strategy — grep the literal term first; only
reach for `find` when grep is ambiguous (hits scattered across files / synonym
mismatch / zero hits). See the skill's references/lookup-strategy.md.

Design constraints
------------------
- Pure stdlib so it runs under bare `python3` (no project venv, no extra deps).
  Uses PyYAML only if it happens to be importable; otherwise a minimal frontmatter
  parser handles the limited subset the OKF schema uses.
- Never imports the invest package. Resolves the repo root from its own location
  (or --repo) and reads files directly.
- Read-only. `new` prints a skeleton to stdout; it does not write files.

Subcommands: index | find | schema | lint | new
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Docs that live under docs/ but are gitignored / machine-regenerated → not OKF targets.
SKIP_RELPATHS = {
    "docs/verdict_accuracy.md",   # gitignored: contains real hit-rate data
    "docs/path_calibration.md",   # gitignored: regenerated report
}

# Glob patterns (relative to repo root) that make up the OKF knowledge base.
DOC_GLOBS = ("docs/wiki/*.md", "docs/wiki/adr/*.md", "docs/*.md")

MD_LINK_RE = re.compile(r"\[(?P<text>[^\]]+)\]\((?P<href>[^)]+)\)")


# --------------------------------------------------------------------------- #
# Output helper (mirrors scripts/skill_cmds/_helpers.py:_print_json; self-contained)
# --------------------------------------------------------------------------- #
def _print_json(obj: Any) -> None:
    out = getattr(sys, "__stdout__", sys.stdout)
    out.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    out.write("\n")
    out.flush()


# --------------------------------------------------------------------------- #
# Repo root resolution
# --------------------------------------------------------------------------- #
def resolve_repo_root(explicit: str | None) -> Path:
    if explicit:
        root = Path(explicit).resolve()
        if not (root / "docs" / "wiki").is_dir():
            sys.stderr.write(f"warning: {root} has no docs/wiki — using it anyway\n")
        return root
    # Walk up from this file until we find a dir containing docs/wiki.
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "docs" / "wiki").is_dir():
            return parent
    # Fallback: three levels up (skills/okf-frontmatter/scripts → repo root).
    return here.parents[2]


# --------------------------------------------------------------------------- #
# Frontmatter parsing (PyYAML fast-path + minimal stdlib fallback)
# --------------------------------------------------------------------------- #
def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_block, body). frontmatter_block is None if absent."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None, text
    # find the closing '---' on its own line after the opener
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            block = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            return block, body
    return None, text  # unterminated → treat as no frontmatter


def _coerce_scalar(s: str) -> Any:
    s = s.strip()
    if s == "" or s == "[]" or s == "{}":
        return [] if s == "[]" else ({} if s == "{}" else "")
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_strip_quotes(x.strip()) for x in _split_inline_list(inner)]
    return _strip_quotes(s)


def _split_inline_list(inner: str) -> list[str]:
    """Split `a, b, c` respecting simple quoted items."""
    items, cur, quote = [], [], None
    for ch in inner:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur or items:
        items.append("".join(cur))
    return [i for i in (x.strip() for x in items) if i != ""]


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _parse_frontmatter_minimal(block: str) -> dict:
    """Tiny YAML-subset parser: scalars, inline lists, block lists, one nesting level.

    Handles exactly what the OKF schema uses:
        key: scalar
        key: [a, b, c]
        key:
          - item
          - item
        documents:
          endpoints: [GET /api/x]
          symbols:
            - Foo
    """
    data: dict[str, Any] = {}
    lines = block.splitlines()
    i = 0
    n = len(lines)

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    while i < n:
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        if indent_of(raw) != 0 or ":" not in raw:
            i += 1
            continue
        key, _, rest = raw.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest:  # scalar or inline list on same line
            data[key] = _coerce_scalar(rest)
            i += 1
            continue
        # value continues on indented following lines: block list OR nested map
        i += 1
        block_items: list[str] = []
        nested: dict[str, Any] = {}
        while i < n:
            nxt = lines[i]
            if not nxt.strip():
                i += 1
                continue
            ind = indent_of(nxt)
            if ind == 0:
                break
            stripped = nxt.strip()
            if stripped.startswith("- "):
                block_items.append(_strip_quotes(stripped[2:].strip()))
                i += 1
            elif ":" in stripped:  # nested mapping (e.g. documents.endpoints)
                nk, _, nv = stripped.partition(":")
                nk, nv = nk.strip(), nv.strip()
                if nv:
                    nested[nk] = _coerce_scalar(nv)
                    i += 1
                else:
                    # nested block list under the sub-key
                    i += 1
                    sub_items: list[str] = []
                    while i < n and lines[i].strip().startswith("- ") and indent_of(lines[i]) > ind:
                        sub_items.append(_strip_quotes(lines[i].strip()[2:].strip()))
                        i += 1
                    nested[nk] = sub_items
            else:
                i += 1
        data[key] = nested if nested else block_items
    return data


def parse_frontmatter(block: str) -> dict:
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(block)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return _parse_frontmatter_minimal(block)


# --------------------------------------------------------------------------- #
# Doc model
# --------------------------------------------------------------------------- #
def iter_doc_paths(root: Path) -> list[Path]:
    seen: dict[str, Path] = {}
    for pat in DOC_GLOBS:
        for p in sorted(root.glob(pat)):
            rel = p.relative_to(root).as_posix()
            if rel in SKIP_RELPATHS:
                continue
            seen[rel] = p
    return [seen[k] for k in sorted(seen)]


def load_doc(root: Path, path: Path) -> dict:
    rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    block, body = split_frontmatter(text)
    fm = parse_frontmatter(block) if block is not None else {}
    links = []
    for m in MD_LINK_RE.finditer(body):
        href = m.group("href").split("#", 1)[0].strip()
        if href and not href.startswith(("http://", "https://", "mailto:")):
            links.append(href)
    # first markdown H1 as fallback title
    h1 = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), "")
    documents = fm.get("documents") or {}
    if not isinstance(documents, dict):
        documents = {}
    return {
        "doc": rel,
        "migrated": block is not None,
        "type": fm.get("type"),
        "title": fm.get("title") or h1,
        "tags": _as_list(fm.get("tags")),
        "intent": fm.get("intent") or "",
        "schema_source": _as_list(fm.get("schema_source")),
        "documents": {
            "endpoints": _as_list(documents.get("endpoints")),
            "config_keys": _as_list(documents.get("config_keys")),
            "symbols": _as_list(documents.get("symbols")),
        },
        "status": fm.get("status"),
        "supersedes": _as_list(fm.get("supersedes")),
        "superseded_by": _as_list(fm.get("superseded_by")),
        "links": links,
        "_path": path,
    }


def _as_list(v: Any) -> list:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [x for x in v if x not in (None, "")]
    return [v]


def build_index(root: Path) -> list[dict]:
    return [load_doc(root, p) for p in iter_doc_paths(root)]


# --------------------------------------------------------------------------- #
# Symbol resolution (file.py:Symbol -> source definition)
# --------------------------------------------------------------------------- #
def resolve_symbol(root: Path, ref: str, max_lines: int = 80) -> dict:
    if ":" not in ref:
        return {"ref": ref, "found": False, "error": "expected 'path/to/file.py:Symbol'"}
    relpath, symbol = ref.rsplit(":", 1)
    fpath = (root / relpath).resolve()
    if not fpath.is_file():
        return {"ref": ref, "found": False, "error": f"file not found: {relpath}"}
    src = fpath.read_text(encoding="utf-8", errors="replace")
    node = _find_symbol_node(src, symbol)
    if node is None:
        return {"ref": ref, "found": False, "file": relpath, "error": f"symbol not found: {symbol}"}
    seg = ast.get_source_segment(src, node) or ""
    seg_lines = seg.splitlines()
    truncated = len(seg_lines) > max_lines
    if truncated:
        seg = "\n".join(seg_lines[:max_lines]) + f"\n    # ... ({len(seg_lines) - max_lines} more lines)"
    return {
        "ref": ref,
        "found": True,
        "file": relpath,
        "lineno": getattr(node, "lineno", None),
        "kind": type(node).__name__,
        "definition": seg,
    }


def _find_symbol_node(src: str, symbol: str):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == symbol:
                return node
        elif isinstance(node, ast.AnnAssign):  # e.g. API_SETTABLE: Dict[...] = {...}
            if isinstance(node.target, ast.Name) and node.target.id == symbol:
                return node
        elif isinstance(node, ast.Assign):  # e.g. HoldingKind = Literal[...]
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == symbol:
                    return node
    return None


# --------------------------------------------------------------------------- #
# find: query -> ranked docs
# --------------------------------------------------------------------------- #
def _norm_endpoint(s: str) -> str:
    s = s.strip()
    parts = s.split()
    path = parts[-1] if parts else s  # drop leading METHOD
    return path.rstrip("/").lower()


def find_docs(root: Path, query: str) -> list[dict]:
    q = query.strip()
    ql = q.lower()
    q_sym = q.rsplit(":", 1)[-1]  # allow 'file.py:Symbol' queries
    results = []
    for d in build_index(root):
        if not d["migrated"]:
            continue
        reasons: list[str] = []
        # 1. schema_source symbol (ownership — highest)
        for ref in d["schema_source"]:
            sym = ref.rsplit(":", 1)[-1]
            if sym.lower() == ql or sym == q_sym or ref.lower() == ql:
                reasons.append("schema_source")
                break
        # 2. documents.endpoints
        if any(_norm_endpoint(e) == _norm_endpoint(q) for e in d["documents"]["endpoints"]):
            reasons.append("documents.endpoints")
        # 3. documents.config_keys
        if any(k.lower() == ql for k in d["documents"]["config_keys"]):
            reasons.append("documents.config_keys")
        # 4. documents.symbols
        if any(s.lower() == ql for s in d["documents"]["symbols"]):
            reasons.append("documents.symbols")
        # 5. intent / tags
        if d["intent"] and ql in d["intent"].lower():
            reasons.append("intent")
        if any(ql == t.lower() or ql in t.lower() for t in d["tags"]):
            reasons.append("tags")
        # 6. substring fallback (title / symbol names / endpoints)
        if not reasons:
            hay = " ".join([
                d["title"] or "",
                " ".join(d["schema_source"]),
                " ".join(d["documents"]["endpoints"]),
                " ".join(d["documents"]["config_keys"]),
                " ".join(d["documents"]["symbols"]),
            ]).lower()
            if ql in hay:
                reasons.append("substring")
        if reasons:
            results.append({
                "doc": d["doc"],
                "type": d["type"],
                "intent": d["intent"],
                "title": d["title"],
                "why_matched": reasons,
                "_rank": _rank(reasons),
            })
    results.sort(key=lambda r: (r.pop("_rank"), r["doc"]))
    return results


# lower rank = stronger ownership = sorted first
_OWNERSHIP = {
    "schema_source": 0,
    "documents.endpoints": 0,
    "documents.config_keys": 0,
    "documents.symbols": 1,
    "intent": 2,
    "tags": 3,
    "substring": 4,
}


def _rank(reasons: list[str]) -> int:
    return min(_OWNERSHIP.get(r, 9) for r in reasons)


# --------------------------------------------------------------------------- #
# lint
# --------------------------------------------------------------------------- #
VALID_TYPES = {"wiki-chapter", "adr", "index", "reference", "report", "readme"}
VALID_STATUS = {"proposed", "accepted", "superseded"}


def lint(root: Path) -> dict:
    docs = build_index(root)
    rel_set = {d["doc"] for d in docs}
    on_disk = {p.relative_to(root).as_posix() for p in iter_doc_paths(root)}
    issues = []
    migrated = 0
    for d in docs:
        rel = d["doc"]
        if not d["migrated"]:
            issues.append({"doc": rel, "level": "info", "msg": "not_migrated (no frontmatter)"})
            continue
        migrated += 1
        # type
        if not d["type"]:
            issues.append({"doc": rel, "level": "error", "msg": "missing required field: type"})
        elif d["type"] not in VALID_TYPES:
            issues.append({"doc": rel, "level": "warning", "msg": f"unknown type: {d['type']}"})
        # internal links resolve
        base = (root / rel).parent
        for href in d["links"]:
            target = (base / href).resolve()
            if not target.exists():
                issues.append({"doc": rel, "level": "error", "msg": f"broken link: {href}"})
        # schema_source resolves to a real symbol
        for ref in d["schema_source"]:
            res = resolve_symbol(root, ref)
            if not res["found"]:
                issues.append({"doc": rel, "level": "error", "msg": f"schema_source dangling: {ref} ({res.get('error')})"})
        # ADR-specific
        if d["type"] == "adr":
            if not d["status"]:
                issues.append({"doc": rel, "level": "warning", "msg": "ADR missing status"})
            elif d["status"] not in VALID_STATUS:
                issues.append({"doc": rel, "level": "warning", "msg": f"ADR invalid status: {d['status']}"})
            for ref in d["supersedes"] + d["superseded_by"]:
                num = re.sub(r"[^0-9].*$", "", str(ref).lstrip("0") or "0")
                if not any(re.search(rf"adr/0*{re.escape(str(ref).split('-')[0])}\b", x) or str(ref) in x for x in on_disk):
                    # best-effort: warn only
                    issues.append({"doc": rel, "level": "warning", "msg": f"supersede target not found: {ref}"})
    errors = sum(1 for i in issues if i["level"] == "error")
    warnings = sum(1 for i in issues if i["level"] == "warning")
    not_migrated = sum(1 for i in issues if i["level"] == "info")
    return {
        "summary": {
            "total": len(docs),
            "migrated": migrated,
            "not_migrated": not_migrated,
            "errors": errors,
            "warnings": warnings,
        },
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# new: print a frontmatter skeleton
# --------------------------------------------------------------------------- #
def skeleton(doc_type: str, name: str) -> str:
    title = name.replace("-", " ").replace("_", " ").strip()
    common = [
        "---",
        f"type: {doc_type}",
        f"title: {title}",
        "tags: []",
        "intent: ",
        "schema_source: []        # e.g. connectors/web_api/models.py:PortfolioResponse",
        "documents:",
        "  endpoints: []          # e.g. GET /api/holdings",
        "  config_keys: []        # e.g. verdict.risk_profile",
        "  symbols: []            # code symbols this doc explains",
    ]
    if doc_type == "adr":
        common += [
            "status: proposed        # proposed | accepted | superseded",
            "date: ",
            "supersedes: []",
            "superseded_by: []",
        ]
    common += ["---", "", f"# {title}", ""]
    return "\n".join(common)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="find_docs", description="OKF doc index / lookup for openInvest")
    ap.add_argument("--repo", help="repo root (default: auto-detect from this file)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="scan docs and emit the JSON frontmatter index")
    p_index.add_argument("--cache", action="store_true", help="also write docs/.okf-index.json")

    p_find = sub.add_parser("find", help="resolve a symbol/endpoint/config-key/keyword to doc(s)")
    p_find.add_argument("query")

    p_schema = sub.add_parser("schema", help="print the code definitions a doc's schema_source points to")
    p_schema.add_argument("doc", help="doc relpath, e.g. docs/wiki/06-api.md")

    p_lint = sub.add_parser("lint", help="check OKF compliance + drift")
    p_lint.add_argument("--ci", action="store_true", help="machine mode: exit non-zero only on errors")

    p_new = sub.add_parser("new", help="print a frontmatter skeleton")
    p_new.add_argument("type", choices=sorted(VALID_TYPES))
    p_new.add_argument("name")

    args = ap.parse_args(argv)
    root = resolve_repo_root(args.repo)

    if args.cmd == "index":
        idx = build_index(root)
        public = [{k: v for k, v in d.items() if not k.startswith("_")} for d in idx]
        if args.cache:
            (root / "docs" / ".okf-index.json").write_text(
                json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        _print_json(public)
        return 0

    if args.cmd == "find":
        _print_json(find_docs(root, args.query))
        return 0

    if args.cmd == "schema":
        d = load_doc(root, root / args.doc)
        sources = [resolve_symbol(root, ref) for ref in d["schema_source"]]
        _print_json({"doc": d["doc"], "schema_source": d["schema_source"], "sources": sources})
        return 0

    if args.cmd == "lint":
        report = lint(root)
        _print_json(report)
        return 1 if report["summary"]["errors"] > 0 else 0

    if args.cmd == "new":
        out = getattr(sys, "__stdout__", sys.stdout)
        out.write(skeleton(args.type, args.name) + "\n")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

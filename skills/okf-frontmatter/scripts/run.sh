#!/bin/bash
# okf-frontmatter skill wrapper — thin shell over find_docs.py.
#
# Unlike the invest skill's run.sh, this one does NOT clone or `uv sync`: the script
# operates on the docs of the repo it lives in. It resolves the repo root from its own
# location (through the install.sh symlink via `readlink -f`) and delegates everything.
#
# Usage:
#   run.sh find <symbol|endpoint|config-key|keyword>   # locate the authoritative doc
#   run.sh schema <doc-relpath>                         # print the code def a doc points to
#   run.sh index [--cache]                              # dump the frontmatter index (JSON)
#   run.sh lint [--ci]                                  # OKF compliance + drift check
#   run.sh new <type> <name>                            # print a frontmatter skeleton
#
# Lookup strategy: grep first. Only call `run.sh find` when grep is ambiguous
# (hits scattered across files / synonym mismatch / zero hits). See
# references/lookup-strategy.md.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# skills/okf-frontmatter/scripts → up 3 = repo root (override with INVEST_HOME).
REPO_ROOT="${INVEST_HOME:-$(cd "$SKILL_DIR/../../.." && pwd)}"

# Prefer the project venv (has PyYAML); fall back to bare python3 (minimal parser).
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PY="$REPO_ROOT/.venv/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi

exec "$PY" "$SKILL_DIR/find_docs.py" --repo "$REPO_ROOT" "$@"

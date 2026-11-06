"""Keep published agent instructions from reintroducing unsafe setup/data handling.

Run with scripts/run_isolated_tests.py like the rest of the suite. Fixtures are synthetic;
the documented stdin commands terminate at a local fake consumer, never an LLM or ledger.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import subprocess

import pytest


PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
INVEST = PLUGIN / "skills/invest"
SETUP = PLUGIN / "skills/invest-setup"


@pytest.mark.parametrize("filename", ["committee-protocol.md", "committee-protocol-hermes.md"])
def test_report_save_examples_keep_shell_text_literal(tmp_path, filename):
    doc = (INVEST / "references" / filename).read_text()
    stage = doc.split("## Stage 6: Persist the transcript", 1)[1]
    command = re.search(r"```bash\n([^`]+)\n```", stage).group(1)
    assert "<<" not in command
    assert '< "/absolute/path/to/private-committee.txt"' in command
    assert "file-writing tool" in stage
    marker = tmp_path / "executed"
    # Include both command-substitution syntaxes and the old heredoc delimiter.
    payload = f"=== CIO ===\n$(touch {marker})\n`touch {marker}`\nEOF\n'\"$HOME\\\n"
    source = tmp_path / "report with spaces.txt"
    source.write_text(payload)
    source.chmod(0o600)
    runner = tmp_path / "run.sh"
    runner.write_text("#!/bin/sh\ncat\n")
    runner.chmod(0o700)
    command = command.replace("<skill-dir>/scripts/run.sh", shlex.quote(str(runner)))
    if command.startswith("run.sh "):
        command = shlex.quote(str(runner)) + command[len("run.sh"):]
    command = command.replace("<SYMBOL>", "TEST").replace("<model-name>", "fixture")
    command = command.replace('"/absolute/path/to/private-committee.txt"', shlex.quote(str(source)))
    result = subprocess.run(["bash", "-c", command], cwd=tmp_path, text=True,
                            capture_output=True, check=True, timeout=5)
    assert result.stdout == payload
    assert not marker.exists()


@pytest.mark.parametrize("path", [SETUP / "SKILL.md", INVEST / "references/onboarding.md"])
def test_onboarding_serializes_json_without_shell_interpolation(path):
    doc = path.read_text()
    examples = re.findall(r"```json\n(.*?)\n```", doc, re.S)
    payload = next(json.loads(example) for example in examples if '"profile"' in example)
    assert "profile" in payload
    assert "JSON" in doc and "file-writing tool" in doc
    assert 'init --from-stdin < "/absolute/path/to/private-onboarding.json"' in doc
    assert "holdings_v2" in doc
    assert "not consent" in doc
    assert "provider/endpoint" in doc
    assert "echo '{" not in doc


def test_setup_preserves_ready_install_and_requires_cron_opt_in():
    doc = (SETUP / "SKILL.md").read_text()
    assert doc.index("Run `doctor` first") < doc.index("offer MCP registration")
    assert "exit without changing MCP configuration, tracking, or schedules" in doc
    assert "If Q4 was declined or skipped, create no" in doc
    assert "without asking for confirmation" not in doc
    assert "hermes config set mcp_servers.openinvest.trust untrusted" in doc
    assert "hermes config set mcp_servers.openinvest.trust full" not in doc
    cron = (SETUP / "references/hermes-cron.md").read_text()
    assert "only after explicit opt-in" in cron
    assert "separate explicit approval" in cron
    assert "verified private origin" in cron


def test_import_preview_and_direct_analysis_disclose_external_processing():
    doc = (INVEST / "references/tools.md").read_text()
    assert "including a preview without `--commit`" in doc
    assert "Obtain approval to send those details to that provider" in doc
    skill = (INVEST / "SKILL.md").read_text()
    assert "A saved API key is" in skill and "not consent" in skill
    assert "portfolio holdings, cash balances, risk preferences and strategy" in skill
    assert "shared or unknown-audience" in skill

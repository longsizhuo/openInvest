"""Hermes registrar for installs made from the repo root (before plugin/ became the install root).

The real registrar is plugin/__init__.py; plugin.yaml here is a symlink to plugin/plugin.yaml.
Loaded by path, not `from .plugin import`, because this file is also imported without a parent
package (pytest collection from the repo root, Hermes loaders that exec the file directly).
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_openinvest_hermes_plugin", Path(__file__).resolve().parent / "plugin" / "__init__.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
register = _mod.register

"""守卫：src/ 与 scripts/ 里不得再有裸 load_dotenv()。

裸 load_dotenv()（不给路径）从调用模块所在目录**向上搜** .env——worktree 里会一路找到主 checkout
的生产 .env。2026-10-13 事故：worktree 跑 pytest，services/notifier.py 的裸 load_dotenv() 把生产
Discord/Gmail 凭据灌进进程，fixture verdict 真发进了 Discord DM。tests/conftest.py 在测试侧
scrub 凭据，这里堵运行时根因：.env 只在 openinvest/paths.py 单点加载（INVEST_ROOT/.env），
其他地方要么别调，要么显式给路径（位置参数或 dotenv_path=）。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("src", "scripts")


def _bare_calls(text: str):
    """AST 扫 load_dotenv(...)（含 paths.py 的 _load_dotenv 别名）：没有位置实参也没有
    dotenv_path= 的调用 = 没给路径 = 裸调用。走 AST 而不是正则，注释 / 字符串里提到的不算。"""
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if not name.endswith("load_dotenv"):
            continue
        if node.args or any(k.arg == "dotenv_path" for k in node.keywords):
            continue
        yield node.lineno, ast.unparse(node)


def test_no_bare_load_dotenv_in_src_or_scripts():
    offenders = []
    for d in SCAN_DIRS:
        for py in sorted((ROOT / d).rglob("*.py")):
            for lineno, call in _bare_calls(py.read_text(encoding="utf-8")):
                offenders.append(f"{py.relative_to(ROOT)}:{lineno}: {call}")
    assert offenders == [], (
        "裸 load_dotenv() 会向上搜目录读到别的 .env（worktree → 生产 .env）。"
        "删掉它（openinvest.paths 已单点加载 INVEST_ROOT/.env）或显式传路径：\n" + "\n".join(offenders)
    )


def test_guard_catches_the_shapes_we_care_about():
    text = (
        "load_dotenv()\n"
        "load_dotenv(override=True)\n"
        "load_dotenv(ROOT / '.env')\n"
        "load_dotenv(dotenv_path=p, override=True)\n"
        "_load_dotenv(INVEST_ROOT / '.env')\n"
        "dotenv.load_dotenv(\n    override=True,\n)\n"
        "# load_dotenv() 注释里提到不算\n"
    )
    assert [ln for ln, _ in _bare_calls(text)] == [1, 2, 6]


def test_paths_is_the_single_loader():
    """唯一允许的加载点：paths.py 显式传 INVEST_ROOT/.env。"""
    src = (ROOT / "src/openinvest/paths.py").read_text(encoding="utf-8")
    assert '_load_dotenv(INVEST_ROOT / ".env")' in src

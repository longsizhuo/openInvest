"""数据目录单一可信源（src/ 重排后 __file__ 相对路径全部废弃）。

历史教训：数据目录（memory/ db/ docs/ static/）用 `Path(__file__).parent.parent`
解析，代码一挪家路径就悄悄漂——#55 拆 routers/ 时 committee task store 漂进了
connectors/web_api/memory/；src/ 重排如果沿用会把用户账本静默换成空库。

解析顺序：
1. env `INVEST_HOME`（run.sh / 部署显式指定）
2. 从本文件向上找仓库标记（editable / git clone 开发场景）
3. cwd 兜底（wheel 安装 + run.sh cd 到数据目录的场景）

包内资源（capabilities 角色 .md / defaults.yaml / rss_feeds.yml）不走这里——
它们跟包走，继续 __file__ 相对。
"""
from __future__ import annotations

import os
from pathlib import Path


def _detect_root() -> Path:
    env = os.getenv("INVEST_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "skills").is_dir():
            return parent
    return Path.cwd()


INVEST_ROOT = _detect_root()

"""兼容 shim —— 实现已搬到 openinvest.cli（src/ 重排，issue #133 PyPI 路线）。

保留原因：旧版 plugin cache 里的 run.sh 调 `$INVEST_DIR/scripts/skill.py`，
git pull 自愈拉到新代码后仍按老路径进来。新入口：`openinvest <cmd>`（console
script）或 `python -m openinvest.cli`。本 shim 只在 git clone 形态存在，不进 wheel。
"""
import sys
from pathlib import Path

# 裸跑 `python scripts/skill.py`（无 editable install）时把 src/ 挂上 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openinvest.cli import *  # noqa: F401,F403,E402（历史 `from scripts.skill import X` 兼容）
from openinvest.cli import main  # noqa: E402

if __name__ == "__main__":
    main()

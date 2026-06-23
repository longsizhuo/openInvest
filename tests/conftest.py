"""pytest 配置：让仓库根加进 sys.path，无需安装即可 import core/jobs/scripts"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_invest_env(monkeypatch):
    """测试与开发者本地 .env 隔离（修"配置渗进测试"这一类 bug）。

    services/notifier.py 等模块在 import 时 load_dotenv()，把开发者机器 .env 里的
    INVEST_* override 灌进 os.environ。全套跑时这些值会渗进"断言仓库默认值"的测试
    （DCA / 集中度 lens 等），导致本地 pytest 顺序敏感地飘红——CI 没 .env 不受影响，
    所以是只在开发机出现的假红。本 fixture 每个测试前清掉所有 INVEST_* env，让
    load_config() 看到的是 tunable.py 默认值；需要特定 env 的测试照常用
    monkeypatch.setenv（在 test body 里设，晚于本 fixture，不冲突）。
    """
    for key in [k for k in os.environ if k.startswith("INVEST_")]:
        monkeypatch.delenv(key, raising=False)
    yield

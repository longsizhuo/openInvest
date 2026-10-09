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

    仅清 env 不够：load_config() 有模块级 _config_cache（只在 cache-miss 时读
    os.environ），且 set_config_override 写的 _persistent_overrides 也是模块全局——
    任一在 env 还在时被填充，清 env 后 cache/override 仍命中旧值，默认值断言照样飘红。
    所以 yield 前后各 reset_config() 一次：清缓存 + 清 persistent overrides，让本 fixture
    成为真正的 per-test config 隔离闸（不再只隔离 env 一半）。
    """
    from openinvest.core.config import reset_config
    for key in [k for k in os.environ if k.startswith("INVEST_")]:
        monkeypatch.delenv(key, raising=False)
    reset_config()
    yield
    reset_config()


@pytest.fixture(autouse=True)
def _isolate_memory_root(tmp_path, monkeypatch):
    """MemoryStore() 默认目录指到每个测试自己的 tmp（2026-05-10 事故类：测试调 cmd_init 时
    只 patch 了 ROOT，MemoryStore() 仍落在 clone 的 memory/，把 fixture 写进真实 portfolio.md）。
    需要特定目录的测试照常在 test body 里 patch MEMORY_ROOT（晚于本 fixture，不冲突）。"""
    import openinvest.core.memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "_memory_root")

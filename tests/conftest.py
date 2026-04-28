"""pytest 配置：让仓库根加进 sys.path，无需安装即可 import core/jobs/scripts"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

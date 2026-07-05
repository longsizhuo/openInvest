"""兼容 shim → openinvest.migrate_profile（老 clone 直跑 `python scripts/migrate_profile.py`）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openinvest.migrate_profile import main  # noqa: E402

if __name__ == "__main__":
    main()

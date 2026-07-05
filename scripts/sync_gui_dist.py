"""兼容 shim → openinvest.gui_dist（旧 plugin run.sh 调 `python -m scripts.sync_gui_dist`）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openinvest.gui_dist import main  # noqa: E402

if __name__ == "__main__":
    main()

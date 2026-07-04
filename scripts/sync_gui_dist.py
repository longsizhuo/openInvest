"""兼容 shim → openinvest.gui_dist（旧 plugin run.sh 调 `python -m scripts.sync_gui_dist`）。"""
from openinvest.gui_dist import main

if __name__ == "__main__":
    main()

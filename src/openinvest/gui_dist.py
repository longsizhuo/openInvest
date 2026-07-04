"""拉 invest-gui 最新 dist tarball 到 invest/static/

GUI 通过 GitHub Releases 分发（不入 invest 主仓库 git history，避免污染）：
  https://github.com/longsizhuo/invest-gui/releases/download/dist-latest/invest-gui-dist.tar.gz

跑法：
  openinvest-web --sync-only  # 或 python -m openinvest.gui_dist               # 拉最新（dist-latest tag，每次 push main 自动更新）
  openinvest-web --sync-only  # 或 python -m openinvest.gui_dist --tag <tag>   # 指定 tag（pinned 版本）
  openinvest-web --sync-only  # 或 python -m openinvest.gui_dist --owner X --repo Y --tag T  # fork 用户改这里

跑完后启动 invest-web 即可：
  openinvest-web
  # 浏览器开 http://localhost:8765 → 完整 GUI
"""
from __future__ import annotations

import argparse
import io
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

DEFAULT_OWNER = "longsizhuo"
DEFAULT_REPO = "invest-gui"
DEFAULT_TAG = "dist-latest"
ARTIFACT_NAME = "invest-gui-dist.tar.gz"
from openinvest.paths import INVEST_ROOT

STATIC_DIR = INVEST_ROOT / "static"


def _download(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "invest-sync-gui-dist/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--dest", default=str(STATIC_DIR), help="解压目标目录")
    parser.add_argument("--keep-existing", action="store_true",
                        help="不清空目标目录（默认会先清空）")
    args = parser.parse_args()

    url = (
        f"https://github.com/{args.owner}/{args.repo}/releases/download/"
        f"{args.tag}/{ARTIFACT_NAME}"
    )
    print(f"→ 下载 {url}")
    try:
        data = _download(url)
    except Exception as e:
        print(f"❌ 下载失败: {e}")
        print(
            "\n常见原因：\n"
            f"  1. {args.owner}/{args.repo} 仓库不存在或还没发过 release\n"
            f"  2. tag '{args.tag}' 不存在（仓库刚 push 了 main 但 GitHub Action 还没跑完）\n"
            "  3. 网络挡了 github.com（试试代理或 raw.githubusercontent.com 镜像）\n"
        )
        return 1
    print(f"✓ 拿到 {len(data):,} bytes")

    dest = Path(args.dest)
    if not args.keep_existing and dest.exists():
        shutil.rmtree(dest)
        print(f"✓ 清空旧 {dest}")
    dest.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        # filter="data" 防 path traversal（Python 3.12+ 推荐，3.14 强制）
        tar.extractall(dest, filter="data")

    file_count = sum(1 for _ in dest.rglob("*"))
    print(f"✓ 解压到 {dest}（{file_count} 个文件）")

    if not (dest / "index.html").exists():
        print("⚠️ 警告：解压后没找到 index.html，构建产物可能损坏")
        return 1

    print(
        f"\n✅ GUI 已就绪。启动 invest-web 后访问：\n"
        f"   uv run uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765\n"
        f"   浏览器开 http://localhost:8765\n",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

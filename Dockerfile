# 使用轻量级的 Python 3.13 镜像
FROM python:3.13-slim

# 设置工作目录
WORKDIR /app

# 安装 uv (比 pip 更快更现代的包管理器)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# 1. 先复制依赖文件 (利用 Docker 缓存层)
COPY pyproject.toml uv.lock ./

# 2. 安装依赖 (不创建 venv，直接装在系统里，减小体积)
RUN uv sync --frozen --no-cache

# 3. 复制项目代码
COPY . .

# 3.5 把 invest-gui 最新 dist 烤进 static/（web 服务 serve 它 → :8765 直出完整 GUI）。
# 非致命：拉不到（离线 / release 暂缺）也不让 build 挂，web 仍能 serve /api。
RUN /app/.venv/bin/python -m scripts.sync_gui_dist \
    || echo "⚠ GUI dist 未烤入；web 仅 serve /api（运行时可跑 python -m scripts.sync_gui_dist 补）"

# 创建必要的目录
RUN mkdir -p db cache_data

# 设置环境变量
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# 启动命令：APScheduler 入口（旧的 scheduler.py 已在 P4 重构里删除）
CMD ["python", "-m", "scheduler.runner"]

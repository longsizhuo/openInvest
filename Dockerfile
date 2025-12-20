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

# 创建必要的目录
RUN mkdir -p db cache_data

# 设置环境变量
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# 启动命令 (默认运行调度器)
CMD ["python", "scheduler.py"]

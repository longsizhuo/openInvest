# 使用轻量级的 Python 3.13 镜像
FROM python:3.13-slim

# 设置工作目录
WORKDIR /app

# 安装 uv (比 pip 更快更现代的包管理器)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# 1. 先复制依赖文件 (利用 Docker 缓存层)
COPY pyproject.toml uv.lock ./

# 2. 只装第三方依赖（--no-install-project：此时 src/ 还没 COPY，项目本体装不了；
#    #133 PyPI 化后项目自带 hatchling 构建，放到代码层之后再装）
RUN uv sync --frozen --no-cache --no-install-project

# 3. 复制项目代码 + 安装项目本体（依赖已在上层缓存，这步秒级）
COPY . .
RUN uv sync --frozen --no-cache

# 创建必要的目录
RUN mkdir -p db cache_data

# 设置环境变量
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# build 时冒烟：两个容器入口模块必须可导入（防 #133 类模块路径迁移把镜像做坏）
RUN python -c "import openinvest.scheduler.runner, connectors.web_api"

# 启动命令：APScheduler 入口（src-layout 后模块在 openinvest.scheduler.runner）
CMD ["python", "-m", "openinvest.scheduler.runner"]

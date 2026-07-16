FROM python:3.10-slim-bullseye

WORKDIR /app

# 设置环境变量，防止生成 .pyc，不缓存，时区设置
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PORT=6001

# 只安装必要的系统依赖（极简模式）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 复制核心依赖并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 创建必要目录
RUN mkdir -p /app/data /app/static/generated

# 暴露端口
EXPOSE 6001

# 启动服务
CMD ["python", "app.py"]

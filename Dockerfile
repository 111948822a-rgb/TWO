FROM python:3.10-slim

# MALLOC_ARENA_MAX=2: glibc 默认按 8×CPU核 创建 malloc arena,多线程 Python 进程
#   RSS 虚高 30-80MB;限制为 2 后显著压低常驻内存(512MB 小实例必备)。
# MALLOC_TRIM_THRESHOLD_=65536: 空闲堆超过 64KB 即归还 OS,避免峰值后 RSS 不回落。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=65536

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
       libgl1 libglib2.0-0 libgomp1 libgfortran5 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN mkdir -p /data/db /data/storage \
    && sed -i 's/\r$//' /app/start.sh \
    && chmod +x /app/start.sh

# 通过 start.sh 启动:打印运行环境 + 执行 gunicorn,便于排查部署问题。
# 若 Render 控制台(Settings → Docker Command)填了内容,会覆盖此 CMD。
CMD ["sh", "/app/start.sh"]

FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN mkdir -p /app/data /app/storage \
    && sed -i 's/\r$//' /app/start.sh \
    && chmod +x /app/start.sh

# 通过 start.sh 启动:打印运行环境 + 执行 gunicorn,便于排查部署问题。
# 若 Render 控制台(Settings → Docker Command)填了内容,会覆盖此 CMD。
CMD ["sh", "/app/start.sh"]

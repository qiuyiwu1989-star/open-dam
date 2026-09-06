FROM python:3.12-slim

# 系统依赖:Pillow 需要的图像库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo zlib1g libpng16-16 libwebp7 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# 分层:先装依赖(缓存友好)
COPY docker/requirements.txt /src/docker/requirements.txt
RUN pip install --no-cache-dir -r /src/docker/requirements.txt \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir open_clip_torch imagehash

COPY app /src/app
COPY worker /src/worker

ENV PYTHONUNBUFFERED=1

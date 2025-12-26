FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8010 \
    HTTP_TIMEOUT_SEC=300 \
    MAX_DOWNLOAD_MB=500 \
    TMP_PREFIX=ffmix_ \
    FFMPEG_BIN=ffmpeg \
    FFPROBE_BIN=ffprobe

RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py /app/app.py

# добавили requests (если app.py качает через requests)
RUN pip install --no-cache-dir fastapi uvicorn httpx pydantic requests

EXPOSE 8010
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]

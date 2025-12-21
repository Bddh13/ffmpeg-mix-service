FROM python:3.11-slim

# ffmpeg + certs for https downloads
RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py

ENV PYTHONUNBUFFERED=1
ENV HTTP_TIMEOUT_SEC=300
ENV MAX_DOWNLOAD_MB=500
ENV TMP_PREFIX=ffmix_

EXPOSE 8010

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]

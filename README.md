# ffmpeg-mix-service

Микросервис на **FastAPI + ffmpeg** для сборки “видео-открытки”.

Что делает:
- скачивает **видео** по `video_url`
- скачивает **фоновую музыку** по `music_url`
- **обрезает музыку** ровно под `duration_ms` и делает **fade-out** (`fade_out_ms`)
- **подмешивает музыку** к голосу:
  - если `voice_url` **не передан** → считается, что голос уже **внутри видео** (например, lip-sync результат)
  - если `voice_url` **передан** → миксует `voice_url` + `music_url`, затем кладёт в видео
- ограничивает итоговую длительность ролика по `duration_ms`
- возвращает готовый `out.mp4` бинарником

> Важно: сервис специально возвращает файл через `FileResponse` и удаляет временные файлы только после отправки ответа (исправляет “aborted”/`FileNotFoundError` при стриминге).

---

## API

### Доступные эндпоинты

- `GET /health` — проверка работоспособности сервиса (JSON).
- `POST /mix` — смешивание видео + музыки (и опционально отдельного голоса) в один `out.mp4`, отдаётся бинарником.
- `POST /clip` — вырезка клипа из длинного видео по таймингам и приведение к вертикальному формату Reels (по умолчанию 1080×1920), отдаётся бинарником.

### `GET /health`
Проверка “жив ли сервис”.

**Response**
{ "ok": true, "service": "ffmpeg-mix-service" }

### `POST /mix`

**Headers**
* `Content-Type: application/json`
* `X-API-Key: <key>` (если задан `API_KEY` в окружении)

**Body**

```json
{
  "video_url": "https://example.com/input.mp4",
  "music_url": "https://example.com/music.mp3",
  "duration_ms": 6516,
  "music_volume": 0.18,
  "fade_out_ms": 1000,

  "voice_url": null,
  "voice_volume": 1.0
}
```

## `POST /clip`

Вырезает фрагмент из длинного видео по таймингам и возвращает MP4 бинарником.

### Заголовки

- `Content-Type: application/json`
- `X-API-Key: <ключ>` (если в `.env` задан `API_KEY`)

### Тело запроса (JSON)

```json
{
  "video_url": "https://example.com/long.mp4",
  "start_ms": 120000,
  "end_ms": 145000,

  "out_w": 1080,
  "out_h": 1920,
  "mode": "cover_center",

  "crf": 20,
  "preset": "veryfast"
}
````

**Важно:** `start_ms` и `end_ms` должны быть **целыми числами в миллисекундах**.

Поля:

* `video_url` — ссылка на исходное видео (обязательно)
* `start_ms`, `end_ms` — начало/конец фрагмента в мс (обязательно)
* `out_w`, `out_h` — размер итогового видео (по умолчанию 1080×1920)
* `mode` — режим кадрирования (сейчас поддерживается `cover_center`)
* `crf`, `preset` — параметры кодирования H.264 (опционально)

### Как работает кадрирование под Reels

Используется фильтр:

* `scale=OUT_W:OUT_H:force_original_aspect_ratio=increase` — масштаб “cover”
* `crop=OUT_W:OUT_H` — центр-кроп до нужного размера
* `setsar=1` — помогает избежать “сплюснутого” воспроизведения на части устройств/плееров

### Точный старт (без прыжка к ключевому кадру)

Для повышения точности старта применяется двухшаговый seek в ffmpeg:

* `-ss` **до** `-i` (быстро подлетает близко к нужному месту)
* `-ss` **после** `-i` (точная доводка старта, часто получается `0`)

### Ответ

* `200 OK`
* `Content-Type: video/mp4`
* Тело ответа: бинарный `mp4`

### Пример (curl)

```bash
curl -L -o clip.mp4 \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "video_url":"https://example.com/long.mp4",
    "start_ms": 120000,
    "end_ms": 145000
  }' \
  http://127.0.0.1:8010/clip
```

---

## Примечание для n8n

`/clip` принимает тайминги в миллисекундах (int). Если тайминги у вас в секундах (float), конвертируйте:

* `start_ms = Math.round(start_s * 1000)`
* `end_ms = Math.round(end_s * 1000)`

Поля:
* `video_url` (string, required) — ссылка на mp4
* `music_url` (string, required) — ссылка на музыку (mp3/m4a/…)
* `duration_ms` (int, required) — длительность голоса в мс (например, `audio_length` из Minimax)
* `music_volume` (float, optional, default `0.18`) — громкость музыки
* `fade_out_ms` (int, optional, default `1000`) — затухание музыки в конце
* `voice_url` (string|null, optional) — ссылка на отдельный голосовой трек; если `null` — голос берётся из видео
* `voice_volume` (float, optional, default `1.0`) — громкость голоса (если `voice_url` задан)

**Response**

```json
* `200 OK` + бинарный `video/mp4` (`out.mp4`)
```

---

## Быстрый запуск (Docker Compose)

### 1) Подготовить `.env`
cp .env.example .env
Заполни `API_KEY` (желательно длинный случайный).

### 2) Собрать и запустить

```json
docker compose up -d --build
```

Проверка:

```json
curl -s http://127.0.0.1:8010/health
```

---

## Пример запроса через curl

Если сервис доступен локально на `127.0.0.1:8010`:

```json
curl -L -o out.mp4 \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "video_url": "https://example.com/input.mp4",
    "music_url": "https://example.com/music.mp3",
    "duration_ms": 6516,
    "music_volume": 0.18,
    "fade_out_ms": 1000
  }' \
  http://127.0.0.1:8010/mix
```

---

## Использование с n8n (если n8n в Docker на другом VPS)

Если сервис запущен на отдельном сервере и наружу не торчит, удобно пробросить через SSH-туннель:

```json
ssh -N \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -L 0.0.0.0:18010:127.0.0.1:8010 \
  root@<FFMPEG_SERVER_IP>
```

На VPS с n8n:

* хостовый docker0 обычно `172.17.0.1`
* в HTTP Request node n8n URL:

  * `http://172.17.0.1:18010/mix`

В n8n node:

* Response Format: `File`
* Binary Property: `data`
* Timeout: `600000` (или больше, если большие файлы)

---

## Переменные окружения (env)

* `API_KEY` — если задан, запросы должны содержать `X-API-Key`
* `HTTP_TIMEOUT_SEC` — таймаут скачивания
* `MAX_DOWNLOAD_MB` — лимит размера скачиваемого файла
* `TMP_PREFIX` — префикс временных папок
* `FFMPEG_BIN` — путь/имя `ffmpeg` (по умолчанию `ffmpeg`)
* `FFPROBE_BIN` — путь/имя `ffprobe` (по умолчанию `ffprobe`)
* `PORT` — порт сервиса (по умолчанию `8010`)

---

## Notes

* Подрезка музыки делается с кодированием в AAC и упаковкой в `.m4a` (MP4 container), чтобы избежать ошибок контейнера/кодека.
* Если у видео нет аудио-стрима и `voice_url` не задан, сервис попытается просто добавить музыку как единственную аудио-дорожку.


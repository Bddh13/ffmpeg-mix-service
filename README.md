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

## Переменные окружения

* `API_KEY` — если задан, сервис проверяет header `X-API-Key`
* `HTTP_TIMEOUT_SEC` — таймаут скачивания файлов по URL (default 300)
* `MAX_DOWNLOAD_MB` — лимит на размер скачиваемых файлов (default 500)
* `TMP_PREFIX` — префикс tmp-папок
* `FFMPEG_BIN` — путь к ffmpeg (по умолчанию `ffmpeg`)

---

## Notes

* Подрезка музыки делается с кодированием в AAC и упаковкой в `.m4a` (MP4 container), чтобы избежать ошибок контейнера/кодека.
* Если у видео нет аудио-стрима и `voice_url` не задан, сервис попытается просто добавить музыку как единственную аудио-дорожку.


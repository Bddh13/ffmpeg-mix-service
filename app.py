# app.py
# ffmpeg-mix-service (+ /clip)
# FastAPI + ffmpeg: микс музыки/голоса с видео и вырезка клипов под вертикальный Reels-формат.

import os
import uuid
import json
import shutil
import tempfile
import subprocess
from typing import Optional, Literal

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, HttpUrl
from starlette.background import BackgroundTask

# -----------------------------
# ENV
# -----------------------------
API_KEY = (os.getenv("API_KEY") or "").strip()
HTTP_TIMEOUT_SEC = int(os.getenv("HTTP_TIMEOUT_SEC") or "300")
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB") or "500")
TMP_PREFIX = os.getenv("TMP_PREFIX") or "ffmix_"
FFMPEG_BIN = os.getenv("FFMPEG_BIN") or "ffmpeg"
FFPROBE_BIN = os.getenv("FFPROBE_BIN") or "ffprobe"

app = FastAPI(title="ffmpeg-mix-service", version="1.1.0")


# -----------------------------
# Models
# -----------------------------
class MixRequest(BaseModel):
    video_url: HttpUrl
    music_url: HttpUrl
    duration_ms: int = Field(..., gt=0)

    music_volume: float = Field(0.18, ge=0.0, le=10.0)
    fade_out_ms: int = Field(1000, ge=0)

    voice_url: Optional[HttpUrl] = None
    voice_volume: float = Field(1.0, ge=0.0, le=10.0)


class ClipRequest(BaseModel):
    video_url: HttpUrl
    start_ms: int = Field(..., ge=0, description="Start time in ms")
    end_ms: int = Field(..., ge=0, description="End time in ms")

    out_w: int = Field(1080, ge=2, le=4096)
    out_h: int = Field(1920, ge=2, le=4096)
    mode: Literal["cover_center"] = "cover_center"

    crf: int = Field(20, ge=0, le=35)
    preset: str = Field("veryfast")


# -----------------------------
# Utils
# -----------------------------
def _check_api_key(request: Request) -> None:
    if not API_KEY:
        return
    got = request.headers.get("X-API-Key", "")
    if got != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _cleanup_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        tail = (p.stderr or "")[-4000:]
        raise HTTPException(status_code=500, detail=f"ffmpeg failed (code={p.returncode}): {tail}")


def _download(url: str, out_path: str) -> None:
    max_bytes = MAX_DOWNLOAD_MB * 1024 * 1024
    total = 0

    with requests.get(url, stream=True, timeout=HTTP_TIMEOUT_SEC) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail="Download too large")
                f.write(chunk)


def _has_audio_stream(video_path: str) -> bool:
    # ffprobe -select_streams a:0 -show_entries stream=index -of json input.mp4
    cmd = [
        FFPROBE_BIN,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "json",
        video_path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        # Если ffprobe недоступен/упал, лучше не ломать запрос — считаем что аудио может быть.
        return True
    try:
        data = json.loads(p.stdout or "{}")
        return bool(data.get("streams"))
    except Exception:
        return True


def _sec(ms: int) -> float:
    return ms / 1000.0


# -----------------------------
# API
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True, "service": "ffmpeg-mix-service"}


@app.post("/mix")
def mix(req: MixRequest, request: Request):
    _check_api_key(request)

    tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    in_video = os.path.join(tmpdir, "input.mp4")
    in_music = os.path.join(tmpdir, "music.in")
    in_voice = os.path.join(tmpdir, "voice.in")
    out_path = os.path.join(tmpdir, "out.mp4")

    duration_s = _sec(req.duration_ms)
    fade_out_s = min(_sec(req.fade_out_ms), duration_s)
    fade_start_s = max(0.0, duration_s - fade_out_s)

    try:
        _download(str(req.video_url), in_video)
        _download(str(req.music_url), in_music)
        if req.voice_url:
            _download(str(req.voice_url), in_voice)

        video_has_audio = _has_audio_stream(in_video)

        # Общая идея:
        # - Режем видео до duration_s
        # - Музыку делаем fade-out в конце
        # - Если voice_url задан: миксуем voice+music -> кладём в видео
        # - Если voice_url НЕ задан: считаем что голос уже в видео и миксуем audio(video)+music
        #
        # NB: Сервис отдаёт FileResponse, поэтому cleanup делаем BackgroundTask-ом.

        if req.voice_url:
            # Миксуем voice + music
            # atrim ограничивает строго по duration_s, music fade-out применяется только к музыке.
            filter_complex = (
                f"[1:a]atrim=0:{duration_s},asetpts=PTS-STARTPTS,"
                f"volume={req.music_volume},"
                f"afade=t=out:st={fade_start_s}:d={fade_out_s}[m];"
                f"[2:a]atrim=0:{duration_s},asetpts=PTS-STARTPTS,"
                f"volume={req.voice_volume}[v];"
                f"[m][v]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            )

            cmd = [
                FFMPEG_BIN,
                "-hide_banner",
                "-y",
                "-i",
                in_video,
                "-i",
                in_music,
                "-i",
                in_voice,
                "-t",
                f"{duration_s:.3f}",
                "-filter_complex",
                filter_complex,
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",  # видео не трогаем (быстрее)
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                out_path,
            ]
            _run(cmd)

        else:
            if video_has_audio:
                # Миксуем audio(video) + music
                filter_complex = (
                    f"[1:a]atrim=0:{duration_s},asetpts=PTS-STARTPTS,"
                    f"volume={req.music_volume},"
                    f"afade=t=out:st={fade_start_s}:d={fade_out_s}[m];"
                    f"[0:a]atrim=0:{duration_s},asetpts=PTS-STARTPTS[v0];"
                    f"[v0][m]amix=inputs=2:duration=first:dropout_transition=0[aout]"
                )

                cmd = [
                    FFMPEG_BIN,
                    "-hide_banner",
                    "-y",
                    "-i",
                    in_video,
                    "-i",
                    in_music,
                    "-t",
                    f"{duration_s:.3f}",
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "0:v:0",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    out_path,
                ]
                _run(cmd)
            else:
                # В видео нет аудио: кладём только музыку как единственную дорожку
                filter_complex = (
                    f"[1:a]atrim=0:{duration_s},asetpts=PTS-STARTPTS,"
                    f"volume={req.music_volume},"
                    f"afade=t=out:st={fade_start_s}:d={fade_out_s}[aout]"
                )

                cmd = [
                    FFMPEG_BIN,
                    "-hide_banner",
                    "-y",
                    "-i",
                    in_video,
                    "-i",
                    in_music,
                    "-t",
                    f"{duration_s:.3f}",
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "0:v:0",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    out_path,
                ]
                _run(cmd)

        return FileResponse(
            out_path,
            media_type="video/mp4",
            filename="out.mp4",
            background=BackgroundTask(_cleanup_dir, tmpdir),
        )

    except HTTPException:
        _cleanup_dir(tmpdir)
        raise
    except Exception as e:
        _cleanup_dir(tmpdir)
        raise HTTPException(status_code=500, detail=f"Unhandled error: {e}")


@app.post("/clip")
def clip(req: ClipRequest, request: Request):
    _check_api_key(request)

    if req.end_ms <= req.start_ms:
        raise HTTPException(status_code=400, detail="end_ms must be > start_ms")

    start_s = _sec(req.start_ms)
    dur_s = _sec(req.end_ms - req.start_ms)

    tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    in_video = os.path.join(tmpdir, "input.mp4")
    out_path = os.path.join(tmpdir, "out.mp4")

    try:
        _download(str(req.video_url), in_video)

        # Вертикальный Reels:
        # scale "cover" (inccrease) + crop по центру.
        # setsar=1 реально спасает от “сплюснуто” на части устройств/плееров (SAR может уехать).
        if req.mode == "cover_center":
            vf = (
                f"scale={req.out_w}:{req.out_h}:force_original_aspect_ratio=increase,"
                f"crop={req.out_w}:{req.out_h},"
                f"setsar=1"
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported mode: {req.mode}")

        # Максимально точный кадр-старт (без “прыжка к ключевому кадру”):
        # двухшаговый seek:
        #   1) -ss ДО -i (быстро)  -> прыгаем близко к нужному месту по ключевым кадрам
        #   2) -ss ПОСЛЕ -i (точно) -> докодируем/выкидываем лишние кадры до точного старта
        #
        # Если start_s маленький, второй шаг может быть ровно "-ss 0" (как ты и просил).
        fast_seek_s = max(0.0, start_s - 2.0)  # “подпрыгиваем” на ~2 секунды раньше
        accurate_seek_s = start_s - fast_seek_s  # остаток для точного seek-а (может быть 0)

        cmd = [
            FFMPEG_BIN,
            "-hide_banner",
            "-y",
            "-ss",
            f"{fast_seek_s:.3f}",
            "-i",
            in_video,
            "-ss",
            f"{accurate_seek_s:.3f}",  # часто получится 0.000
            "-t",
            f"{dur_s:.3f}",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            req.preset,
            "-crf",
            str(req.crf),
            "-pix_fmt",
            "yuv420p",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            out_path,
        ]

        _run(cmd)

        return FileResponse(
            out_path,
            media_type="video/mp4",
            filename="out.mp4",
            background=BackgroundTask(_cleanup_dir, tmpdir),
        )

    except HTTPException:
        _cleanup_dir(tmpdir)
        raise
    except Exception as e:
        _cleanup_dir(tmpdir)
        raise HTTPException(status_code=500, detail=f"Unhandled error: {e}")


@app.exception_handler(HTTPException)
def http_exception_handler(_, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT") or "8010"))

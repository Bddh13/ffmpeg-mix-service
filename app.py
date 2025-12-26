# app.py
# ffmpeg-mix-service (+ /clip)
# FastAPI + ffmpeg: микс музыки/голоса с видео и вырезка клипов под вертикальный Reels-формат.
#
# /mix:
#   - Берём duration_ms как длину "основного" (голос/аудио-часть)
#   - Добавляем хвост музыки +1 сек (TAIL_MS=1000)
#   - Музыку фейдим в конце (только на добавленной секунде)
#   - Видео делаем той же длины, что и подложка (duration + 1 сек) и фейдим последнюю секунду в чёрный
#
# /clip:
#   - Вырезка фрагмента по start_ms/end_ms
#   - Вертикальный Reels (scale cover + center crop + setsar=1)
#   - Двухшаговый seek для максимально точного кадр-старта

import os
import re
import json
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Literal

import httpx
from fastapi import FastAPI, HTTPException, Header, Request
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

# Хвост музыки/видео в конце /mix (всегда +1 сек по умолчанию)
TAIL_MS = int(os.getenv("TAIL_MS") or "1000")

app = FastAPI(title="ffmpeg-mix-service", version="1.2.0")


# -----------------------------
# Models
# -----------------------------
class MixRequest(BaseModel):
    video_url: HttpUrl
    music_url: HttpUrl
    duration_ms: int = Field(..., gt=0)

    music_volume: float = Field(0.18, ge=0.0, le=10.0)
    fade_out_ms: int = Field(1000, ge=0)  # оставлено для совместимости, но фейд хвоста делаем по TAIL_MS

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
def _check_api_key_value(x_api_key: Optional[str]) -> None:
    if not API_KEY:
        return
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid X-API-Key")


def _cleanup_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        tail = (p.stderr or "")[-4000:]
        raise HTTPException(status_code=500, detail=f"ffmpeg failed (code={p.returncode}): {tail}")


def _run_probe(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


async def _download_to(path: Path, url: str) -> None:
    max_bytes = MAX_DOWNLOAD_MB * 1024 * 1024

    async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT_SEC) as client:
        r = await client.get(str(url))
        r.raise_for_status()

        total = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            async for chunk in r.aiter_bytes(chunk_size=1024 * 256):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail="Download too large")
                f.write(chunk)


def _has_audio_stream(video_path: str) -> bool:
    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=index",
        "-of", "json",
        video_path,
    ]
    p = _run_probe(cmd)
    if p.returncode != 0:
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
async def mix(req: MixRequest, x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    _check_api_key_value(x_api_key)

    # Базовая длительность (голос/основной фрагмент)
    base_s = max(_sec(int(req.duration_ms)), 0.001)

    # Хвост: всегда +1 сек (по умолчанию), можно менять через env TAIL_MS
    tail_ms = max(int(TAIL_MS), 1)
    tail_s = max(_sec(tail_ms), 0.001)

    total_s = max(base_s + tail_s, 0.001)

    # Музыку фейдим ровно на хвосте (последняя добавленная секунда)
    # Важно: fade start = base_s (начало хвоста)
    fade_start_s = base_s
    fade_dur_s = tail_s

    tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    tmp = Path(tmpdir)

    in_video = tmp / "input.mp4"
    in_music = tmp / "music.in"
    in_voice = tmp / "voice.in"
    out_path = tmp / "out.mp4"

    try:
        await _download_to(in_video, str(req.video_url))
        await _download_to(in_music, str(req.music_url))
        if req.voice_url:
            await _download_to(in_voice, str(req.voice_url))

        video_has_audio = _has_audio_stream(str(in_video))

        # Видео: режем до base_s, добавляем хвост (clone last frame) и фейдим хвост в чёрный
        # tpad stop_duration = tail_s (если tail_s=0, то tpad ничего не добавит)
        v_filter = (
            f"[0:v]trim=0:{base_s:.3f},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={tail_s:.3f},"
            f"fade=t=out:st={fade_start_s:.3f}:d={fade_dur_s:.3f}[vout]"
        )

        # Музыка: режем до total_s, фейдим на хвосте (последняя добавленная секунда)
        m_filter = (
            f"[1:a]atrim=0:{total_s:.3f},asetpts=PTS-STARTPTS,"
            f"volume={req.music_volume},"
            f"afade=t=out:st={fade_start_s:.3f}:d={fade_dur_s:.3f}[m]"
        )

        # Дальше собираем аудио в зависимости от наличия voice_url / аудио в видео
        if req.voice_url:
            a_filter = (
                f"[2:a]atrim=0:{base_s:.3f},asetpts=PTS-STARTPTS,"
                f"volume={req.voice_volume}[v];"
                f"[m][v]amix=inputs=2:duration=longest:dropout_transition=0,"
                f"atrim=0:{total_s:.3f}[aout]"
            )
            filter_complex = v_filter + ";" + m_filter + ";" + a_filter

            cmd = [
                FFMPEG_BIN,
                "-hide_banner",
                "-y",
                "-i", str(in_video),
                "-i", str(in_music),
                "-i", str(in_voice),
                "-t", f"{total_s:.3f}",
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", "[aout]",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                str(out_path),
            ]
            _run(cmd)

        else:
            if video_has_audio:
                a_filter = (
                    f"[0:a]atrim=0:{base_s:.3f},asetpts=PTS-STARTPTS[a0];"
                    f"[a0][m]amix=inputs=2:duration=longest:dropout_transition=0,"
                    f"atrim=0:{total_s:.3f}[aout]"
                )
                filter_complex = v_filter + ";" + m_filter + ";" + a_filter

                cmd = [
                    FFMPEG_BIN,
                    "-hide_banner",
                    "-y",
                    "-i", str(in_video),
                    "-i", str(in_music),
                    "-t", f"{total_s:.3f}",
                    "-filter_complex", filter_complex,
                    "-map", "[vout]",
                    "-map", "[aout]",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "20",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(out_path),
                ]
                _run(cmd)
            else:
                # В видео нет аудио: кладём только музыку как единственную дорожку
                filter_complex = v_filter + ";" + m_filter + ";[m]atrim=0:{:.3f}[aout]".format(total_s)

                cmd = [
                    FFMPEG_BIN,
                    "-hide_banner",
                    "-y",
                    "-i", str(in_video),
                    "-i", str(in_music),
                    "-t", f"{total_s:.3f}",
                    "-filter_complex", filter_complex,
                    "-map", "[vout]",
                    "-map", "[aout]",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "20",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(out_path),
                ]
                _run(cmd)

        return FileResponse(
            str(out_path),
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
async def clip(req: ClipRequest, x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    _check_api_key_value(x_api_key)

    if req.end_ms <= req.start_ms:
        raise HTTPException(status_code=400, detail="end_ms must be > start_ms")

    start_s = _sec(req.start_ms)
    dur_s = _sec(req.end_ms - req.start_ms)

    tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    tmp = Path(tmpdir)

    in_video = tmp / "input.mp4"
    out_path = tmp / "out.mp4"

    try:
        await _download_to(in_video, str(req.video_url))

        # Вертикальный Reels:
        # scale "cover" (increase) + crop по центру.
        # setsar=1 реально спасает от “сплюснуто” на части устройств/плееров (SAR может уехать).
        if req.mode == "cover_center":
            vf = (
                f"scale={req.out_w}:{req.out_h}:force_original_aspect_ratio=increase,"
                f"crop={req.out_w}:{req.out_h},"
                f"setsar=1"
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported mode: {req.mode}")

        # Максимально точный кадр-старт: двухшаговый seek
        fast_seek_s = max(0.0, start_s - 2.0)
        accurate_seek_s = start_s - fast_seek_s

        cmd = [
            FFMPEG_BIN,
            "-hide_banner",
            "-y",
            "-ss", f"{fast_seek_s:.3f}",
            "-i", str(in_video),
            "-ss", f"{accurate_seek_s:.3f}",
            "-t", f"{dur_s:.3f}",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", req.preset,
            "-crf", str(req.crf),
            "-pix_fmt", "yuv420p",
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_path),
        ]

        _run(cmd)

        return FileResponse(
            str(out_path),
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

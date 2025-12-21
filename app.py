# app.py
# FastAPI microservice: trims background music to voice length (+ optional fade out),
# mixes voice + music, muxes into video, returns MP4.
#
# Important:
# - Returns FileResponse with BackgroundTask cleanup so tmp files are removed AFTER response is sent.
# - Trimmed music is written as .m4a (AAC in MP4 container). Do NOT write AAC into .mp3 container.

import os
import re
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.responses import FileResponse
from starlette.background import BackgroundTask


APP_NAME = "ffmpeg-mix-service"

API_KEY = os.getenv("API_KEY", "")  # require header X-API-Key if set
TMP_PREFIX = os.getenv("TMP_PREFIX", "ffmix_")
HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "300"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "500"))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

app = FastAPI(title=APP_NAME)


class MixRequest(BaseModel):
    video_url: str = Field(..., description="Public URL to input MP4 video")
    voice_url: Optional[str] = Field(None, description="Public URL to voice audio (optional)")
    music_url: str = Field(..., description="Public URL to background music audio")
    duration_ms: int = Field(..., ge=1, description="Voice duration in milliseconds (used to trim music/video)")
    music_volume: float = Field(0.18, ge=0.0, le=2.0, description="Background music volume multiplier")
    fade_out_ms: int = Field(1000, ge=0, description="Fade out duration for music in ms")
    voice_volume: float = Field(1.0, ge=0.0, le=3.0, description="Voice volume multiplier")


def _validate_url(u: str) -> None:
    if not isinstance(u, str) or not u.strip():
        raise HTTPException(status_code=400, detail="Empty URL")
    if not re.match(r"^https?://", u.strip(), re.IGNORECASE):
        raise HTTPException(status_code=400, detail=f"URL must start with http(s): {u}")


async def _download_to(path: Path, url: str) -> None:
    _validate_url(url)
    max_bytes = MAX_DOWNLOAD_MB * 1024 * 1024

    async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT_SEC) as client:
        r = await client.get(url)
        r.raise_for_status()

        total = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            async for chunk in r.aiter_bytes(chunk_size=1024 * 256):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Downloaded file too large (> {MAX_DOWNLOAD_MB} MB): {url}",
                    )
                f.write(chunk)


def _run_ffmpeg(args: list[str]) -> None:
    cmd = [FFMPEG_BIN, "-y", *args]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "ffmpeg failed",
                "returncode": p.returncode,
                "stderr_tail": p.stderr[-4000:],
                "cmd": " ".join(cmd[:40]) + (" ..." if len(cmd) > 40 else ""),
            },
        )


@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME}


@app.post("/mix")
async def mix(req: MixRequest, x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid X-API-Key")

    _validate_url(req.video_url)
    _validate_url(req.music_url)
    if req.voice_url:
        _validate_url(req.voice_url)

    voice_ms = int(req.duration_ms)
    if voice_ms <= 0:
        raise HTTPException(status_code=400, detail="duration_ms must be > 0")

    tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    tmp = Path(tmpdir)

    video_in = tmp / "video.mp4"
    music_in = tmp / "music.mp3"
    voice_in = tmp / "voice.mp3"

    # IMPORTANT: trimmed music is AAC in MP4 container, NOT .mp3
    music_trim = tmp / "music_trim.m4a"
    out_path = tmp / "out.mp4"

    try:
        await _download_to(video_in, req.video_url)
        await _download_to(music_in, req.music_url)
        if req.voice_url:
            await _download_to(voice_in, req.voice_url)

        dur_s = max(voice_ms / 1000.0, 0.001)
        fade_s = max(req.fade_out_ms / 1000.0, 0.0)
        fade_start = max(dur_s - fade_s, 0.0)

        music_af = f"atrim=0:{dur_s},asetpts=PTS-STARTPTS,volume={req.music_volume}"
        if fade_s > 0:
            music_af += f",afade=t=out:st={fade_start}:d={fade_s}"

        # Trim music into .m4a (AAC in MP4 container)
        _run_ffmpeg([
            "-i", str(music_in),
            "-map_metadata", "-1",
            "-filter:a", music_af,
            "-t", f"{dur_s}",
            "-vn",
            "-c:a", "aac",
            "-b:a", "192k",
            "-f", "mp4",
            str(music_trim),
        ])

        if req.voice_url:
            # External voice + music mix -> mux into video
            amix_filter = (
                f"[1:a]volume={req.voice_volume},asetpts=PTS-STARTPTS[voice];"
                f"[2:a]asetpts=PTS-STARTPTS[music];"
                f"[voice][music]amix=inputs=2:duration=first:dropout_transition=0,atrim=0:{dur_s}[aout]"
            )

            _run_ffmpeg([
                "-i", str(video_in),
                "-i", str(voice_in),
                "-i", str(music_trim),
                "-filter_complex", amix_filter,
                "-map", "0:v:0",
                "-map", "[aout]",
                "-t", f"{dur_s}",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                str(out_path),
            ])
        else:
            # Mix existing video audio with trimmed music.
            mix_filter = (
                f"[1:a]asetpts=PTS-STARTPTS[music];"
                f"[0:a]volume=1.0,asetpts=PTS-STARTPTS[orig];"
                f"[orig][music]amix=inputs=2:duration=first:dropout_transition=0,atrim=0:{dur_s}[aout]"
            )

            try:
                _run_ffmpeg([
                    "-i", str(video_in),
                    "-i", str(music_trim),
                    "-filter_complex", mix_filter,
                    "-map", "0:v:0",
                    "-map", "[aout]",
                    "-t", f"{dur_s}",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(out_path),
                ])
            except HTTPException:
                # Fallback if video has no audio stream: just add music as audio
                _run_ffmpeg([
                    "-i", str(video_in),
                    "-i", str(music_trim),
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-t", f"{dur_s}",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(out_path),
                ])

        if not out_path.exists():
            raise HTTPException(status_code=500, detail="Output file was not produced")

        def _cleanup():
            shutil.rmtree(tmpdir, ignore_errors=True)

        return FileResponse(
            path=str(out_path),
            media_type="video/mp4",
            filename="out.mp4",
            background=BackgroundTask(_cleanup),
        )

    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail={"message": "Unhandled error", "error": str(e)})

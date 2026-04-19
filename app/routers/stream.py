import asyncio
import os
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime

from ..database import get_db, Song, PlayHistory, User, AsyncSessionLocal
from sqlalchemy.orm import selectinload
from ..routers.auth import get_current_user
from ..config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

QUALITY_PRESETS = {
    "low":      {"codec": "libmp3lame", "bitrate": "128k", "ext": "mp3"},
    "medium":   {"codec": "libmp3lame", "bitrate": "192k", "ext": "mp3"},
    "high":     {"codec": "libmp3lame", "bitrate": "320k", "ext": "mp3"},
    "lossless": None,
}


def _validate_filepath(filepath: str) -> None:
    """
    Protection contre le path traversal.
    Verifie que le fichier demande est bien dans un des dossiers musique autorises.
    Leve HTTPException 403 sinon.
    """
    try:
        resolved = Path(filepath).resolve()
        allowed = False
        for music_dir in settings.music_dirs_list:
            try:
                resolved.relative_to(Path(music_dir).resolve())
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            logger.warning(f"Path traversal bloque : {filepath}")
            raise HTTPException(status_code=403, detail="Acces refuse")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Chemin invalide")


@router.get("/{song_id}")
async def stream_song(
    song_id: int,
    request: Request,
    quality: str = "lossless",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    song = await db.get(Song, song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Titre introuvable")
    _validate_filepath(song.filepath)
    if not os.path.exists(song.filepath):
        raise HTTPException(status_code=404, detail="Fichier audio introuvable")

    asyncio.create_task(_record_play(song.id, user.id))
    preset = QUALITY_PRESETS.get(quality, None)
    if preset is None or not settings.TRANSCODING_ENABLED:
        return await _stream_file(request, song.filepath)
    return await _stream_transcoded(request, song, preset)


@router.get("/file/{song_hash}/legacy")
async def stream_by_hash(
    song_hash: str,
    request: Request,
    bitrate: str = "0",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Song).where(Song.hash == song_hash))
    song = result.scalar_one_or_none()
    if not song:
        raise HTTPException(status_code=404, detail="Titre introuvable")

    _validate_filepath(song.filepath)
    if not os.path.exists(song.filepath):
        raise HTTPException(status_code=404, detail="Fichier audio introuvable")

    asyncio.create_task(_record_play(song.id, user.id))

    br = int(bitrate) if bitrate.isdigit() else 0
    if br == 0 or not settings.TRANSCODING_ENABLED:
        return await _stream_file(request, song.filepath)
    quality = "low" if br <= 128 else "medium" if br <= 192 else "high"
    return await _stream_transcoded(request, song, QUALITY_PRESETS[quality])


async def _stream_file(request: Request, filepath: str) -> Response:
    file_size = os.path.getsize(filepath)
    content_type = _mime_for(filepath)
    range_header = request.headers.get("range")

    if range_header:
        range_val = range_header.replace("bytes=", "")
        parts = range_val.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        def iterfile():
            with open(filepath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    data = f.read(min(65536, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            iterfile(),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Cache-Control": "public, max-age=3600",
            },
        )
    else:
        def iterfile():
            with open(filepath, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        return StreamingResponse(
            iterfile(),
            media_type=content_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Cache-Control": "public, max-age=3600",
            },
        )


async def _stream_transcoded(request: Request, song: Song, preset: dict) -> StreamingResponse:
    cmd = [
        settings.FFMPEG_PATH, "-i", song.filepath,
        "-vn", "-acodec", preset["codec"],
        "-ab", preset["bitrate"], "-f", preset["ext"],
        "-loglevel", "error", "pipe:1",
    ]
    range_header = request.headers.get("range")
    if range_header and "bytes=" in range_header:
        parts = range_header.replace("bytes=", "").split("-")
        start_bytes = int(parts[0]) if parts[0] else 0
        if start_bytes > 0:
            estimated_bitrate = int(preset["bitrate"].replace("k", "")) * 1000 / 8
            seek_seconds = start_bytes / estimated_bitrate
            cmd = [settings.FFMPEG_PATH, "-ss", str(seek_seconds)] + cmd[1:]

    async def stream_ffmpeg():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            await proc.wait()

    return StreamingResponse(
        stream_ffmpeg(),
        media_type=f"audio/{preset['ext']}",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
            "X-Transcoded": "true",
        },
    )


async def _record_play(song_id: int, user_id: int):
    """Enregistre la lecture et declenche le scrobble Last.fm"""
    try:
        async with AsyncSessionLocal() as db:
            db.add(PlayHistory(song_id=song_id, user_id=user_id))
            await db.execute(
                update(Song)
                .where(Song.id == song_id)
                .values(play_count=Song.play_count + 1, last_played=datetime.utcnow())
            )
            await db.commit()
        # Scrobble Last.fm apres enregistrement
        if settings.lastfm_enabled:
            asyncio.create_task(_scrobble(song_id))
    except Exception as e:
        logger.debug(f"Erreur historique: {e}")


async def _scrobble(song_id: int):
    """Scrobble sur Last.fm"""
    try:
        from ..routers.extras import scrobble_track
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Song)
                .options(selectinload(Song.artist), selectinload(Song.album))
                .where(Song.id == song_id))
            song = result.scalar_one_or_none()
            if song:
                await scrobble_track(song)
    except Exception as e:
        logger.debug(f"Scrobble error: {e}")


def _mime_for(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    return {
        ".mp3":  "audio/mpeg",
        ".flac": "audio/flac",
        ".wav":  "audio/wav",
        ".aiff": "audio/aiff",
        ".aif":  "audio/aiff",
        ".m4a":  "audio/mp4",
        ".aac":  "audio/aac",
        ".ogg":  "audio/ogg",
        ".opus": "audio/opus",
    }.get(ext, "audio/mpeg")

@router.get("/download/{song_hash}")
async def download_track(
    song_hash: str,
    db:   AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Téléchargement complet du fichier audio (pour lecture offline)."""
    from fastapi.responses import FileResponse
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Song)
        .options(selectinload(Song.artist))
        .where(Song.hash == song_hash)
    )
    song = result.scalar_one_or_none()
    if not song:
        raise HTTPException(404, "Titre introuvable")

    _validate_filepath(song.filepath)

    if not os.path.exists(song.filepath):
        raise HTTPException(404, "Fichier audio introuvable sur le disque")

    filename = os.path.basename(song.filepath)
    # Nom propre : "Artiste - Titre.ext" (artist déjà chargé via selectinload)
    ext = os.path.splitext(filename)[1]
    artist_name = song.artist.name if song.artist else ""
    if artist_name and song.title:
        safe_name = f"{artist_name} - {song.title}{ext}".replace("/", "_")
    else:
        safe_name = filename

    return FileResponse(
        path=song.filepath,
        media_type="audio/mpeg",
        filename=safe_name,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )

@router.get("/waveform/{song_hash}")
async def get_waveform(
    song_hash: str,
    db:   AsyncSession = Depends(get_db),
    _:    User = Depends(get_current_user),
    settings_cfg = Depends(lambda: None),
):
    """Retourne les peaks audio normalisés (100 points) pour la seekbar."""
    from fastapi.responses import JSONResponse
    import json as _json

    result = await db.execute(select(Song).where(Song.hash == song_hash))
    song = result.scalar_one_or_none()
    if not song:
        raise HTTPException(404, "Titre introuvable")

    # Chercher en cache
    from ..config import settings as cfg
    cache_path = os.path.join(cfg.CACHE_DIR, f"waveform_{song_hash}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return JSONResponse(_json.load(f))

    # Générer avec ffmpeg
    _validate_filepath(song.filepath)
    peaks = await _generate_waveform(song.filepath, cache_path)
    return JSONResponse({"peaks": peaks})


async def _generate_waveform(filepath: str, cache_path: str) -> list:
    """Génère 100 peaks normalisés via ffmpeg."""
    import asyncio, json as _json
    try:
        cmd = [
            "ffmpeg", "-i", filepath,
            "-af", "aresample=8000,asetnsamples=n=8000:p=0",
            "-ac", "1", "-f", "data", "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if not raw:
            return [0.0] * 100

        import struct
        samples = struct.unpack(f"{len(raw)}b", raw)
        # Découper en 100 chunks et prendre le max absolu
        n     = len(samples)
        chunk = max(1, n // 100)
        peaks = []
        for i in range(100):
            start = i * chunk
            end   = min(start + chunk, n)
            chunk_max = max(abs(s) for s in samples[start:end]) if start < n else 0
            peaks.append(round(chunk_max / 128, 3))

        # Sauvegarder en cache
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            _json.dump({"peaks": peaks}, f)

        return peaks
    except Exception as e:
        import logging; logging.getLogger(__name__).debug(f"waveform error: {e}")
        return [0.0] * 100

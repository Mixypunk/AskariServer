import asyncio
"""
Routes images et compatibilite Swing Music
"""
import os
import logging
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from ..config import settings

compat_router = APIRouter()
logger = logging.getLogger(__name__)

# Extensions et noms de pochettes a chercher dans les dossiers (comme Swing Music)
IMG_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
IMG_HIERARCHY   = ["cover", "front", "folder", "album", "artwork", "back"]


def _webp_response(path: str) -> Response:
    if os.path.exists(path):
        return FileResponse(path, media_type="image/webp")
    return Response(status_code=404)


def _find_folder_cover(audio_filepath: str) -> str | None:
    """
    Cherche une pochette dans le meme dossier que le fichier audio.
    Ordre : cover.jpg > front.jpg > folder.jpg > album.jpg > premiere image trouvee
    (exactement comme Swing Music)
    """
    try:
        folder = Path(audio_filepath).parent
        if not folder.exists():
            return None

        images = [f for f in folder.iterdir() if f.suffix.lower() in IMG_EXTENSIONS]
        if not images:
            return None

        # Priorite par nom
        for name in IMG_HIERARCHY:
            for img in images:
                if img.stem.lower().startswith(name):
                    return str(img)

        # Sinon premiere image
        return str(images[0])
    except Exception as e:
        logger.debug(f"find_folder_cover error: {e}")
        return None


def _cache_cover(audio_filepath: str, thumb_hash: str) -> str | None:
    """
    Extrait et met en cache la pochette d'un fichier audio ou de son dossier.
    Retourne le chemin du fichier cache cree, ou None.
    """
    try:
        from PIL import Image
        import io
        import mutagen
        from mutagen.id3 import ID3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4

        img_data = None
        ext = Path(audio_filepath).suffix.lower()

        # 1. Essai tags embarques
        try:
            if ext == ".mp3":
                tags = ID3(audio_filepath)
                for tag in tags.values():
                    if hasattr(tag, "FrameID") and tag.FrameID == "APIC":
                        img_data = tag.data
                        break
            elif ext == ".flac":
                audio = FLAC(audio_filepath)
                if audio.pictures:
                    img_data = audio.pictures[0].data
            elif ext in (".m4a", ".aac"):
                audio = MP4(audio_filepath)
                if "covr" in audio.tags:
                    img_data = bytes(audio.tags["covr"][0])
        except Exception:
            pass

        # 2. Fallback : image dans le dossier
        if not img_data:
            folder_cover = _find_folder_cover(audio_filepath)
            if folder_cover:
                img = Image.open(folder_cover).convert("RGB")
            else:
                return None
        else:
            img = Image.open(io.BytesIO(img_data)).convert("RGB")

        # Sauvegarder en cache
        os.makedirs(settings.CACHE_DIR, exist_ok=True)
        thumb_path = os.path.join(settings.CACHE_DIR, f"{thumb_hash}.webp")
        thumb = img.copy()
        thumb.thumbnail((300, 300))
        thumb.save(thumb_path, "WEBP", quality=85)

        # Version HD
        hd_path = os.path.join(settings.CACHE_DIR, f"{thumb_hash}_hd.webp")
        if not os.path.exists(hd_path):
            hd = img.copy()
            hd.thumbnail((600, 600))
            hd.save(hd_path, "WEBP", quality=92)

        return thumb_path

    except Exception as e:
        logger.debug(f"cache_cover error for {audio_filepath}: {e}")
        return None


async def _get_or_cache_thumbnail(image_hash: str) -> str | None:
    """
    Cherche le thumbnail en cache, sinon le genere a partir de la DB.
    """
    thumb_path = os.path.join(settings.CACHE_DIR, f"{image_hash}.webp")
    if os.path.exists(thumb_path):
        return thumb_path

    # Chercher un fichier audio avec ce image_hash dans la DB
    try:
        from ..database import AsyncSessionLocal, Song
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Song)
                .options(selectinload(Song.artist), selectinload(Song.album))
                .where(Song.image_hash == image_hash)
                .limit(1)
            )
            song = result.scalar_one_or_none()
            if not song:
                # Essai par album hash
                from ..database import Album
                alb_r = await db.execute(
                    select(Album).where(Album.hash == image_hash).limit(1))
                album = alb_r.scalar_one_or_none()
                if album:
                    # Prendre le premier titre de l'album
                    s_r = await db.execute(
                        select(Song).where(Song.album_id == album.id).limit(1))
                    song = s_r.scalar_one_or_none()

            if song and song.filepath and os.path.exists(song.filepath):
                return _cache_cover(song.filepath, image_hash)
    except Exception as e:
        logger.debug(f"get_or_cache_thumbnail error: {e}")

    return None


# ── ROUTES IMAGES ─────────────────────────────────────────────────────────────

@compat_router.get("/img/thumbnail/{image_hash:path}")
async def get_thumbnail(image_hash: str):
    """Sert la pochette d'un titre ou album"""
    # Nettoyer le hash (enlever .webp et query params)
    clean = image_hash.split("?")[0].replace(".webp", "").strip("/")

    thumb_path = os.path.join(settings.CACHE_DIR, f"{clean}.webp")
    if os.path.exists(thumb_path):
        return FileResponse(thumb_path, media_type="image/webp")

    # Generer si possible
    generated = await _get_or_cache_thumbnail(clean)
    if generated and os.path.exists(generated):
        return FileResponse(generated, media_type="image/webp")

    return Response(status_code=404)


@compat_router.get("/img/artist/small/{artist_hash:path}")
async def get_artist_image_small(artist_hash: str):
    return await _serve_artist_image(artist_hash)


@compat_router.get("/img/artist/medium/{artist_hash:path}")
async def get_artist_image_medium(artist_hash: str):
    return await _serve_artist_image(artist_hash)


@compat_router.get("/img/artist/{artist_hash:path}")
async def get_artist_image(artist_hash: str):
    return await _serve_artist_image(artist_hash)


async def _serve_artist_image(artist_hash: str) -> Response:
    # Nettoyer le hash : enlever .webp, query params, slashes
    clean = artist_hash.split("?")[0].strip("/")
    if clean.endswith(".webp"):
        clean = clean[:-5]
    
    logger.debug(f"Artist image request: original={artist_hash!r} clean={clean!r}")

    # 1. Cache existant
    path = os.path.join(settings.CACHE_DIR, f"artist_{clean}.webp")
    if os.path.exists(path):
        return FileResponse(path, media_type="image/webp")

    # 2. Generer depuis Deezer / Last.fm / avatar
    await _generate_artist_image(clean, path)
    if os.path.exists(path):
        return FileResponse(path, media_type="image/webp")

    # 3. Fallback : pochette du premier album de l'artiste
    try:
        from ..database import AsyncSessionLocal, Artist, Song
        from ..scanner import make_hash, make_artist_hash
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Artist))
            artists = result.scalars().all()
            artist = next((a for a in artists if make_artist_hash(a.name) == clean), None)
            if artist:
                song_r = await db.execute(
                    select(Song).where(Song.artist_id == artist.id).limit(1))
                song = song_r.scalar_one_or_none()
                if song and song.image_hash:
                    thumb = os.path.join(settings.CACHE_DIR, f"{song.image_hash}.webp")
                    if os.path.exists(thumb):
                        logger.debug(f"Artist fallback to album cover: {thumb}")
                        return FileResponse(thumb, media_type="image/webp")
                    # Generer la pochette si manquante
                    generated = await _get_or_cache_thumbnail(song.image_hash)
                    if generated and os.path.exists(generated):
                        return FileResponse(generated, media_type="image/webp")
    except Exception as e:
        logger.debug(f"artist fallback error: {e}")

    logger.debug(f"No image found for artist hash: {clean}")
    return Response(status_code=404)


async def _generate_artist_image(artist_hash: str, save_path: str):
    """
    Tente de trouver une image artiste :
    1. Last.fm (si cle configuree)
    2. Avatar colore avec initiales
    """
    try:
        from ..database import AsyncSessionLocal, Artist
        from ..scanner import make_hash, make_artist_hash
        from ..config import settings as cfg
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Artist))
            artists = result.scalars().all()
            artist = next((a for a in artists if make_artist_hash(a.name) == artist_hash), None)
            if not artist:
                return

        name = artist.name
        os.makedirs(os.path.dirname(save_path) or settings.CACHE_DIR, exist_ok=True)

        import httpx
        from PIL import Image
        import io

        async with httpx.AsyncClient(timeout=10,
            headers={"User-Agent": "AskariServer/1.0"}) as client:

            # 1. Deezer API — gratuit, sans cle API, retourne de vraies photos
            try:
                r = await client.get(
                    "https://api.deezer.com/search/artist",
                    params={"q": name, "limit": 1},
                )
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("data", [])
                    if items:
                        img_url = items[0].get("picture_medium") or items[0].get("picture")
                        if img_url and "default" not in img_url:
                            ir = await client.get(img_url)
                            if ir.status_code == 200 and len(ir.content) > 1000:
                                img_bytes = ir.content
                                await asyncio.get_event_loop().run_in_executor(
                                    None, _save_image_bytes, img_bytes, save_path)
                                logger.debug(f"Deezer image OK: {name}")
                                return
            except Exception as e:
                logger.debug(f"Deezer error for {name}: {e}")

            # 2. Last.fm (si cle configuree)
            if cfg.LASTFM_API_KEY:
                try:
                    r = await client.get(
                        "https://ws.audioscrobbler.com/2.0/",
                        params={"method": "artist.getinfo", "artist": name,
                                "api_key": cfg.LASTFM_API_KEY, "format": "json"},
                    )
                    if r.status_code == 200:
                        images = r.json().get("artist", {}).get("image", [])
                        for img in reversed(images):
                            url = img.get("#text", "")
                            if url and "2a96cbd8b46e442fc41c2b86b821562f" not in url:
                                ir = await client.get(url)
                                if ir.status_code == 200 and len(ir.content) > 1000:
                                    img_bytes = ir.content
                                    await asyncio.get_event_loop().run_in_executor(
                                        None, _save_image_bytes, img_bytes, save_path)
                                    return
                except Exception:
                    pass

        # 3. Avatar colore en dernier recours
        _make_avatar(name, save_path)

    except Exception as e:
        logger.debug(f"generate_artist_image error: {e}")


def _save_image_bytes(img_bytes: bytes, save_path: str) -> None:
    """Sauvegarde une image depuis des bytes (sync, pour run_in_executor)"""
    try:
        from PIL import Image
        import io
        img_obj = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_obj.thumbnail((300, 300))
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        img_obj.save(save_path, "WEBP", quality=85)
    except Exception as e:
        logger.debug(f"_save_image_bytes error: {e}")


def _make_avatar(name: str, save_path: str):
    """Genere un avatar colore solide (garanti sans dependance police)"""
    try:
        from PIL import Image, ImageDraw
        import hashlib

        COLORS = [
            (124, 106, 245), (29, 158, 117), (216, 90, 48),
            (212, 83, 126), (55, 138, 221), (99, 153, 34),
            (186, 117, 23), (163, 45, 45),
        ]
        h = int(hashlib.md5(name.encode()).hexdigest()[:6], 16)
        r, g, b = COLORS[h % len(COLORS)]

        size = 300
        img = Image.new("RGB", (size, size), (r, g, b))
        draw = ImageDraw.Draw(img)

        # Cercle plus clair au centre pour simuler un avatar
        margin = size // 4
        draw.ellipse(
            [margin, margin, size - margin, size - margin],
            fill=(min(r + 40, 255), min(g + 40, 255), min(b + 40, 255))
        )

        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        img.save(save_path, "WEBP", quality=85)
        logger.debug(f"Avatar genere: {save_path}")
    except Exception as e:
        logger.warning(f"make_avatar error: {e}")


@compat_router.get("/img/playlist/{image_hash:path}")
async def get_playlist_image(image_hash: str):
    clean = image_hash.split("?")[0].replace(".webp", "").strip("/")
    path = os.path.join(settings.CACHE_DIR, f"playlist_{clean}.webp")
    return _webp_response(path)

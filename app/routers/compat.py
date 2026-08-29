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




def _webp_response(path: str) -> Response:
    if os.path.exists(path):
        return FileResponse(path, media_type="image/webp")
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

    # 3. If all fails (which it shouldn't because of avatar), return 404
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

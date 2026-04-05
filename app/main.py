"""
Askaria Server — Serveur de streaming musical
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db, get_db
from .config import settings
from .database import User

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Validation SECRET_KEY ──────────────────────────────────────────────
    if settings.secret_key_is_default:
        logger.critical(
            "SECRET_KEY par defaut detectee ! "
            "Generez une vraie cle : openssl rand -hex 32 "
            "et ajoutez-la dans le docker-compose sous SECRET_KEY."
        )
        raise RuntimeError(
            "SECRET_KEY non configuree — arret du serveur. "
            "Ajoutez une cle generee avec : openssl rand -hex 32"
        )
        # En dev on continue, en prod on pourrait lever une exception
        # raise RuntimeError("SECRET_KEY non configuree")

    logger.info("Askaria Server demarrage...")
    os.makedirs(settings.CACHE_DIR, exist_ok=True)
    await init_db()

    if settings.AUTO_SCAN_ON_START:
        from .scanner import scanner
        asyncio.create_task(scanner.scan_all())

    asyncio.create_task(_pregenerate_artist_avatars())
    yield
    logger.info("Askaria Server arret propre")


app = FastAPI(
    title="Askaria Server",
    version="1.1.0",
    description="Serveur de streaming musical personnel",
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes publiques (avant les routers — pas d'auth requise) ──────────────────
@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "version": "1.1.0", "name": "Askaria"}

@app.get("/ping", tags=["System"])
async def ping():
    return "pong"

# ── Routers ───────────────────────────────────────────────────────────────────
from .routers.auth    import router as auth_router
from .routers.library import (songs_router, albums_router, artists_router,
                               search_router, playlists_router)
from .routers.extras  import (lyrics_router, stats_router, scan_router,
                               users_router, favourites_router, lastfm_router,
                               radio_router)
from .routers.stream  import router as stream_router
from .routers.compat  import compat_router
from .routers.websocket import router as ws_router

app.include_router(auth_router,       prefix="/auth",   tags=["Auth"])
app.include_router(users_router,                        tags=["Users"])
app.include_router(songs_router,                        tags=["Songs"])
app.include_router(albums_router,                       tags=["Albums"])
app.include_router(artists_router,                      tags=["Artists"])
app.include_router(playlists_router,                    tags=["Playlists"])
app.include_router(search_router,                       tags=["Search"])
app.include_router(lyrics_router,                       tags=["Lyrics"])
app.include_router(stats_router,                        tags=["Stats"])
app.include_router(scan_router,                         tags=["Scan"])
app.include_router(favourites_router,                   tags=["Favourites"])
app.include_router(radio_router,                        tags=["Radio"])
app.include_router(stream_router,    prefix="/stream",  tags=["Stream"])
# Routes download et waveform accessibles sans prefix (compatibilité app mobile)
app.include_router(stream_router,    prefix="",         tags=["Stream"], include_in_schema=False)
app.include_router(compat_router,                       tags=["Compat"])
app.include_router(ws_router,                           tags=["WebSocket"])
app.include_router(lastfm_router,                       tags=["LastFM"])

# ── Route compat stream sans prefix (app mobile) ──────────────────────────────
from .routers.stream import stream_by_hash as _stream_by_hash
from sqlalchemy.ext.asyncio import AsyncSession
from .routers.auth import get_current_user

@app.get("/file/{song_hash}/legacy", tags=["Stream"])
async def stream_compat(
    song_hash: str, request: Request, bitrate: str = "0",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _stream_by_hash(
        song_hash=song_hash, request=request,
        bitrate=bitrate, db=db, user=user)

# ── Fichiers statiques ────────────────────────────────────────────────────────
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/admin", include_in_schema=False)
async def admin_ui():
    p = os.path.join(static_dir, "admin.html")
    return FileResponse(p) if os.path.exists(p) else JSONResponse({"error": "introuvable"}, status_code=404)

@app.get("/", include_in_schema=False)
async def web_player():
    p = os.path.join(static_dir, "index.html")
    return FileResponse(p) if os.path.exists(p) else JSONResponse({"name": "Askaria", "version": "1.1.0"})


async def _pregenerate_artist_avatars():
    """Pre-genere les avatars pour tous les artistes sans image"""
    await asyncio.sleep(8)  # Attendre que le scan demarre
    try:
        from .database import AsyncSessionLocal, Artist
        from .scanner import make_artist_hash
        from .routers.compat import _generate_artist_image
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Artist))
            artists = result.scalars().all()

        count = 0
        for artist in artists:
            h = make_artist_hash(artist.name)
            path = os.path.join(settings.CACHE_DIR, f"artist_{h}.webp")
            if not os.path.exists(path):
                await _generate_artist_image(h, path)
                await asyncio.sleep(0.05)
                count += 1

        if count:
            logger.info(f"Avatars pre-generes : {count} artistes")
    except Exception as e:
        logger.debug(f"pregenerate avatars error: {e}")


@app.exception_handler(404)
async def not_found(request, exc):
    # Ne pas intercepter les routes statiques
    if request.url.path.startswith("/static"):
        return JSONResponse({"error": "Fichier non trouve"}, status_code=404)
    return JSONResponse({"error": "Non trouve"}, status_code=404)

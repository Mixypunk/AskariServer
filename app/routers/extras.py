"""
Routers : lyrics, stats, scan, users, favourites, lastfm
"""
import os
import re
import httpx
import logging
import asyncio
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, delete
from sqlalchemy.orm import selectinload
from datetime import datetime
from pydantic import BaseModel, field_validator
from typing import Optional
import bcrypt

from ..database import (get_db, Song, Album, SongArtist, LyricsCache, PlayHistory,
                         User, Favourite, Artist, AsyncSessionLocal, Playlist, PlaylistEntry)
from ..routers.auth import get_current_user, require_admin, create_token
from ..config import settings
from ..scanner import make_hash, make_artist_hash

logger = logging.getLogger(__name__)


# ── LRC PARSER ────────────────────────────────────────────────────────────────
def parse_lrc(text: str) -> dict:
    """
    Parse LRC → format Swing Music attendu par l'app mobile.
    Synced  : {"lyrics": [{"time": ms, "text": "..."},...], "synced": True}
    Unsynced: {"lyrics": ["ligne1",...],"synced": False}
    """
    if not text:
        return {"lyrics": [], "synced": False}

    time_re = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")
    synced, plain = [], []

    for line in text.splitlines():
        m = time_re.match(line.strip())
        if m:
            ms = int((int(m.group(1)) * 60 + float(m.group(2))) * 1000)
            synced.append({"time": ms, "text": m.group(3).strip()})
        else:
            s = line.strip()
            # Ignorer meta-tags LRC [ar:...] [ti:...] et lignes vides
            if s and not re.match(r"\[\w[\w\s]*:", s):
                plain.append(s)

    if synced:
        return {"lyrics": synced, "synced": True}
    if plain:
        return {"lyrics": plain, "synced": False}
    return {"lyrics": [], "synced": False}


# ── DETECTION DES PAROLES ──────────────────────────────────────────────────────
def get_lyrics_from_filepath(filepath: str) -> Optional[str]:
    """
    Cherche les paroles pour un fichier audio dans l'ordre :
    1. Fichier .lrc dans le meme dossier (meme nom)
    2. Tags USLT embarques (MP3)
    3. Tags LYRICS generiques (FLAC, OGG...)

    Retourne le texte brut ou None.
    """
    if not filepath:
        return None

    # 1. Fichier .lrc — plusieurs strategies de chemin
    try:
        p = Path(filepath)
        lrc_candidates = [
            p.with_suffix(".lrc"),
            p.with_suffix(".LRC"),
        ]
        for lrc in lrc_candidates:
            if lrc.exists():
                text = lrc.read_text(encoding="utf-8", errors="ignore").strip()
                if len(text) > 10:
                    logger.debug(f"LRC file found: {lrc}")
                    return text
    except Exception as e:
        logger.debug(f"LRC file check error for {filepath}: {e}")

    # 2. Tags embarques
    try:
        import mutagen
        # ID3 USLT (MP3)
        try:
            from mutagen.id3 import ID3
            tags = ID3(filepath)
            for key, tag in tags.items():
                if key.startswith("USLT"):
                    text = str(tag.text) if hasattr(tag, "text") else str(tag)
                    if len(text) > 10:
                        return text
        except Exception:
            pass

        # Tags generiques FLAC / OGG / M4A
        audio = mutagen.File(filepath, easy=False)
        if audio:
            for key in ["lyrics", "LYRICS", "©lyr", "----:com.apple.iTunes:Lyrics"]:
                val = audio.get(key)
                if val:
                    text = str(val[0]) if isinstance(val, list) else str(val)
                    if len(text) > 10:
                        return text
    except Exception as e:
        logger.debug(f"Tag lyrics check error for {filepath}: {e}")

    return None


# ── LRCLIB ────────────────────────────────────────────────────────────────────
async def fetch_lrclib(title: str, artist: str, album: str, duration: int) -> Optional[dict]:
    """Telechargement depuis lrclib.net (gratuit, sans cle API)"""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{settings.LRCLIB_BASE}/get",
                params={"track_name": title, "artist_name": artist,
                        "album_name": album, "duration": duration},
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("syncedLyrics"):
                    return {"content": data["syncedLyrics"], "synced": True}
                if data.get("plainLyrics"):
                    return {"content": data["plainLyrics"], "synced": False}
    except Exception as e:
        logger.debug(f"lrclib error: {e}")
    return None


async def save_lyrics_cache(db, song_id: int, content: Optional[str],
                             synced: bool, source: str):
    cache_r = await db.execute(
        select(LyricsCache).where(LyricsCache.song_id == song_id))
    cache = cache_r.scalar_one_or_none()
    if cache:
        cache.content  = content
        cache.synced   = synced
        cache.source   = source
        cache.cached_at = datetime.utcnow()
    else:
        db.add(LyricsCache(song_id=song_id, content=content,
                            synced=synced, source=source))
    await db.commit()


# ── LYRICS ENDPOINT ───────────────────────────────────────────────────────────
lyrics_router = APIRouter()

class LyricsRequest(BaseModel):
    trackhash: str
    filepath: str = ""

@lyrics_router.post("/lyrics")
async def get_lyrics(
    body: LyricsRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    song_r = await db.execute(
        select(Song)
        .options(
            selectinload(Song.artist),
            selectinload(Song.album),
            selectinload(Song.song_artists).selectinload(SongArtist.artist),
        )
        .where(Song.hash == body.trackhash)
    )
    song = song_r.scalar_one_or_none()
    if not song:
        raise HTTPException(404, "Titre introuvable")

    # 1. Fichier .lrc ou tags embarques (priorite absolue)
    # Essai avec le filepath de la requete ET celui en DB
    # run_in_executor car mutagen est synchrone (I/O disque)
    loop = asyncio.get_event_loop()
    for fp in set(filter(None, [body.filepath, song.filepath])):
        raw = await loop.run_in_executor(None, get_lyrics_from_filepath, fp)
        if raw:
            parsed = parse_lrc(raw)
            parsed["source"] = "lrc_file"
            await save_lyrics_cache(db, song.id, raw, parsed["synced"], "lrc_file")
            return parsed

    # 2. Cache DB (lrclib precedemment telecharge)
    cache_r = await db.execute(
        select(LyricsCache).where(LyricsCache.song_id == song.id))
    cache = cache_r.scalar_one_or_none()
    if cache and cache.content and cache.source != "not_found":
        age = (datetime.utcnow() - cache.cached_at).days
        if age < settings.LYRICS_CACHE_DAYS:
            parsed = parse_lrc(cache.content)
            parsed["source"] = cache.source
            return parsed

    # 3. lrclib.net
    artist_name = song.artist.name if song.artist else ""
    album_title  = song.album.title  if song.album  else ""
    lrclib = await fetch_lrclib(song.title, artist_name, album_title, song.duration)
    if lrclib:
        await save_lyrics_cache(db, song.id, lrclib["content"], lrclib["synced"], "lrclib")
        parsed = parse_lrc(lrclib["content"])
        parsed["source"] = "lrclib"
        return parsed

    await save_lyrics_cache(db, song.id, None, False, "not_found")
    return {"lyrics": None, "synced": False, "source": None}


# ── STATS ─────────────────────────────────────────────────────────────────────
stats_router = APIRouter()

@stats_router.get("/stats/overview")
async def stats_overview(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Résumé global : bibliothèque + écoutes de l'utilisateur."""
    total_songs  = (await db.execute(select(func.count(Song.id)))).scalar() or 0
    dur_secs     = (await db.execute(select(func.sum(Song.duration)))).scalar() or 0
    total_albums = (await db.execute(select(func.count(Album.id)))).scalar() or 0
    total_artists= (await db.execute(select(func.count(Artist.id)))).scalar() or 0
    # Lectures de cet utilisateur
    total_plays  = (await db.execute(
        select(func.count(PlayHistory.id))
        .where(PlayHistory.user_id == user.id)
    )).scalar() or 0
    # Temps d'écoute total (secondes) de cet utilisateur
    listen_secs  = (await db.execute(
        select(func.sum(Song.duration))
        .join(PlayHistory, PlayHistory.song_id == Song.id)
        .where(PlayHistory.user_id == user.id)
    )).scalar() or 0
    return {
        "total_songs":   total_songs,
        "total_albums":  total_albums,
        "total_artists": total_artists,
        "total_seconds": dur_secs,
        "total_plays":   total_plays,
        "listen_seconds": listen_secs,
        "listen_hours":  round(listen_secs / 3600, 1),
    }


@stats_router.get("/stats/top-tracks")
async def top_tracks(
    limit: int = 10,
    period: str = "all",   # all | week | month
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Top titres les plus écoutés par l'utilisateur."""
    from datetime import timedelta
    from .library import song_to_dict

    # Filtre période
    cutoff = None
    if period == "week":
        cutoff = datetime.utcnow() - timedelta(days=7)
    elif period == "month":
        cutoff = datetime.utcnow() - timedelta(days=30)

    q = (
        select(Song, func.count(PlayHistory.id).label("plays"))
        .join(PlayHistory, PlayHistory.song_id == Song.id)
        .options(
            selectinload(Song.artist),
            selectinload(Song.album),
            selectinload(Song.song_artists).selectinload(SongArtist.artist),
        )
        .where(PlayHistory.user_id == user.id)
    )
    if cutoff:
        q = q.where(PlayHistory.played_at >= cutoff)
    q = q.group_by(Song.id).order_by(desc("plays")).limit(limit)

    rows = (await db.execute(q)).all()
    items = []
    for song, plays in rows:
        d = song_to_dict(song)
        d["user_plays"] = plays
        items.append(d)
    return {"items": items, "period": period}


@stats_router.get("/stats/top-artists")
async def top_artists(
    limit: int = 10,
    period: str = "all",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Top artistes les plus écoutés par l'utilisateur."""
    from datetime import timedelta

    cutoff = None
    if period == "week":
        cutoff = datetime.utcnow() - timedelta(days=7)
    elif period == "month":
        cutoff = datetime.utcnow() - timedelta(days=30)

    q = (
        select(Artist, func.count(PlayHistory.id).label("plays"))
        .join(Song,        Song.artist_id == Artist.id)
        .join(PlayHistory, PlayHistory.song_id == Song.id)
        .where(PlayHistory.user_id == user.id)
    )
    if cutoff:
        q = q.where(PlayHistory.played_at >= cutoff)
    q = q.group_by(Artist.id).order_by(desc("plays")).limit(limit)

    rows = (await db.execute(q)).all()
    return {"items": [
        {"name": a.name, "artisthash": make_artist_hash(a.name), "plays": p}
        for a, p in rows
    ], "period": period}


@stats_router.get("/stats/history")
async def play_history(
    limit: int = 30,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Historique des dernières lectures avec infos complètes."""
    result = await db.execute(
        select(PlayHistory, Song)
        .join(Song, Song.id == PlayHistory.song_id)
        .options(
            selectinload(Song.artist),
            selectinload(Song.album),
            selectinload(Song.song_artists).selectinload(SongArtist.artist),
        )
        .where(PlayHistory.user_id == user.id)
        .order_by(desc(PlayHistory.played_at))
        .limit(limit)
    )
    from .library import song_to_dict
    items = []
    for history, song in result.all():
        d = song_to_dict(song)
        d["played_at"] = history.played_at.isoformat()
        items.append(d)
    return {"items": items}


@stats_router.get("/stats/heatmap")
async def listening_heatmap(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Distribution des écoutes par heure de la journée (0-23)."""
    result = await db.execute(
        select(
            func.extract("hour", PlayHistory.played_at).label("hour"),
            func.count(PlayHistory.id).label("plays"),
        )
        .where(PlayHistory.user_id == user.id)
        .group_by("hour")
        .order_by("hour")
    )
    rows = result.all()
    # Remplir les 24h avec 0 si pas de données
    counts = {int(r.hour): r.plays for r in rows}
    return {"hours": [{"hour": h, "plays": counts.get(h, 0)} for h in range(24)]}


@stats_router.get("/stats/genres")
async def top_genres(
    limit: int = 8,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Top genres les plus écoutés par l'utilisateur."""
    result = await db.execute(
        select(Song.genre, func.count(PlayHistory.id).label("plays"))
        .join(PlayHistory, PlayHistory.song_id == Song.id)
        .where(PlayHistory.user_id == user.id)
        .where(Song.genre.isnot(None))
        .where(Song.genre != "")
        .group_by(Song.genre)
        .order_by(desc("plays"))
        .limit(limit)
    )
    return {"items": [{"genre": r.genre, "plays": r.plays} for r in result.all()]}


@stats_router.get("/stats/daily")
async def daily_stats(
    days: int = 7,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Nombre d'écoutes par jour sur les N derniers jours."""
    from datetime import timedelta, date
    cutoff = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(
            func.date(PlayHistory.played_at).label("day"),
            func.count(PlayHistory.id).label("plays"),
        )
        .where(PlayHistory.user_id == user.id)
        .where(PlayHistory.played_at >= cutoff)
        .group_by("day")
        .order_by("day")
    )
    rows = result.all()
    # Remplir les jours manquants
    counts = {str(r.day): r.plays for r in rows}
    result_days = []
    for i in range(days):
        d = (datetime.utcnow() - timedelta(days=days - 1 - i)).date()
        result_days.append({"date": str(d), "plays": counts.get(str(d), 0)})
    return {"items": result_days, "days": days}


# ── SCAN ──────────────────────────────────────────────────────────────────────
scan_router = APIRouter()

@scan_router.post("/scan/start")
async def start_scan(incremental: bool = False, _: User = Depends(require_admin)):
    from ..scanner import scanner
    asyncio.create_task(scanner.scan_all(incremental=incremental))
    return {"ok": True, "mode": "incremental" if incremental else "complet"}

@scan_router.get("/scan/status")
async def scan_status(_: User = Depends(get_current_user)):
    from ..scanner import scanner
    return {"scanning": scanner.is_scanning, "progress": scanner.progress}

@scan_router.post("/scan/lyrics")
async def fetch_missing_lyrics(_: User = Depends(require_admin)):
    """Force le telechargement des paroles manquantes pour toute la bibliotheque"""
    asyncio.create_task(_fetch_all_lyrics())
    return {"ok": True, "message": "Paroles en cours de telechargement..."}

@scan_router.delete("/scan/lyrics/cache")
async def clear_lyrics_cache(_: User = Depends(require_admin)):
    """Vide le cache paroles pour forcer un nouveau telechargement"""
    async with AsyncSessionLocal() as db:
        await db.execute(LyricsCache.__table__.delete())
        await db.commit()
    return {"ok": True, "message": "Cache paroles vide"}

@scan_router.delete("/scan/artists/cache")
async def clear_artist_images_cache(_: User = Depends(require_admin)):
    """Vide le cache images artistes pour forcer un re-telechargement"""
    import os, glob
    cache_dir = settings.CACHE_DIR
    deleted = 0
    for f in glob.glob(os.path.join(cache_dir, "artist_*.webp")):
        try:
            os.remove(f)
            deleted += 1
        except Exception:
            pass
    return {"ok": True, "deleted": deleted, "message": f"{deleted} images artistes supprimees"}


async def _fetch_all_lyrics():
    """
    Parcourt tous les titres et telecharge les paroles manquantes.
    Ordre de priorite : .lrc → tags embarques → lrclib.net
    """
    found = 0
    errors = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Song).options(
            selectinload(Song.artist),
            selectinload(Song.album),
            selectinload(Song.song_artists).selectinload(SongArtist.artist),
        )
        )
        all_songs = result.scalars().all()
        total = len(all_songs)
        logger.info(f"Scan paroles : {total} titres a traiter")

        for i, song in enumerate(all_songs):
            try:
                # Verifier cache existant
                cache_r = await db.execute(
                    select(LyricsCache).where(LyricsCache.song_id == song.id))
                cache = cache_r.scalar_one_or_none()

                # Skip si deja trouve (pas not_found)
                if cache and cache.content and cache.source not in ("not_found", None):
                    continue

                # 1. .lrc ou tags embarques (run_in_executor : mutagen est sync)
                loop = asyncio.get_event_loop()
                raw = await loop.run_in_executor(None, get_lyrics_from_filepath, song.filepath or "")
                if raw:
                    parsed = parse_lrc(raw)
                    await save_lyrics_cache(db, song.id, raw, parsed["synced"], "lrc_file")
                    found += 1
                    logger.debug(f"LRC trouve: {song.title}")
                    continue

                # 2. lrclib.net
                artist = song.artist.name if song.artist else ""
                album  = song.album.title  if song.album  else ""
                lrclib = await fetch_lrclib(song.title, artist, album, song.duration)
                if lrclib:
                    await save_lyrics_cache(db, song.id, lrclib["content"],
                                              lrclib["synced"], "lrclib")
                    found += 1
                else:
                    await save_lyrics_cache(db, song.id, None, False, "not_found")

                # Pause polie avec lrclib.net
                await asyncio.sleep(0.25)

            except Exception as e:
                errors += 1
                logger.debug(f"Erreur paroles '{song.title}': {e}")

    logger.info(f"Paroles terminees : {found}/{total} trouves, {errors} erreurs")


# ── USERS ─────────────────────────────────────────────────────────────────────
users_router = APIRouter()

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2 or len(v) > 50:
            raise ValueError("Le nom doit faire entre 2 et 50 caractères")
        import re
        if not re.match(r"^[\w\-\.]+$", v):
            raise ValueError("Caractères non autorisés dans le nom d'utilisateur")
        return v

    @field_validator("password")
    @classmethod
    def password_valid(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Mot de passe trop court (min 6 caractères)")
        if len(v) > 200:
            raise ValueError("Mot de passe trop long")
        return v

@users_router.get("/users/list")
async def list_users(db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin)):
    result = await db.execute(select(User))
    return {"users": [
        {"id": u.id, "username": u.username, "role": u.role,
         "is_active": u.is_active,
         "can_download": u.can_download,
         "last_seen": u.last_seen.isoformat() if u.last_seen else None}
        for u in result.scalars().all()
    ]}

@users_router.post("/users/create")
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin)):
    count_r = await db.execute(select(func.count(User.id)))
    if (count_r.scalar() or 0) >= settings.MAX_USERS:
        raise HTTPException(400, f"Limite {settings.MAX_USERS} utilisateurs atteinte")
    if (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none():
        raise HTTPException(400, "Nom d'utilisateur deja pris")
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = User(username=body.username, password=hashed, role=body.role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"ok": True,
            "user": {"id": user.id, "username": user.username, "role": user.role},
            "accesstoken": create_token(user.id, "access")}

@users_router.delete("/users/{user_id}")
async def delete_user(user_id: int, hard_delete: bool = False, db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(400, "Impossible de supprimer son propre compte")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")
    
    if hard_delete:
        # Remove user dependencies to respect foreign keys
        await db.execute(delete(PlayHistory).where(PlayHistory.user_id == user.id))
        await db.execute(delete(Favourite).where(Favourite.user_id == user.id))
        
        playlists_res = await db.execute(select(Playlist.id).where(Playlist.owner_id == user.id))
        playlist_ids = playlists_res.scalars().all()
        if playlist_ids:
            await db.execute(delete(PlaylistEntry).where(PlaylistEntry.playlist_id.in_(playlist_ids)))
            await db.execute(delete(Playlist).where(Playlist.id.in_(playlist_ids)))
            
        await db.delete(user)
    else:
        user.is_active = False
        
    await db.commit()
    return {"ok": True}


@users_router.patch("/users/{user_id}/permissions")
async def update_user_permissions(
    user_id: int,
    can_download: bool,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """[Admin] Modifie les permissions d'un utilisateur (can_download)."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")
    user.can_download = can_download
    await db.commit()
    return {"ok": True, "can_download": user.can_download}


# ── PROFIL UTILISATEUR ────────────────────────────────────────────────────────

class ProfileUpdate(BaseModel):
    username:   Optional[str] = None
    email:      Optional[str] = None
    birth_date: Optional[str] = None   # YYYY-MM-DD
    bio:        Optional[str] = None

    @field_validator("username")
    @classmethod
    def username_valid(cls, v):
        if v is None: return v
        import re
        v = v.strip()
        if len(v) < 2 or len(v) > 50:
            raise ValueError("Entre 2 et 50 caractères")
        if not re.match(r"^[\w\-\.\s]+$", v):
            raise ValueError("Caractères non autorisés")
        return v

    @field_validator("email")
    @classmethod
    def email_valid(cls, v):
        if v is None or v.strip() == "": return None
        import re
        v = v.strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Email invalide")
        if len(v) > 200:
            raise ValueError("Email trop long")
        return v

    @field_validator("birth_date")
    @classmethod
    def birth_date_valid(cls, v):
        if v is None or v == "": return None
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("Format date invalide (YYYY-MM-DD)")
        return v

    @field_validator("bio")
    @classmethod
    def bio_valid(cls, v):
        if v is None: return v
        return v.strip()[:300]

class PasswordChange(BaseModel):
    current_password: str
    new_password:     str


def _user_to_dict(u) -> dict:
    return {
        "id":         u.id,
        "username":   u.username,
        "role":       u.role,
        "can_download": u.can_download,
        "email":      u.email,
        "birth_date": u.birth_date,
        "bio":        u.bio,
        "avatar":     u.avatar,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_seen":  u.last_seen.isoformat()  if u.last_seen  else None,
    }


@users_router.get("/users/me")
async def get_my_profile(
    user: User = Depends(get_current_user),
):
    """Retourne le profil complet de l'utilisateur connecté."""
    return _user_to_dict(user)


@users_router.patch("/users/me")
async def update_my_profile(
    body: ProfileUpdate,
    db:   AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Met à jour les infos du profil (username, email, date naissance, bio)."""
    if body.username is not None:
        # Vérifier unicité du nouveau username
        existing = (await db.execute(
            select(User).where(User.username == body.username, User.id != user.id)
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(400, "Nom d'utilisateur déjà pris")
        user.username = body.username.strip()

    if body.email is not None:
        email = body.email.strip().lower() if body.email.strip() else None
        if email:
            # Vérifier unicité email
            existing = (await db.execute(
                select(User).where(User.email == email, User.id != user.id)
            )).scalar_one_or_none()
            if existing:
                raise HTTPException(400, "Email déjà utilisé")
        user.email = email

    if body.birth_date is not None:
        user.birth_date = body.birth_date or None

    if body.bio is not None:
        user.bio = body.bio[:300] if body.bio else None

    await db.commit()
    await db.refresh(user)
    return _user_to_dict(user)


@users_router.post("/users/me/password")
async def change_password(
    body: PasswordChange,
    db:   AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change le mot de passe après vérification de l'ancien."""
    if not bcrypt.checkpw(body.current_password.encode(), user.password.encode()):
        raise HTTPException(400, "Mot de passe actuel incorrect")
    if len(body.new_password) < 6:
        raise HTTPException(400, "Le nouveau mot de passe doit faire au moins 6 caractères")
    user.password = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    await db.commit()
    return {"ok": True, "message": "Mot de passe modifié"}


@users_router.post("/users/me/avatar")
async def upload_avatar(
    request: Request,
    db:      AsyncSession = Depends(get_db),
    user:    User = Depends(get_current_user),
):
    """Upload une photo de profil (JPEG/PNG/WEBP, max 5MB)."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "Aucune image fournie")
    if len(body) > 5 * 1024 * 1024:
        raise HTTPException(400, "Image trop grande (max 5MB)")

    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(body)).convert("RGB")
        # Recadrer en carré centré
        w, h = img.size
        size = min(w, h)
        left = (w - size) // 2
        top  = (h - size) // 2
        img = img.crop((left, top, left + size, top + size))
        img.thumbnail((300, 300))

        # Sauvegarder
        avatar_path = os.path.join(settings.CACHE_DIR, f"avatar_{user.id}.webp")
        img.save(avatar_path, "WEBP", quality=88)

        # Supprimer l'ancien si différent
        if user.avatar and user.avatar != avatar_path:
            try: os.remove(user.avatar)
            except: pass

        user.avatar = avatar_path
        await db.commit()
        return {"ok": True, "avatar": f"/users/me/avatar/{user.id}"}
    except Exception as e:
        raise HTTPException(500, f"Erreur traitement image : {e}")


@users_router.get("/users/me/avatar/{user_id}")
async def get_avatar(user_id: int, db: AsyncSession = Depends(get_db)):
    """Retourne la photo de profil (public — pas d'auth requise)."""
    from fastapi.responses import FileResponse, Response
    user = await db.get(User, user_id)
    if not user or not user.avatar or not os.path.exists(user.avatar):
        # Retourner un avatar généré avec les initiales
        return await _generated_avatar(user.username if user else "?")
    return FileResponse(user.avatar, media_type="image/webp")


async def _generated_avatar(username: str):
    """Génère un avatar coloré avec l'initiale de l'utilisateur."""
    from fastapi.responses import Response
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io, hashlib
        COLORS = [
            (124, 106, 245), (29, 158, 117), (216, 90, 48),
            (212, 83, 126), (55, 138, 221), (99, 153, 34),
        ]
        h = int(hashlib.md5(username.encode()).hexdigest()[:6], 16)
        r, g, b = COLORS[h % len(COLORS)]
        size = 200
        img  = Image.new("RGB", (size, size), (r, g, b))
        draw = ImageDraw.Draw(img)
        letter = username[0].upper() if username else "?"
        # Dessiner l'initiale au centre (sans police externe)
        draw.ellipse(
            [size//4, size//4, 3*size//4, 3*size//4],
            fill=(min(r+50, 255), min(g+50, 255), min(b+50, 255))
        )
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=85)
        return Response(buf.getvalue(), media_type="image/webp")
    except Exception:
        return Response(b"", media_type="image/webp")


# ── FAVOURITES ────────────────────────────────────────────────────────────────
favourites_router = APIRouter()

class FavouriteToggle(BaseModel):
    trackhash: str

@favourites_router.post("/track/favourite")
async def toggle_favourite(body: FavouriteToggle, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user)):
    song_r = await db.execute(select(Song).where(Song.hash == body.trackhash))
    song = song_r.scalar_one_or_none()
    if not song:
        raise HTTPException(404, "Titre introuvable")
    fav_r = await db.execute(
        select(Favourite).where(Favourite.user_id == user.id,
                                 Favourite.song_id == song.id))
    fav = fav_r.scalar_one_or_none()
    if fav:
        await db.delete(fav)
        action = "removed"
    else:
        db.add(Favourite(user_id=user.id, song_id=song.id))
        action = "added"
    await db.commit()
    return {"ok": True, "action": action}

@favourites_router.get("/favourites")
async def get_favourites(db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user)):
    from .library import song_to_dict
    result = await db.execute(
        select(Song)
        .options(
            selectinload(Song.artist),
            selectinload(Song.album),
            selectinload(Song.song_artists).selectinload(SongArtist.artist),
        )
        .join(Favourite, Favourite.song_id == Song.id)
        .where(Favourite.user_id == user.id))
    return {"tracks": [song_to_dict(s) for s in result.scalars().all()]}


# ── LAST.FM ───────────────────────────────────────────────────────────────────
lastfm_router = APIRouter()
_lastfm_network = None

def _get_lastfm():
    global _lastfm_network
    if not settings.lastfm_enabled:
        return None
    if _lastfm_network is None:
        try:
            import pylast
            _lastfm_network = pylast.LastFMNetwork(
                api_key=settings.LASTFM_API_KEY,
                api_secret=settings.LASTFM_API_SECRET,
                username=settings.LASTFM_USERNAME,
                password_hash=settings.LASTFM_PASSWORD_HASH,
            )
        except Exception as e:
            logger.warning(f"Last.fm init failed: {e}")
    return _lastfm_network


async def scrobble_track(song: Song):
    if not settings.lastfm_enabled:
        return
    try:
        network = _get_lastfm()
        if not network:
            return
        artist = song.artist.name if song.artist else ""
        album  = song.album.title  if song.album  else ""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: network.scrobble(
            artist=artist, title=song.title, album=album,
            timestamp=int(datetime.utcnow().timestamp()),
        ))
        logger.debug(f"Scrobble OK: {artist} - {song.title}")
    except Exception as e:
        logger.debug(f"Scrobble error: {e}")


@lastfm_router.get("/lastfm/status")
async def lastfm_status(_: User = Depends(get_current_user)):
    return {
        "enabled":  settings.lastfm_enabled,
        "username": settings.LASTFM_USERNAME if settings.lastfm_enabled else None,
    }

# ── RADIO ─────────────────────────────────────────────────────────────────────
radio_router = APIRouter()

@radio_router.get("/radio/{song_hash}")
async def get_radio(
    song_hash: str,
    limit: int = 30,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Génère un mix radio à partir d'un titre seed.
    Algorithme :
      1. Trouver le genre + artiste du titre seed
      2. Prendre des titres du même genre (60%)
      3. Compléter avec des titres populaires d'autres genres (40%)
      4. Exclure le titre seed, mélanger
    """
    from .library import song_to_dict
    import random

    # Titre seed
    seed_result = await db.execute(
        select(Song)
        .options(
            selectinload(Song.artist),
            selectinload(Song.album),
            selectinload(Song.song_artists).selectinload(SongArtist.artist),
        )
        .where(Song.hash == song_hash)
    )
    seed = seed_result.scalar_one_or_none()
    if not seed:
        raise HTTPException(404, "Titre introuvable")

    genre      = seed.genre or ""
    artist_id  = seed.artist_id
    tracks = []

    # 1. Même genre (si genre disponible)
    if genre:
        genre_q = await db.execute(
            select(Song)
            .options(
                selectinload(Song.artist),
                selectinload(Song.album),
                selectinload(Song.song_artists).selectinload(SongArtist.artist),
            )
            .where(Song.genre == genre)
            .where(Song.hash != song_hash)
            .order_by(desc(Song.play_count))
            .limit(int(limit * 0.7))
        )
        tracks += genre_q.scalars().all()

    # 2. Même artiste (si pas assez)
    if artist_id and len(tracks) < limit // 2:
        artist_q = await db.execute(
            select(Song)
            .options(
                selectinload(Song.artist),
                selectinload(Song.album),
                selectinload(Song.song_artists).selectinload(SongArtist.artist),
            )
            .where(Song.artist_id == artist_id)
            .where(Song.hash != song_hash)
            .limit(10)
        )
        for s in artist_q.scalars().all():
            if s.id not in {t.id for t in tracks}:
                tracks.append(s)

    # 3. Compléter avec des titres populaires si pas assez
    if len(tracks) < limit:
        pop_q = await db.execute(
            select(Song)
            .options(
                selectinload(Song.artist),
                selectinload(Song.album),
                selectinload(Song.song_artists).selectinload(SongArtist.artist),
            )
            .where(Song.hash != song_hash)
            .where(Song.play_count > 0)
            .order_by(desc(Song.play_count))
            .limit(limit - len(tracks))
        )
        for s in pop_q.scalars().all():
            if s.id not in {t.id for t in tracks}:
                tracks.append(s)

    # Mélanger et limiter
    random.shuffle(tracks)
    tracks = tracks[:limit]

    return {
        "seed":   song_to_dict(seed),
        "tracks": [song_to_dict(t) for t in tracks],
        "total":  len(tracks),
        "genre":  genre,
    }

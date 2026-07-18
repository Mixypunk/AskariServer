"""
Routes : songs, albums, artists, search, playlists
Compatible avec l'API attendue par l'app mobile AskaMusic
"""
import time
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, desc
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from ..database import get_db, Song, Album, Artist, SongArtist, Playlist, PlaylistEntry, Favourite, User
from ..routers.auth import get_current_user
from ..config import settings
from ..scanner import make_hash, make_artist_hash

# ── Cache memoire simple (TTL 5 minutes) ──────────────────────────────────────
_cache: dict = {}   # { key: (timestamp, data) }
_CACHE_TTL = 300    # secondes

def _cache_get(key: str):
    entry = _cache.get(key)
    if entry:
        if time.time() - entry[0] < _CACHE_TTL:
            return entry[1]
        else:
            del _cache[key]
    return None

def _cache_set(key: str, data):
    _cache[key] = (time.time(), data)

def _cache_invalidate(*prefixes: str):
    keys = [k for k in list(_cache.keys()) if any(k.startswith(p) for p in prefixes)]
    for k in keys:
        del _cache[k]


# ── Cache hash artiste → id (full table scan évité) ─────────────────────
_artist_hash_cache: dict = {}   # { artist_hash: artist_id }

def _invalidate_artist_cache():
    _artist_hash_cache.clear()


# ── SONGS ──────────────────────────────────────────────────────────────────────
songs_router = APIRouter()

LOSSLESS_FMT = {"flac", "wav", "aiff", "aif", "alac", "ape", "wv"}

def song_to_dict(s: Song, fav_ids: set = None) -> dict:
    fmt = (s.format or "").lower()

    # Multi-artiste : lire depuis song_artists si disponible, sinon fallback
    artists_list = []
    albumartists_list = []
    if hasattr(s, "song_artists") and s.song_artists:
        # Trier par position
        sorted_sa = sorted(s.song_artists, key=lambda x: x.position)
        for sa in sorted_sa:
            if sa.artist:
                entry = {"name": sa.artist.name,
                         "artisthash": make_artist_hash(sa.artist.name)}
                if sa.role in ("main", "featured"):
                    artists_list.append(entry)
                if sa.role in ("main", "albumartist"):
                    albumartists_list.append(entry)

    # Fallback si pas de song_artists chargés
    if not artists_list:
        main_artist_name = s.artist.name if s.artist else "Unknown Artist"
        main_artist_hash = make_artist_hash(main_artist_name) if s.artist else ""
        artists_list = [{"name": main_artist_name, "artisthash": main_artist_hash}]
    if not albumartists_list:
        albumartists_list = [artists_list[0]] if artists_list else []

    artist_name = ", ".join(a["name"] for a in artists_list)
    artist_hash = artists_list[0]["artisthash"] if artists_list else ""
    album_title = s.album.title if s.album else "Unknown Album"
    album_hash  = s.album.hash if s.album else ""
    return {
        "trackhash":    s.hash,
        "hash":         s.hash,
        "title":        s.title,
        "artist":       artist_name,
        "artisthash":   artist_hash,
        "artists":      artists_list,
        "albumartists": albumartists_list,
        "album":        album_title,
        "albumhash":    album_hash,
        "duration":     s.duration,
        "track":        s.track_number or 0,
        "trackno":      s.track_number or 0,
        "disc":         s.disc_number or 1,
        "year":         s.year,
        "date":         str(s.year) if s.year else None,
        "genre":        s.genre or "",
        "filepath":     s.filepath,
        "image":        s.image_hash or album_hash,
        "format":       s.format or "",
        "bitrate":      s.bitrate or 0,
        "sample_rate":  s.sample_rate or 0,
        "file_size":    s.file_size or 0,
        "is_lossless":  fmt in LOSSLESS_FMT,
        "play_count":   s.play_count or 0,
        "date_added":   s.date_added.isoformat() if s.date_added else None,
        "is_favourite": (s.id in fav_ids) if fav_ids is not None else False,
    }


@songs_router.get("/getall/songs/stats")
async def library_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    cached = _cache_get("stats")
    if cached:
        return cached
    total    = (await db.execute(select(func.count(Song.id)))).scalar() or 0
    dur_secs = (await db.execute(select(func.sum(Song.duration)))).scalar() or 0
    albums   = (await db.execute(select(func.count(Album.id)))).scalar() or 0
    artists  = (await db.execute(select(func.count(Artist.id)))).scalar() or 0
    result = {
        "total_songs":   total,
        "total_albums":  albums,
        "total_artists": artists,
        "total_seconds": dur_secs,
        "total_hours":   round(dur_secs / 3600, 1),
    }
    _cache_set("stats", result)
    return result


@songs_router.post("/folder")
async def get_songs_folder(
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """POST /folder — endpoint principal de l'app mobile"""
    start = body.get("start", 0)
    limit = body.get("limit", 500)
    result = await db.execute(
        select(Song)
        .options(selectinload(Song.artist), selectinload(Song.album),
        selectinload(Song.song_artists).selectinload(SongArtist.artist))
        .offset(start).limit(limit)
    )
    songs = result.scalars().all()
    favs = await db.execute(select(Favourite.song_id).where(Favourite.user_id == user.id))
    fav_ids = {f[0] for f in favs.all()}
    total_count = await db.scalar(select(func.count(Song.id)))
    return {"tracks": [song_to_dict(s, fav_ids) for s in songs], "total": total_count}


@songs_router.get("/getall/songs")
async def get_all_songs(
    start: int = 0,
    limit: int = 500,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # On cache les donnees statiques (sans is_favourite, car per-user)
    # puis on injecte les favoris de l'utilisateur courant
    cache_key = f"songs_base:{start}:{limit}"
    cached_songs = _cache_get(cache_key)

    favs = await db.execute(select(Favourite.song_id).where(Favourite.user_id == user.id))
    fav_ids = {f[0] for f in favs.all()}

    if cached_songs is not None:
        # Injecter is_favourite sur une copie legere (evite de muter le cache)
        items = [{**s, "is_favourite": s["_song_id"] in fav_ids} for s in cached_songs]
        total_count = await db.scalar(select(func.count(Song.id)))
        return {"items": items, "total": total_count}

    result = await db.execute(
        select(Song)
        .options(selectinload(Song.artist), selectinload(Song.album),
        selectinload(Song.song_artists).selectinload(SongArtist.artist))
        .order_by(Song.title)
        .offset(start).limit(limit)
    )
    songs = result.scalars().all()
    # Stocker avec _song_id (cle interne) pour reinjecter is_favourite sans re-query
    base_items = [dict(song_to_dict(s), _song_id=s.id) for s in songs]
    _cache_set(cache_key, base_items)
    items = [{**s, "is_favourite": s["_song_id"] in fav_ids} for s in base_items]
    total_count = await db.scalar(select(func.count(Song.id)))
    return {"items": items, "total": total_count}


# ── ALBUMS ─────────────────────────────────────────────────────────────────────
albums_router = APIRouter()

def album_to_dict(a: Album) -> dict:
    artist_name = a.artist.name if a.artist else "Unknown Artist"
    artist_hash = make_artist_hash(artist_name) if a.artist else ""
    return {
        "albumhash":    a.hash,
        "hash":         a.hash,
        "title":        a.title,
        "artist":       artist_name,
        "artisthash":   artist_hash,
        "albumartists": [{"name": artist_name, "artisthash": artist_hash}],
        "date":         str(a.year) if a.year else None,
        "count":        a.track_count,
        "trackcount":   a.track_count,
        "duration":     a.duration,
        "image":        a.hash,
    }


@albums_router.get("/getall/albums")
async def get_albums(
    start: int = 0,
    limit: int = 200,
    sortby: str = "created_date",
    reverse: str = "1",
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    cache_key = f"albums:{start}:{limit}:{sortby}:{reverse}"
    cached = _cache_get(cache_key)
    if cached:
        return {"items": cached}

    is_desc = str(reverse).lower() in ("1", "true")
    
    if sortby == "created_date":
        order_col = Album.id.desc() if is_desc else Album.id.asc()
    elif sortby == "year":
        order_col = Album.year.desc() if is_desc else Album.year.asc()
    else:
        order_col = Album.title.desc() if is_desc else Album.title.asc()

    result = await db.execute(
        select(Album)
        .options(selectinload(Album.artist))
        .order_by(order_col)
        .offset(start).limit(limit)
    )
    albums = result.scalars().all()
    items = [album_to_dict(a) for a in albums]
    _cache_set(cache_key, items)
    return {"items": items}


@albums_router.get("/album/{album_hash}/tracks")
async def get_album_tracks(
    album_hash: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Album).where(Album.hash == album_hash))
    album = result.scalar_one_or_none()
    if not album:
        raise HTTPException(404, "Album introuvable")
    result = await db.execute(
        select(Song)
        .options(selectinload(Song.artist), selectinload(Song.album),
        selectinload(Song.song_artists).selectinload(SongArtist.artist))
        .where(Song.album_id == album.id)
        .order_by(Song.track_number)
    )
    songs = result.scalars().all()
    return {"tracks": [song_to_dict(s) for s in songs]}


# ── ARTISTS ────────────────────────────────────────────────────────────────────
artists_router = APIRouter()

def artist_to_dict(a: Artist) -> dict:
    h = make_artist_hash(a.name)
    return {
        "artisthash": h,
        "hash":       h,
        "name":       a.name,
        "image":      f"{h}.webp",
        "albumcount": len(a.albums) if a.albums else 0,
        "trackcount": len(a.songs)  if a.songs  else 0,
    }


@artists_router.get("/getall/artists")
async def get_artists(
    start: int = 0,
    limit: int = 200,
    sortby: str = "name",
    reverse: str = "0",
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    cache_key = f"artists:{start}:{limit}:{sortby}:{reverse}"
    cached = _cache_get(cache_key)
    if cached:
        return {"items": cached}

    is_desc = str(reverse).lower() in ("1", "true")
    
    if sortby == "created_date":
        order_col = Artist.id.desc() if is_desc else Artist.id.asc()
    else:
        order_col = Artist.name.desc() if is_desc else Artist.name.asc()

    result = await db.execute(
        select(Artist)
        .options(selectinload(Artist.albums), selectinload(Artist.songs))
        .offset(start).limit(limit)
        .order_by(order_col)
    )
    artists = result.scalars().all()
    items = [artist_to_dict(a) for a in artists]
    _cache_set(cache_key, items)
    return {"items": items}


async def _find_artist_by_hash(db, artist_hash: str):
    """Trouve un artiste par son hash xxhash — utilise un cache mémoire pour éviter le full scan."""
    if artist_hash not in _artist_hash_cache:
        # Rebuild cache
        _artist_hash_cache.clear()
        result = await db.execute(select(Artist.id, Artist.name))
        for row in result.all():
            _artist_hash_cache[make_artist_hash(row.name)] = row.id
            
    if artist_hash in _artist_hash_cache:
        return await db.get(Artist, _artist_hash_cache[artist_hash])
    return None


@artists_router.get("/artist/{artist_hash}/tracks")
async def get_artist_tracks(
    artist_hash: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    artist = await _find_artist_by_hash(db, artist_hash)
    if not artist:
        raise HTTPException(404, "Artiste introuvable")
    result = await db.execute(
        select(Song)
        .options(selectinload(Song.artist), selectinload(Song.album),
        selectinload(Song.song_artists).selectinload(SongArtist.artist))
        .where(Song.artist_id == artist.id)
        .order_by(Song.track_number)
    )
    songs = result.scalars().all()
    return {"tracks": [song_to_dict(s) for s in songs]}


@artists_router.get("/artist/{artist_hash}/albums")
async def get_artist_albums(
    artist_hash: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    artist = await _find_artist_by_hash(db, artist_hash)
    if not artist:
        raise HTTPException(404, "Artiste introuvable")
    result = await db.execute(
        select(Album)
        .options(selectinload(Album.artist))
        .where(Album.artist_id == artist.id)
        .order_by(Album.year.desc())
    )
    albums = result.scalars().all()
    return {"albums": [album_to_dict(a) for a in albums]}


# ── SEARCH ─────────────────────────────────────────────────────────────────────
search_router = APIRouter()

@search_router.get("/search/")
async def search_songs(
    q: str,
    limit: int = 50,
    itemtype: str = "tracks",
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q_safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    query = f"%{q_safe}%"
    result = await db.execute(
        select(Song)
        .options(selectinload(Song.artist), selectinload(Song.album),
        selectinload(Song.song_artists).selectinload(SongArtist.artist))
        .join(Artist, Song.artist_id == Artist.id, isouter=True)
        .join(Album,  Song.album_id  == Album.id,  isouter=True)
        .where(or_(
            Song.title.ilike(query),
            Artist.name.ilike(query),
            Album.title.ilike(query),
        ))
        .limit(limit if limit > 0 else 1000)
    )
    songs = result.scalars().all()
    return {"tracks": [song_to_dict(s) for s in songs]}


@search_router.get("/search/top")
async def search_top(
    q: str,
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q_safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    query = f"%{q_safe}%"
    songs_r = await db.execute(
        select(Song).options(selectinload(Song.artist), selectinload(Song.album),
        selectinload(Song.song_artists).selectinload(SongArtist.artist))
        .join(Artist, Song.artist_id == Artist.id, isouter=True)
        .where(or_(Song.title.ilike(query), Artist.name.ilike(query)))
        .limit(limit)
    )
    albums_r = await db.execute(
        select(Album).options(selectinload(Album.artist))
        .where(Album.title.ilike(query)).limit(limit)
    )
    artists_r = await db.execute(
        select(Artist)
        .options(selectinload(Artist.albums), selectinload(Artist.songs))
        .where(Artist.name.ilike(query)).limit(limit)
    )
    return {
        "tracks":  [song_to_dict(s) for s in songs_r.scalars().all()],
        "albums":  [album_to_dict(a) for a in albums_r.scalars().all()],
        "artists": [artist_to_dict(a) for a in artists_r.scalars().all()],
    }


# ── PLAYLISTS ──────────────────────────────────────────────────────────────────
playlists_router = APIRouter()

class PlaylistCreate(BaseModel):
    name: str
    description: str = ""
    is_public: bool = False

class PlaylistUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None

class TracksAdd(BaseModel):
    trackhashes: List[str]

class TrackRemove(BaseModel):
    trackhash: str
    index: int

class TrackReorder(BaseModel):
    old_index: int
    new_index: int


def playlist_to_dict(p: Playlist, track_count: int = None) -> dict:
    try:
        count = track_count if track_count is not None else (len(p.entries) if p.entries is not None else 0)
    except Exception:
        count = 0
    return {
        "id":          str(p.id),
        "name":        p.name,
        "description": p.description or "",
        "count":       count,
        "trackcount":  count,
        "extra":       {"description": p.description or ""},
        "is_public":   p.is_public if hasattr(p, 'is_public') else False,
        "image":       None,
    }


@playlists_router.get("/playlists")
async def get_playlists(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Playlist)
        .options(selectinload(Playlist.entries))
        .where(Playlist.owner_id == user.id)
        .order_by(Playlist.updated_at.desc())
    )
    playlists = result.scalars().all()
    return {"data": [playlist_to_dict(p, track_count=len(p.entries) if p.entries else 0) for p in playlists]}


@playlists_router.get("/playlists/public")
async def get_public_playlists(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Playlist)
        .options(selectinload(Playlist.entries))
        .where(Playlist.is_public == True)
        .where(Playlist.owner_id != user.id)
        .order_by(Playlist.updated_at.desc())
        .limit(50)
    )
    playlists = result.scalars().all()
    return {"data": [playlist_to_dict(p, track_count=len(p.entries) if p.entries else 0) for p in playlists]}


@playlists_router.get("/playlists/{playlist_id}")
async def get_playlist_tracks(
    playlist_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Playlist)
        .options(
            selectinload(Playlist.entries).selectinload(PlaylistEntry.song).selectinload(Song.artist),
            selectinload(Playlist.entries).selectinload(PlaylistEntry.song).selectinload(Song.album),
            selectinload(Playlist.entries).selectinload(PlaylistEntry.song)
                .selectinload(Song.song_artists).selectinload(SongArtist.artist),
        )
        .where(Playlist.id == playlist_id)
    )
    playlist = result.scalar_one_or_none()
    if not playlist:
        raise HTTPException(404, "Playlist introuvable")
    if playlist.owner_id != user.id and not playlist.is_public:
        raise HTTPException(403, "Acces refuse")
    tracks = [song_to_dict(e.song) for e in sorted(playlist.entries, key=lambda e: e.position) if e.song]
    return {"info": playlist_to_dict(playlist), "tracks": tracks}


@playlists_router.post("/playlists/new")
async def create_playlist(
    body: PlaylistCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    playlist = Playlist(name=body.name, description=body.description, is_public=body.is_public, owner_id=user.id)
    db.add(playlist)
    await db.commit()
    await db.refresh(playlist)
    _cache_invalidate("songs", "stats")
    return {"playlist": playlist_to_dict(playlist, track_count=0)}


@playlists_router.post("/playlists/{playlist_id}/update")
async def update_playlist(
    playlist_id: int,
    body: PlaylistUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    playlist = await db.get(Playlist, playlist_id)
    if not playlist or playlist.owner_id != user.id:
        raise HTTPException(404, "Playlist introuvable")
    if body.name is not None:        playlist.name        = body.name
    if body.description is not None: playlist.description = body.description
    if body.is_public is not None:   playlist.is_public   = body.is_public
    playlist.updated_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}


@playlists_router.post("/playlists/{playlist_id}/delete")
async def delete_playlist(
    playlist_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    playlist = await db.get(Playlist, playlist_id)
    if not playlist or playlist.owner_id != user.id:
        raise HTTPException(404, "Playlist introuvable")
    await db.delete(playlist)
    await db.commit()
    return {"ok": True}


@playlists_router.post("/playlists/{playlist_id}/add")
async def add_tracks(
    playlist_id: int,
    body: TracksAdd,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    playlist = await db.get(Playlist, playlist_id)
    if not playlist or playlist.owner_id != user.id:
        raise HTTPException(404, "Playlist introuvable")
    result = await db.execute(
        select(func.max(PlaylistEntry.position)).where(PlaylistEntry.playlist_id == playlist_id))
    max_pos = result.scalar() or 0
    added = 0
    song_r = await db.execute(select(Song).where(Song.hash.in_(body.trackhashes)))
    songs = song_r.scalars().all()
    hash_to_id = {s.hash: s.id for s in songs}
    for i, h in enumerate(body.trackhashes):
        if h in hash_to_id:
            db.add(PlaylistEntry(playlist_id=playlist_id, song_id=hash_to_id[h], position=max_pos + i + 1))
            added += 1
    playlist.updated_at = datetime.utcnow()
    await db.commit()
    _cache_invalidate("playlists")
    return {"ok": True, "added": added}


@playlists_router.post("/playlists/{playlist_id}/remove")
async def remove_track(
    playlist_id: int,
    body: TrackRemove,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    playlist = await db.get(Playlist, playlist_id)
    if not playlist or playlist.owner_id != user.id:
        raise HTTPException(404, "Playlist introuvable")
    song_r = await db.execute(select(Song).where(Song.hash == body.trackhash))
    song = song_r.scalar_one_or_none()
    if not song:
        raise HTTPException(404, "Titre introuvable")
    
    result = await db.execute(
        select(PlaylistEntry)
        .where(PlaylistEntry.playlist_id == playlist_id)
        .order_by(PlaylistEntry.position)
    )
    entries = result.scalars().all()
    
    if body.index < len(entries):
        entry = entries[body.index]
        if entry.song_id == song.id:
            await db.delete(entry)
            playlist.updated_at = datetime.utcnow()
            await db.commit()
            return {"ok": True}
            
    # Fallback si l'index fourni ne correspond pas à l'ordre exact
    entry_r = await db.execute(
        select(PlaylistEntry)
        .where(PlaylistEntry.playlist_id == playlist_id)
        .where(PlaylistEntry.song_id == song.id)
    )
    all_entries = entry_r.scalars().all()
    if all_entries:
        await db.delete(all_entries[0])
        playlist.updated_at = datetime.utcnow()
        await db.commit()
    return {"ok": True}


@playlists_router.post("/playlists/{playlist_id}/reorder")
async def reorder_playlist(
    playlist_id: int,
    body: TrackReorder,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    playlist = await db.get(Playlist, playlist_id)
    if not playlist or playlist.owner_id != user.id:
        raise HTTPException(404, "Playlist introuvable")
    result = await db.execute(
        select(PlaylistEntry).where(PlaylistEntry.playlist_id == playlist_id).order_by(PlaylistEntry.position))
    entries = result.scalars().all()
    if body.old_index < len(entries) and body.new_index < len(entries):
        entry = entries.pop(body.old_index)
        entries.insert(body.new_index, entry)
        for i, e in enumerate(entries):
            e.position = i
        playlist.updated_at = datetime.utcnow()
        await db.commit()
    return {"ok": True}

# ── GENRES ───────────────────────────────────────────────────────────────────
@songs_router.get("/genres")
async def list_genres(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Liste tous les genres disponibles avec nombre de titres."""
    result = await db.execute(
        select(Song.genre, func.count(Song.id).label("count"))
        .where(Song.genre.isnot(None))
        .where(Song.genre != "")
        .group_by(Song.genre)
        .order_by(desc("count"))
    )
    rows = result.all()
    return {"genres": [{"name": r.genre, "count": r.count} for r in rows]}


@songs_router.get("/genres/{genre}/tracks")
async def genre_tracks(
    genre: str,
    start: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Retourne les titres d'un genre donné."""
    result = await db.execute(
        select(Song)
        .options(
            selectinload(Song.artist),
            selectinload(Song.album),
            selectinload(Song.song_artists).selectinload(SongArtist.artist),
        )
        .where(Song.genre == genre)
        .order_by(Song.title)
        .offset(start)
        .limit(limit)
    )
    songs = result.scalars().all()
    return {"tracks": [song_to_dict(s) for s in songs], "genre": genre}


@songs_router.get("/decades")
async def list_decades(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Liste les décennies disponibles."""
    result = await db.execute(
        select(
            (func.floor(Song.year / 10) * 10).label("decade"),
            func.count(Song.id).label("count"),
        )
        .where(Song.year.isnot(None))
        .where(Song.year > 1900)
        .group_by("decade")
        .order_by(desc("decade"))
    )
    rows = result.all()
    return {"decades": [{"year": int(r.decade), "label": f"{int(r.decade)}s", "count": r.count} for r in rows]}


@songs_router.get("/decades/{decade}/tracks")
async def decade_tracks(
    decade: int,
    start: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Retourne les titres d'une décennie."""
    result = await db.execute(
        select(Song)
        .options(
            selectinload(Song.artist),
            selectinload(Song.album),
            selectinload(Song.song_artists).selectinload(SongArtist.artist),
        )
        .where(Song.year >= decade)
        .where(Song.year < decade + 10)
        .order_by(Song.year, Song.title)
        .offset(start)
        .limit(limit)
    )
    songs = result.scalars().all()
    return {"tracks": [song_to_dict(s) for s in songs], "decade": decade}

import asyncio
import hashlib
import os
import logging
import httpx
from pathlib import Path
from typing import Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from .database import AsyncSessionLocal, Song, Artist, Album, LyricsCache, SongArtist
from .config import settings
from sqlalchemy import select
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = {
    ".mp3", ".flac", ".wav", ".aiff", ".aif",
    ".m4a", ".aac", ".ogg", ".opus", ".wv", ".ape"
}

# Pool de threads pour les operations synchrones (mutagen, Pillow)
_thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="scanner")


def make_hash(text: str) -> str:
    """Hash MD5 generique pour albums, songs, etc."""
    return hashlib.md5(text.encode()).hexdigest()[:16]


def parse_artists(raw: str) -> list[str]:
    """
    Parse une chaine d'artistes en liste.
    Gère : feat. / ft. / & / , / x / vs / with
    Retourne une liste de noms nettoyés, sans doublons.
    Ex: "Artist1 feat. Artist2 & Artist3" → ["Artist1", "Artist2", "Artist3"]
    """
    import re
    if not raw:
        return []
    # Séparateurs courants
    pattern = r'(?i)\s*(?:feat\.?|ft\.?|featuring|\s&\s|,\s+|\s[xX]\s|\svs\.?\s|\swith\s)\s*'
    parts = re.split(pattern, raw.strip())
    seen = set()
    result = []
    for p in parts:
        name = p.strip().strip("'()[] ").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result if result else [raw.strip()]


def make_artist_hash(name: str) -> str:
    """
    Hash artiste compatible Swing Music / AskaSound.
    Utilise xxhash avec unidecode + suppression non-alphanumerique.
    """
    try:
        import xxhash
        from unidecode import unidecode

        def clean(s: str) -> str:
            s = s.lower().strip().replace(" ", "")
            t = "".join(c for c in s if c.isalnum())
            return t if t else s

        cleaned = clean(unidecode(name))
        return xxhash.xxh3_64(cleaned.encode("utf-8")).hexdigest()
    except ImportError:
        return make_hash(name)


def _extract_metadata_sync(filepath: str) -> Optional[dict]:
    """Extraction metadata (sync, appellee via run_in_executor)"""
    try:
        import mutagen
        audio = mutagen.File(filepath, easy=True)
        if audio is None:
            return None

        def tag(key, default=""):
            val = audio.get(key, [default])
            return str(val[0]) if val else default

        def tag_int(key):
            try:
                v = tag(key, "0")
                return int(v.split("/")[0]) if v else None
            except Exception:
                return None

        title = tag("title") or os.path.splitext(os.path.basename(filepath))[0]
        return {
            "title":        title,
            "artist":       tag("artist") or tag("albumartist") or "Artiste inconnu",
            "album":        tag("album") or "Album inconnu",
            "year":         tag_int("date") or tag_int("year"),
            "genre":        tag("genre"),
            "track_number": tag_int("tracknumber"),
            "disc_number":  tag_int("discnumber"),
            "duration":     int(audio.info.length) if audio.info else 0,
            "bitrate":      getattr(audio.info, "bitrate", None),
            "sample_rate":  getattr(audio.info, "sample_rate", None),
            "format":       Path(filepath).suffix.lower().lstrip("."),
        }
    except Exception as e:
        logger.debug(f"mutagen error {filepath}: {e}")
        return None


def _extract_thumbnail_sync(filepath: str, thumb_hash: str, cache_dir: str,
                              thumb_size: int, hd_size: int) -> Optional[str]:
    """Extraction thumbnail (sync, appellee via run_in_executor)"""
    try:
        import io
        from PIL import Image
        from mutagen.id3 import ID3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4

        os.makedirs(cache_dir, exist_ok=True)
        thumb_path = os.path.join(cache_dir, f"{thumb_hash}.webp")
        if os.path.exists(thumb_path):
            return thumb_path

        img_data = None
        ext = Path(filepath).suffix.lower()

        try:
            if ext == ".mp3":
                tags = ID3(filepath)
                for tag in tags.values():
                    if hasattr(tag, "FrameID") and tag.FrameID == "APIC":
                        img_data = tag.data
                        break
            elif ext == ".flac":
                audio = FLAC(filepath)
                if audio.pictures:
                    img_data = audio.pictures[0].data
            elif ext in (".m4a", ".aac"):
                audio = MP4(filepath)
                if "covr" in audio.tags:
                    img_data = bytes(audio.tags["covr"][0])
        except Exception:
            pass

        # Fallback : cover.jpg dans le dossier
        if not img_data:
            folder = Path(filepath).parent
            for cover_name in ["cover.jpg", "cover.jpeg", "cover.png",
                                 "folder.jpg", "front.jpg", "album.jpg"]:
                cover = folder / cover_name
                if cover.exists():
                    try:
                        img = Image.open(cover).convert("RGB")
                        thumb = img.copy()
                        thumb.thumbnail((thumb_size, thumb_size))
                        thumb.save(thumb_path, "WEBP", quality=85)
                        hd_path = os.path.join(cache_dir, f"{thumb_hash}_hd.webp")
                        if not os.path.exists(hd_path):
                            hd = img.copy()
                            hd.thumbnail((hd_size, hd_size))
                            hd.save(hd_path, "WEBP", quality=92)
                        return thumb_path
                    except Exception:
                        pass
            return None

        img = Image.open(io.BytesIO(img_data)).convert("RGB")
        thumb = img.copy()
        thumb.thumbnail((thumb_size, thumb_size))
        thumb.save(thumb_path, "WEBP", quality=85)
        hd_path = os.path.join(cache_dir, f"{thumb_hash}_hd.webp")
        if not os.path.exists(hd_path):
            hd = img.copy()
            hd.thumbnail((hd_size, hd_size))
            hd.save(hd_path, "WEBP", quality=92)
        return thumb_path
    except Exception:
        return None


def _get_embedded_lyrics_sync(filepath: str) -> Optional[str]:
    """Recherche de paroles (sync)"""
    try:
        import mutagen
        # 1. Fichier .lrc
        lrc_path = os.path.splitext(filepath)[0] + ".lrc"
        if os.path.exists(lrc_path):
            try:
                with open(lrc_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
                if len(content) > 10:
                    return content
            except Exception:
                pass
        # 2. Tags ID3 USLT
        try:
            from mutagen.id3 import ID3
            tags = ID3(filepath)
            for key, tag in tags.items():
                if key.startswith("USLT"):
                    text = tag.text
                    if text and len(text) > 10:
                        return text
        except Exception:
            pass
        # 3. Tags generiques
        audio = mutagen.File(filepath, easy=False)
        if audio:
            for key in ["lyrics", "LYRICS", "©lyr", "----:com.apple.iTunes:Lyrics"]:
                val = audio.get(key)
                if val:
                    text = str(val[0]) if isinstance(val, list) else str(val)
                    if len(text) > 10:
                        return text
    except Exception:
        pass
    return None


class LibraryScanner:
    def __init__(self):
        self._scanning = False
        self._progress = {"total": 0, "done": 0, "current": "", "lyrics": 0}

    @property
    def progress(self):
        return self._progress

    @property
    def is_scanning(self):
        return self._scanning

    async def scan_all(self, incremental: bool = False):
        if self._scanning:
            logger.warning("Scan deja en cours")
            return
        self._scanning = True
        mode = "incremental" if incremental else "complet"
        logger.info(f"Demarrage scan {mode}...")
        start = datetime.utcnow()

        try:
            all_files = []
            for music_dir in settings.music_dirs_list:
                if os.path.exists(music_dir):
                    for root, _, files in os.walk(music_dir):
                        for f in files:
                            if Path(f).suffix.lower() in SUPPORTED_FORMATS:
                                all_files.append(os.path.join(root, f))
                else:
                    logger.warning(f"Dossier introuvable: {music_dir}")

            self._progress["total"] = len(all_files)
            self._progress["lyrics"] = 0
            logger.info(f"{len(all_files)} fichiers audio trouves")

            lyrics_tasks = []
            for i, filepath in enumerate(all_files):
                self._progress["done"] = i
                self._progress["current"] = os.path.basename(filepath)
                song_id = await self._process_file(filepath)
                if song_id and settings.LYRICS_ON_SCAN:
                    lyrics_tasks.append(song_id)
                # Ceder le controle a l'event loop tous les 10 fichiers
                if i % 10 == 0:
                    await asyncio.sleep(0)

            if settings.LYRICS_ON_SCAN and lyrics_tasks:
                logger.info(f"Telechargement paroles pour {len(lyrics_tasks)} titres...")
                await self._batch_fetch_lyrics(lyrics_tasks)

            elapsed = (datetime.utcnow() - start).seconds
            logger.info(f"Scan termine : {len(all_files)} fichiers en {elapsed}s, {self._progress['lyrics']} paroles")

            # Nettoyage : supprimer les entrees DB dont le fichier n'existe plus
            removed = await self._cleanup_missing_files()
            if removed:
                logger.info(f"Nettoyage : {removed} fichiers manquants retires de la DB")

            # Invalider le cache apres scan
            from .routers.library import _cache_invalidate, _invalidate_artist_cache
            _cache_invalidate("songs", "albums", "artists", "stats")
            _invalidate_artist_cache()  # Invalidate le cache hash→id des artistes

        except Exception as e:
            logger.error(f"Erreur scan: {e}")
        finally:
            self._scanning = False
            self._progress = {"total": 0, "done": 0, "current": "", "lyrics": 0}

    async def _process_file(self, filepath: str) -> Optional[int]:
        """
        Traite un fichier audio.
        - Extraction metadata via thread pool (mutagen est synchrone)
        - Transaction DB propre avec rollback en cas d'erreur
        """
        try:
            # 1. Extraction metadata dans thread pool (non bloquant)
            loop = asyncio.get_event_loop()
            meta = await loop.run_in_executor(_thread_pool, _extract_metadata_sync, filepath)
            if not meta:
                return None

            async with AsyncSessionLocal() as db:
                try:
                    result = await db.execute(select(Song).where(Song.filepath == filepath))
                    existing = result.scalar_one_or_none()

                    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                    if existing and existing.date_modified == mtime:
                        return None  # Inchange

                    # Artiste principal (premier de la liste)
                    artist_list = meta.get("artist_list", [meta["artist"]])
                    artist = await self._get_or_create_artist(db, artist_list[0])
                    album  = await self._get_or_create_album(db, meta, artist)

                    # Extraction thumbnail dans thread pool
                    thumb_hash = make_hash(f"{meta['artist']}_{meta['album']}")
                    thumb_path = await loop.run_in_executor(
                        _thread_pool,
                        _extract_thumbnail_sync,
                        filepath, thumb_hash, settings.CACHE_DIR,
                        settings.THUMB_SIZE, settings.THUMB_LARGE_SIZE,
                    )

                    if existing:
                        existing.title         = meta["title"]
                        existing.duration      = meta["duration"]
                        existing.track_number  = meta["track_number"]
                        existing.bitrate       = meta["bitrate"]
                        existing.format        = meta["format"]
                        existing.image         = thumb_path
                        existing.image_hash    = thumb_hash
                        existing.artist_id     = artist.id if artist else None
                        existing.album_id      = album.id  if album  else None
                        existing.date_modified = mtime
                        await db.commit()
                        return existing.id
                    else:
                        song = Song(
                            filepath=filepath,
                            filename=os.path.basename(filepath),
                            title=meta["title"],
                            duration=meta["duration"],
                            track_number=meta["track_number"],
                            disc_number=meta["disc_number"],
                            year=meta["year"],
                            genre=meta["genre"],
                            bitrate=meta["bitrate"],
                            sample_rate=meta["sample_rate"],
                            format=meta["format"],
                            file_size=os.path.getsize(filepath),
                            image=thumb_path,
                            image_hash=thumb_hash,
                            hash=make_hash(filepath),
                            artist_id=artist.id if artist else None,
                            album_id=album.id  if album  else None,
                            date_modified=mtime,
                        )
                        db.add(song)
                        await db.commit()
                        await db.refresh(song)

                        # Créer les liaisons song_artists (multi-artiste)
                        await self._sync_song_artists(db, song.id, meta)

                        # Mettre a jour le compteur de pistes de l'album
                        if album:
                            from sqlalchemy import update, func
                            count_r = await db.execute(
                                select(func.count(Song.id)).where(Song.album_id == album.id))
                            album.track_count = count_r.scalar() or 0
                            await db.commit()

                        return song.id

                except Exception as e:
                    await db.rollback()
                    logger.debug(f"Erreur DB {filepath}: {e}")
                    return None

        except Exception as e:
            logger.debug(f"Erreur traitement {filepath}: {e}")
            return None

    async def _batch_fetch_lyrics(self, song_ids: list):
        """Telecharge les paroles manquantes"""
        async with AsyncSessionLocal() as db:
            for song_id in song_ids:
                try:
                    cache_r = await db.execute(
                        select(LyricsCache).where(LyricsCache.song_id == song_id))
                    if cache_r.scalar_one_or_none():
                        continue

                    result2 = await db.execute(
                        select(Song)
                        .options(selectinload(Song.artist), selectinload(Song.album))
                        .where(Song.id == song_id))
                    song = result2.scalar_one_or_none()
                    if not song:
                        continue

                    # Paroles embarquees via thread pool
                    loop = asyncio.get_event_loop()
                    embedded = await loop.run_in_executor(
                        _thread_pool, _get_embedded_lyrics_sync, song.filepath)

                    if embedded:
                        db.add(LyricsCache(
                            song_id=song_id, content=embedded, synced=False, source="embedded"))
                        await db.commit()
                        self._progress["lyrics"] += 1
                        continue

                    # lrclib.net
                    artist_name = song.artist.name if song.artist else ""
                    album_title = song.album.title if song.album else ""
                    lyrics = await self._fetch_lrclib(
                        song.title, artist_name, album_title, song.duration)
                    if lyrics:
                        db.add(LyricsCache(
                            song_id=song_id, content=lyrics["content"],
                            synced=lyrics["synced"], source="lrclib"))
                        await db.commit()
                        self._progress["lyrics"] += 1

                    await asyncio.sleep(0.2)

                except Exception as e:
                    logger.debug(f"Erreur paroles song_id={song_id}: {e}")

    async def _fetch_lrclib(self, title: str, artist: str,
                              album: str, duration: int) -> Optional[dict]:
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
                    elif data.get("plainLyrics"):
                        return {"content": data["plainLyrics"], "synced": False}
        except Exception:
            pass
        return None

    async def _sync_song_artists(self, db, song_id: int, meta: dict):
        """Synchronise la table song_artists depuis les métadonnées."""
        from sqlalchemy import delete
        try:
            # Supprimer les anciennes liaisons
            await db.execute(delete(SongArtist).where(SongArtist.song_id == song_id))

            artist_list      = meta.get("artist_list",      [meta.get("artist", "")])
            albumartist_list = meta.get("albumartist_list", artist_list)

            seen = set()
            pos = 0
            # Artistes principaux
            for name in artist_list:
                if not name or name in seen:
                    continue
                seen.add(name)
                a = await self._get_or_create_artist(db, name)
                if a:
                    role = "main" if pos == 0 else "featured"
                    db.add(SongArtist(song_id=song_id, artist_id=a.id,
                                      role=role, position=pos))
                pos += 1

            # Album artists (si différents)
            for name in albumartist_list:
                if not name or name in seen:
                    continue
                seen.add(name)
                a = await self._get_or_create_artist(db, name)
                if a:
                    db.add(SongArtist(song_id=song_id, artist_id=a.id,
                                      role="albumartist", position=pos))
                pos += 1

            await db.flush()
        except Exception as e:
            logger.debug(f"_sync_song_artists error song_id={song_id}: {e}")

    async def _get_or_create_artist(self, db, name: str) -> Optional[Artist]:
        if not name:
            return None
        result = await db.execute(select(Artist).where(Artist.name == name))
        artist = result.scalar_one_or_none()
        if not artist:
            artist = Artist(name=name, name_sort=name.lower())
            db.add(artist)
            await db.flush()
        return artist

    async def _cleanup_missing_files(self) -> int:
        """Supprime de la DB les titres dont le fichier audio n'existe plus sur le disque."""
        removed = 0
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Song.id, Song.filepath))
                rows = result.all()
                for song_id, filepath in rows:
                    if not os.path.exists(filepath):
                        song = await db.get(Song, song_id)
                        if song:
                            await db.delete(song)
                            removed += 1
                if removed:
                    await db.commit()
        except Exception as e:
            logger.debug(f"Cleanup error: {e}")
        return removed

    async def _get_or_create_album(self, db, meta: dict,
                                     artist: Optional[Artist]) -> Optional[Album]:
        title = meta.get("album", "")
        if not title:
            return None
        album_hash = make_hash(f"{meta['artist']}_{title}")
        result = await db.execute(select(Album).where(Album.hash == album_hash))
        album = result.scalar_one_or_none()
        if not album:
            album = Album(
                title=title,
                artist_id=artist.id if artist else None,
                year=meta.get("year"),
                genre=meta.get("genre"),
                hash=album_hash,
            )
            db.add(album)
            await db.flush()
        return album


scanner = LibraryScanner()

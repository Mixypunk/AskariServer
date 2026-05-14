from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy import (Column, Integer, String, Boolean, DateTime,
                         ForeignKey, Text, BigInteger, Index, UniqueConstraint)
from datetime import datetime
from .config import settings

# ── Engine PostgreSQL async ───────────────────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    # Pool de connexions optimisé pour FastAPI/uvicorn
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,       # vérifie que la connexion est vivante avant usage
    pool_recycle=3600,        # recycle les connexions après 1h
)
AsyncSessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id         = Column(Integer, primary_key=True, index=True)
    username   = Column(String(50), unique=True, nullable=False, index=True)
    password   = Column(String(200), nullable=False)
    role       = Column(String(20), default="user")
    avatar     = Column(String(200), nullable=True)
    email      = Column(String(200), nullable=True, unique=True)
    birth_date = Column(String(20), nullable=True)   # format ISO: YYYY-MM-DD
    bio        = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen  = Column(DateTime, nullable=True)
    is_active  = Column(Boolean, default=True)
    can_download = Column(Boolean, default=False)


class Artist(Base):
    __tablename__ = "artists"
    id        = Column(Integer, primary_key=True, index=True)
    name      = Column(String(200), unique=True, nullable=False)
    name_sort = Column(String(200), nullable=True)
    bio       = Column(Text, nullable=True)
    albums       = relationship("Album",      back_populates="artist")
    songs        = relationship("Song",       back_populates="artist")
    song_artists = relationship("SongArtist", back_populates="artist")

    __table_args__ = (Index("ix_artists_name", "name"),)


class Album(Base):
    __tablename__ = "albums"
    id          = Column(Integer, primary_key=True, index=True)
    title       = Column(String(300), nullable=False)
    artist_id   = Column(Integer, ForeignKey("artists.id"), nullable=True, index=True)
    year        = Column(Integer, nullable=True)
    genre       = Column(String(100), nullable=True)
    hash        = Column(String(32), unique=True, nullable=False, index=True)
    track_count = Column(Integer, default=0)
    duration    = Column(Integer, default=0)
    artist      = relationship("Artist", back_populates="albums")
    songs       = relationship("Song", back_populates="album")


class Song(Base):
    __tablename__ = "songs"
    id           = Column(Integer, primary_key=True, index=True)
    filepath     = Column(String(1000), unique=True, nullable=False, index=True)
    filename     = Column(String(300), nullable=True)
    title        = Column(String(300), nullable=False, index=True)
    duration     = Column(Integer, default=0)
    track_number = Column(Integer, nullable=True)
    disc_number  = Column(Integer, nullable=True)
    year         = Column(Integer, nullable=True)
    genre        = Column(String(100), nullable=True)
    bitrate      = Column(Integer, nullable=True)
    sample_rate  = Column(Integer, nullable=True)
    format       = Column(String(10), nullable=True)
    file_size    = Column(BigInteger, nullable=True)
    image        = Column(String(500), nullable=True)
    image_hash   = Column(String(32), nullable=True, index=True)
    hash         = Column(String(32), unique=True, nullable=False, index=True)
    play_count   = Column(Integer, default=0, index=True)
    last_played  = Column(DateTime, nullable=True)
    date_added   = Column(DateTime, default=datetime.utcnow, index=True)
    date_modified = Column(DateTime, nullable=True)
    artist_id    = Column(Integer, ForeignKey("artists.id"), nullable=True, index=True)
    album_id     = Column(Integer, ForeignKey("albums.id"),  nullable=True, index=True)
    artist       = relationship("Artist", back_populates="songs")
    album        = relationship("Album",  back_populates="songs")
    song_artists = relationship("SongArtist", back_populates="song",
                                cascade="all, delete-orphan")


class SongArtist(Base):
    """Table de liaison many-to-many entre Song et Artist.
    Remplace le simple artist_id sur Song pour supporter les multi-artistes.
    role : 'main' | 'featured' | 'albumartist'
    """
    __tablename__ = "song_artists"
    id        = Column(Integer, primary_key=True, index=True)
    song_id   = Column(Integer, ForeignKey("songs.id"), nullable=False, index=True)
    artist_id = Column(Integer, ForeignKey("artists.id"), nullable=False, index=True)
    role      = Column(String(20), default="main")  # main | featured | albumartist
    position  = Column(Integer, default=0)           # ordre d'affichage

    song   = relationship("Song",   back_populates="song_artists")
    artist = relationship("Artist", back_populates="song_artists")

    __table_args__ = (
        UniqueConstraint("song_id", "artist_id", "role",
                         name="uq_song_artist_role"),
    )


class Playlist(Base):
    __tablename__ = "playlists"
    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    owner_id    = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    is_public   = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow)
    entries     = relationship("PlaylistEntry", back_populates="playlist",
                               cascade="all, delete-orphan")


class PlaylistEntry(Base):
    __tablename__ = "playlist_entries"
    id          = Column(Integer, primary_key=True, index=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id"), nullable=False, index=True)
    song_id     = Column(Integer, ForeignKey("songs.id"),     nullable=False)
    position    = Column(Integer, default=0)
    playlist    = relationship("Playlist", back_populates="entries")
    song        = relationship("Song")


class Favourite(Base):
    __tablename__ = "favourites"
    id      = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    song_id = Column(Integer, ForeignKey("songs.id"), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "song_id", name="uq_favourite_user_song"),
    )


class PlayHistory(Base):
    __tablename__ = "play_history"
    id        = Column(Integer, primary_key=True, index=True)
    song_id   = Column(Integer, ForeignKey("songs.id"), nullable=False, index=True)
    user_id   = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    played_at = Column(DateTime, default=datetime.utcnow, index=True)


class LyricsCache(Base):
    __tablename__ = "lyrics_cache"
    id        = Column(Integer, primary_key=True, index=True)
    song_id   = Column(Integer, ForeignKey("songs.id"), unique=True, index=True)
    synced    = Column(Boolean, default=False)
    content   = Column(Text, nullable=True)
    source    = Column(String(50), nullable=True)
    cached_at = Column(DateTime, default=datetime.utcnow)


async def init_db():
    """Crée les tables si elles n'existent pas (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

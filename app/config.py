from pydantic_settings import BaseSettings
from typing import List
import secrets


class Settings(BaseSettings):
    HOST: str = "0.0.0.0"
    PORT: int = 7777

    # IMPORTANT : generez une vraie cle avec : openssl rand -hex 32
    SECRET_KEY: str = "CHANGE_ME"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7   # 7 jours
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # CORS : separees par des virgules, ex: "https://mon-domaine.duckdns.org,http://localhost:3000"
    # Laisser vide pour autoriser tout (non recommande en production)
    ALLOWED_ORIGINS: str = ""

    MUSIC_DIRS: str = "/music"
    AUTO_SCAN_ON_START: bool = True
    SCAN_INTERVAL_HOURS: int = 24

    DATABASE_URL: str = "postgresql+asyncpg://askaria:askaria@db:5432/askaria"

    CACHE_DIR: str = "/data/cache"
    THUMB_SIZE: int = 300
    THUMB_LARGE_SIZE: int = 600

    FFMPEG_PATH: str = "ffmpeg"
    TRANSCODING_ENABLED: bool = True
    DEFAULT_QUALITY: str = "high"

    # Paroles via lrclib.net (gratuit, sans API key)
    LRCLIB_BASE: str = "https://lrclib.net/api"
    LYRICS_CACHE_DAYS: int = 30
    LYRICS_ON_SCAN: bool = True

    # Last.fm scrobbling
    LASTFM_API_KEY: str = ""
    LASTFM_API_SECRET: str = ""
    LASTFM_USERNAME: str = ""
    LASTFM_PASSWORD_HASH: str = ""

    ALLOW_REGISTRATION: bool = False
    MAX_USERS: int = 10
    TRUST_PROXY_HEADERS: bool = True

    # Rate limiting login (tentatives / fenetre secondes)
    LOGIN_RATE_LIMIT: int = 5
    LOGIN_RATE_WINDOW: int = 60

    @property
    def music_dirs_list(self) -> List[str]:
        return [d.strip() for d in self.MUSIC_DIRS.split(":") if d.strip()]

    @property
    def allowed_origins_list(self) -> List[str]:
        if not self.ALLOWED_ORIGINS.strip():
            return ["*"]
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def lastfm_enabled(self) -> bool:
        return bool(self.LASTFM_API_KEY and self.LASTFM_API_SECRET and self.LASTFM_USERNAME)

    @property
    def secret_key_is_default(self) -> bool:
        return self.SECRET_KEY in ("CHANGE_ME", "change-this-secret-key-in-production",
                                    "CHANGEZ-MOI-openssl-rand-hex-32")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


settings = Settings()

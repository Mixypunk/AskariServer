"""
WebSocket — stats temps réel, paroles live, activité utilisateurs
"""
import asyncio
import json
import logging
from typing import Dict, Set
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db, PlayHistory, Song, User
from ..config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Gestionnaire de connexions WebSocket ──────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        # user_id → set de websockets actives
        self._connections: Dict[int, Set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, user_id: int):
        await ws.accept()
        if user_id not in self._connections:
            self._connections[user_id] = set()
        self._connections[user_id].add(ws)
        logger.info(f"WS connecté: user {user_id} ({len(self._connections)} users actifs)")

    def disconnect(self, ws: WebSocket, user_id: int):
        if user_id in self._connections:
            self._connections[user_id].discard(ws)
            if not self._connections[user_id]:
                del self._connections[user_id]

    async def send(self, user_id: int, data: dict):
        """Envoyer un message à toutes les connexions d'un user"""
        if user_id in self._connections:
            dead = set()
            for ws in self._connections[user_id].copy():
                try:
                    await ws.send_json(data)
                except Exception:
                    dead.add(ws)
            for ws in dead:
                self._connections[user_id].discard(ws)

    async def broadcast(self, data: dict, exclude_user: int = None):
        """Envoyer à tous les users connectés"""
        for user_id in list(self._connections.keys()):
            if user_id != exclude_user:
                await self.send(user_id, data)

    @property
    def active_users(self) -> int:
        return len(self._connections)


manager = ConnectionManager()


# ── Endpoint WebSocket principal ──────────────────────────────────────────────
@router.websocket("/ws/{token}")
async def websocket_endpoint(ws: WebSocket, token: str):
    """
    WebSocket authentifié par token JWT dans l'URL
    Messages entrants :
      {"type": "scrobble", "song_hash": "...", "position": 120, "duration": 240}
      {"type": "ping"}
    Messages sortants :
      {"type": "pong"}
      {"type": "scrobble_ok"}
      {"type": "scan_progress", "done": 50, "total": 200, "current": "song.mp3"}
      {"type": "lyrics_update", "song_hash": "...", "line": 3}
    """
    # Valider le token JWT
    try:
        import jwt
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except Exception:
        await ws.close(code=4001, reason="Token invalide")
        return

    await manager.connect(ws, user_id)

    try:
        # Message de bienvenue
        await ws.send_json({
            "type": "connected",
            "user_id": user_id,
            "active_users": manager.active_users,
        })

        while True:
            try:
                data = await asyncio.wait_for(ws.receive_json(), timeout=60.0)
                await _handle_message(ws, user_id, data)
            except asyncio.TimeoutError:
                # Ping keepalive
                await ws.send_json({"type": "ping"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS error user {user_id}: {e}")
    finally:
        manager.disconnect(ws, user_id)


async def _handle_message(ws: WebSocket, user_id: int, data: dict):
    """Traiter les messages entrants"""
    msg_type = data.get("type", "")

    if msg_type == "ping":
        await ws.send_json({"type": "pong"})

    elif msg_type == "scrobble":
        # Enregistrer la lecture côté serveur
        await _scrobble(user_id, data)
        await ws.send_json({"type": "scrobble_ok"})

    elif msg_type == "now_playing":
        # Diffuser l'activité aux autres users (si admin)
        await manager.broadcast({
            "type": "user_activity",
            "user_id": user_id,
            "song_hash": data.get("song_hash"),
            "song_title": data.get("title"),
        }, exclude_user=user_id)


async def _scrobble(user_id: int, data: dict):
    """Enregistrer un scrobble dans l'historique"""
    try:
        from ..database import AsyncSessionLocal
        from sqlalchemy import select, update
        from datetime import datetime

        song_hash = data.get("song_hash", "")
        position = int(data.get("position", 0))
        duration = int(data.get("duration", 0))
        completed = duration > 0 and position / duration > 0.8

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Song).where(Song.hash == song_hash))
            song = result.scalar_one_or_none()
            if not song:
                return

            entry = PlayHistory(
                user_id=user_id,
                song_id=song.id,
            )
            db.add(entry)

            if completed:
                await db.execute(
                    update(Song)
                    .where(Song.id == song.id)
                    .values(
                        play_count=Song.play_count + 1,
                        last_played=datetime.utcnow()
                    )
                )
            await db.commit()

    except Exception as e:
        logger.debug(f"Scrobble error: {e}")


# ── Notifier les clients du scan en cours ─────────────────────────────────────
async def broadcast_scan_progress(done: int, total: int, current: str):
    """Appelé par le scanner pour notifier tous les clients"""
    await manager.broadcast({
        "type": "scan_progress",
        "done": done,
        "total": total,
        "current": current,
        "percent": round(done / total * 100) if total > 0 else 0,
    })

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db, User
from ..config import settings
from .auth import get_current_user
import asyncio
import logging
import os
import httpx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/downloader", tags=["Downloader"])

def download_track_sync(track_id: str, download_folder: str, arl: str, format: str):
    try:
        from deemix.app.settings import load_settings
        from deemix.plugins.deezer import Deezer
        from deemix import download
        
        dz = Deezer()
        if arl:
            dz.login_via_arl(arl)
        else:
            raise Exception("Aucun jeton ARL (DEEZER_ARL) configuré.")
            
        deemix_settings = load_settings()
        deemix_settings['download_format'] = format
        
        # S'assurer que le dossier de téléchargement existe
        os.makedirs(download_folder, exist_ok=True)
        
        # Téléchargement
        download.download_track(dz, track_id, deemix_settings, download_folder)
        logger.info(f"Téléchargement du morceau {track_id} réussi.")
    except Exception as e:
        logger.error(f"Erreur lors du téléchargement (track {track_id}): {e}")
        raise

@router.post("/deezer/{track_id}")
async def download_deezer_track(
    track_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user)
):
    if not settings.DEEZER_ARL:
        raise HTTPException(status_code=500, detail="DEEZER_ARL non configuré sur le serveur.")
        
    download_folder = settings.music_dirs_list[0] if settings.music_dirs_list else "/music"
    
    # 1. Obtenir l'ID max actuel pour identifier la nouveauté
    from sqlalchemy import func
    max_id_result = await db.execute(select(func.max(Song.id)))
    max_id = max_id_result.scalar() or 0

    # 2. Téléchargement bloquant (mais dans un thread pour ne pas bloquer l'Event Loop)
    await asyncio.to_thread(
        download_track_sync,
        track_id,
        download_folder,
        settings.DEEZER_ARL,
        settings.DEEMIX_FORMAT
    )
    
    # 3. Lancer un scan incrémental pour ajouter le fichier à la BDD
    from ..scanner import scanner
    await scanner.scan_all(incremental=True)
    
    # 4. Retrouver le morceau qui vient d'être ajouté
    new_song_result = await db.execute(select(Song).where(Song.id > max_id).order_by(Song.id.desc()))
    new_songs = new_song_result.scalars().all()
    
    if not new_songs:
        # Fallback au cas où le scan a raté ou le fichier existait déjà
        # On renvoie juste un message de succès sans hash
        return {"message": "Téléchargement terminé, mais morceau non identifié.", "hash": None}
        
    # Le premier de la liste est notre morceau
    song_hash = new_songs[0].hash
    
    return {
        "message": f"Téléchargement du morceau {track_id} terminé.", 
        "folder": download_folder,
        "hash": song_hash
    }

@router.get("/deezer/search")
async def search_deezer(
    q: str,
    limit: int = 50,
    user: User = Depends(get_current_user)
):
    """
    Search tracks on Deezer.
    Matches the user's query against track titles, artists, and albums.
    """
    if not q:
        return {"data": []}
        
    url = f"https://api.deezer.com/search?q={q}&limit={limit}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            return data
        except Exception as e:
            logger.error(f"Erreur lors de la recherche Deezer avec '{q}': {e}")
            raise HTTPException(status_code=500, detail="Erreur lors de la recherche sur Deezer")

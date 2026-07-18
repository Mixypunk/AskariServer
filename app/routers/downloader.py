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
        
        try:
            deemix_settings['download_format'] = format
            download.download_track(dz, track_id, deemix_settings, download_folder)
            logger.info(f"Téléchargement du morceau {track_id} réussi en {format}.")
        except Exception as e:
            logger.warning(f"Échec du téléchargement en {format} pour {track_id}: {e}. Tentative de repli en MP3_128...")
            try:
                deemix_settings['download_format'] = "MP3_128"
                download.download_track(dz, track_id, deemix_settings, download_folder)
                logger.info(f"Téléchargement du morceau {track_id} réussi en MP3_128.")
            except Exception as e2:
                logger.error(f"Échec définitif du téléchargement (track {track_id}): {e2}")
                raise Exception(f"Impossible de télécharger le morceau (Vérifiez votre compte Deezer).")
    except Exception as e:
        logger.error(f"Erreur globale downloader (track {track_id}): {e}")
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
    try:
        await asyncio.to_thread(
            download_track_sync,
            track_id,
            download_folder,
            settings.DEEZER_ARL,
            settings.DEEMIX_FORMAT
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    Search tracks on Deezer.
    Matches the user's query against track titles, artists, and albums.
    """
    if not q:
        return {"data": []}
        
    url = f"https://api.deezer.com/search?q={q}&limit={limit}"
    
    # 1. Fetch local songs that match the query
    from sqlalchemy import select, or_
    from sqlalchemy.orm import selectinload
    from ..database import Song, Artist
    
    local_songs_query = select(Song).join(Artist, isouter=True).options(selectinload(Song.artist)).where(
        or_(
            Song.title.ilike(f"%{q}%"),
            Artist.name.ilike(f"%{q}%")
        )
    )
    result = await db.execute(local_songs_query)
    local_songs = result.scalars().all()
    
    def is_already_local(deezer_track):
        dt = deezer_track.get("title", "").lower().strip()
        da = deezer_track.get("artist", {}).get("name", "").lower().strip()
        
        for s in local_songs:
            lt = s.title.lower().strip() if s.title else ""
            la = s.artist.name.lower().strip() if s.artist and s.artist.name else ""
            # Correspondance souple sur le titre et l'artiste
            if (dt in lt or lt in dt) and (da in la or la in da):
                return True
        return False
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            
            # 2. Filtrer les résultats
            if "data" in data:
                filtered_data = [t for t in data["data"] if not is_already_local(t)]
                data["data"] = filtered_data
                
            return data
        except Exception as e:
            logger.error(f"Erreur lors de la recherche Deezer avec '{q}': {e}")
            raise HTTPException(status_code=500, detail="Erreur lors de la recherche sur Deezer")

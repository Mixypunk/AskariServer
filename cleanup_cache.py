import os
import glob

# Try to get from env or default to docker path
CACHE_DIR = os.environ.get("CACHE_DIR", "/music/img/artwork")

print(f"Nettoyage du dossier cache : {CACHE_DIR}")

if not os.path.exists(CACHE_DIR):
    print("Dossier introuvable. Si vous n'êtes pas dans Docker, définissez CACHE_DIR.")
else:
    files = glob.glob(os.path.join(CACHE_DIR, "*.webp"))
    removed = 0
    for f in files:
        filename = os.path.basename(f)
        if not filename.startswith("artist_") and not filename.startswith("playlist_"):
            try:
                os.remove(f)
                removed += 1
            except Exception as e:
                print(f"Erreur suppression {filename}: {e}")
                
    print(f"Terminé : {removed} pochettes supprimées.")

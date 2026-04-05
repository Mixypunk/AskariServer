#!/usr/bin/env python3
"""
AskariServer — Script de premier démarrage
Génère une clé secrète et crée le compte admin initial
"""
import os
import secrets
import sys
import subprocess


def generate_secret_key(length=64) -> str:
    return secrets.token_urlsafe(length)


def create_env_file():
    if os.path.exists(".env"):
        print("✅ .env existe déjà")
        return

    key = generate_secret_key()
    with open(".env.example") as f:
        template = f.read()

    env_content = template.replace(
        "generez-une-cle-aleatoire-longue-ici-min-32-chars",
        key
    )

    with open(".env", "w") as f:
        f.write(env_content)

    print(f"✅ .env créé avec une clé secrète générée automatiquement")
    print(f"   Clé : {key[:20]}...")


def check_ffmpeg():
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5
        )
        version = result.stdout.split("\n")[0]
        print(f"✅ ffmpeg : {version}")
        return True
    except FileNotFoundError:
        print("⚠️  ffmpeg non trouvé — le transcoding sera désactivé")
        print("   Installation : apt install ffmpeg (Debian/Ubuntu)")
        return False


def check_music_dir():
    music_dir = os.environ.get("MUSIC_DIRS", "/music").split(":")[0]
    if os.path.exists(music_dir):
        files = sum(1 for r, d, f in os.walk(music_dir)
                   for fn in f if fn.endswith(
                       ('.mp3', '.flac', '.wav', '.m4a', '.ogg', '.opus')))
        print(f"✅ Dossier musique : {music_dir} ({files} fichiers audio)")
    else:
        print(f"⚠️  Dossier musique non trouvé : {music_dir}")
        print("   Adapter MUSIC_DIRS dans .env")


def main():
    print("🎵 AskariServer — Initialisation")
    print("=" * 50)

    create_env_file()
    check_ffmpeg()
    check_music_dir()

    print("\n" + "=" * 50)
    print("📋 Prochaines étapes :")
    print("  1. Vérifier/adapter .env")
    print("  2. Démarrer : uvicorn app.main:app --host 0.0.0.0 --port 7777")
    print("  3. Créer admin : POST /auth/setup")
    print("     curl -X POST http://localhost:7777/auth/setup \\")
    print('          -H "Content-Type: application/json" \\')
    print('          -d \'{"username": "admin", "password": "motdepasse"}\'')
    print("  4. Documentation API : http://localhost:7777/docs")
    print()


if __name__ == "__main__":
    main()

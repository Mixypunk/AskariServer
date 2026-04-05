# AskariServer — Guide d'installation TrueNAS SCALE Goldeye

---

## Vue d'ensemble

```
git push  →  GitHub Actions build  →  GHCR image  →  TrueNAS Custom App
```

---

## PARTIE 1 — Préparer GitHub (une seule fois)

### 1.1 Configurer Git et pousser le code

```bash
git config --global user.email "votre@email.com"
git config --global user.name "Mixypunk"
git init
git remote add origin https://github.com/Mixypunk/Askariserver.git
git branch -M main
git add .
git commit -m "initial commit"
git push origin main --force
```

Si Git demande un mot de passe : utilisez un Personal Access Token GitHub
(pas votre mot de passe). Créez-en un sur https://github.com/settings/tokens
avec la permission `repo` cochée.

### 1.2 Rendre l'image publique sur GHCR

Après que GitHub Actions ait terminé le build (~5 min) :

1. https://github.com/Mixypunk?tab=packages
2. Cliquer sur **askariserver**
3. **Package settings** → **Change visibility** → **Public**

---

## PARTIE 2 — Installer sur TrueNAS

### 2.1 Créer le dossier de données (SSH)

```bash
mkdir -p /mnt/Apps/askariserver
```

### 2.2 Générer une clé secrète (SSH)

```bash
openssl rand -hex 32
```

Copiez le résultat — vous en aurez besoin à l'étape suivante.

### 2.3 Créer l'app dans TrueNAS

1. **Apps** → **Discover Apps** → **Custom App** (bouton haut à droite)
2. Collez ce docker-compose dans le champ **Docker Compose** :

```yaml
services:
  askariserver:
    image: ghcr.io/mixypunk/askariserver:latest
    restart: unless-stopped
    ports:
      - "7777:7777"
    volumes:
      - /mnt/NAS/Media/Music:/music:ro
      - /mnt/Apps/askariserver:/data
    environment:
      SECRET_KEY: "REMPLACEZ-PAR-VOTRE-CLE-GENEREE"
      PORT: "7777"
      HOST: "0.0.0.0"
      MUSIC_DIRS: "/music"
      AUTO_SCAN_ON_START: "true"
      SCAN_INTERVAL_HOURS: "24"
      DATABASE_URL: "sqlite+aiosqlite:////data/askari.db"
      CACHE_DIR: "/data/cache"
      TRANSCODING_ENABLED: "true"
      FFMPEG_PATH: "ffmpeg"
      TRUST_PROXY_HEADERS: "true"
      ALLOW_REGISTRATION: "false"
      MAX_USERS: "10"
      LYRICS_ON_SCAN: "true"
      LASTFM_API_KEY: ""
      LASTFM_API_SECRET: ""
      LASTFM_USERNAME: ""
      LASTFM_PASSWORD_HASH: ""
```

3. Remplacez `REMPLACEZ-PAR-VOTRE-CLE-GENEREE` par la clé de l'étape 2.2
4. Cliquer **Install**

### 2.4 Vérifier

Dans **Apps**, attendez que le statut passe à **Running**.
Accédez à : `http://IP_DU_NAS:7777`

---

## PARTIE 3 — Premier démarrage

### Créer le compte admin (SSH)

```bash
curl -X POST http://localhost:7777/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "VOTRE_MOT_DE_PASSE"}'
```

### Configurer l'app mobile AskaSound

Dans l'app → Paramètres → URL serveur :
```
http://IP_DU_NAS:7777
```

---

## PARTIE 4 — Mises à jour

```bash
git add .
git commit -m "description du changement"
git push origin main
```

GitHub Actions rebuild automatiquement (~5 min), puis dans TrueNAS :
**Apps** → **askariserver** → **Stop** → **Start**

---

## PARTIE 5 — Last.fm (optionnel)

Pour activer le scrobbling automatique :

1. Créez un compte API sur https://www.last.fm/api/account/create
2. Générez le hash MD5 de votre mot de passe Last.fm :
   ```bash
   echo -n "votre_mot_de_passe_lastfm" | md5sum
   ```
3. Modifiez le docker-compose dans TrueNAS → remplissez les 4 variables Last.fm
4. **Stop** → **Start**

---

## PARTIE 6 — Fichiers LRC (paroles synchronisées)

Placez vos fichiers `.lrc` dans le même dossier que vos fichiers audio,
avec le même nom de fichier :

```
/mnt/NAS/Media/Music/
├── PLK - Nouvelles.mp3
├── PLK - Nouvelles.lrc   ← detecte automatiquement
```

Puis lancez un scan incrémental depuis le panel Admin du lecteur web.

---

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `SECRET_KEY` | — | Clé JWT (obligatoire) |
| `MUSIC_DIRS` | `/music` | Dossier(s) musique, séparés par `:` |
| `PORT` | `7777` | Port d'écoute |
| `AUTO_SCAN_ON_START` | `true` | Scan au démarrage |
| `SCAN_INTERVAL_HOURS` | `24` | Re-scan automatique |
| `TRANSCODING_ENABLED` | `true` | Transcoding ffmpeg |
| `LYRICS_ON_SCAN` | `true` | Télécharger paroles au scan |
| `ALLOW_REGISTRATION` | `false` | Inscription libre |
| `MAX_USERS` | `10` | Limite utilisateurs |
| `LASTFM_API_KEY` | — | API Key Last.fm |
| `LASTFM_API_SECRET` | — | Secret Last.fm |
| `LASTFM_USERNAME` | — | Pseudo Last.fm |
| `LASTFM_PASSWORD_HASH` | — | MD5 du mot de passe Last.fm |

---

## Dépannage

**L'image ne se télécharge pas**
→ Vérifiez que le package GHCR est Public (étape 1.2)
→ Vérifiez que le build GitHub Actions est vert

**Le container s'arrête au démarrage**
→ Apps → askariserver → Logs
→ Cause fréquente : SECRET_KEY manquante ou /data inaccessible
→ Si erreur "table already exists" : supprimez /mnt/Apps/askariserver/askari.db

**Aucun titre trouvé**
→ Vérifiez le chemin de la musique (sensible à la casse : Music ≠ music)
→ Vérifiez les permissions : chmod -R 755 /mnt/NAS/Media/Music

**Port déjà utilisé**
→ Changez "7777:7777" en "7778:7777" dans le docker-compose

**Pas de paroles**
→ Placez les .lrc à côté des fichiers audio
→ Lancez un scan incrémental depuis Admin → Bibliothèque

---

Issues : https://github.com/Mixypunk/Askariserver/issues

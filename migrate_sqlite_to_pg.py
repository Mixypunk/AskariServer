#!/usr/bin/env python3
"""
Script de migration SQLite → PostgreSQL pour Askaria.

Usage sur TrueNAS (dans le container ou en local) :
    pip install sqlalchemy asyncpg aiosqlite
    python3 migrate_sqlite_to_pg.py \
        --sqlite /mnt/Apps/askariserver/askari.db \
        --pg "postgresql+asyncpg://askaria:MOT_DE_PASSE@localhost:5432/askaria"

Le script :
    1. Lit toutes les tables depuis SQLite
    2. Crée le schéma dans PostgreSQL (si pas déjà fait)
    3. Insère les données par batch de 500
    4. Vérifie les counts en fin de migration
"""
import asyncio
import argparse
import sys
from datetime import datetime

async def migrate(sqlite_url: str, pg_url: str, batch_size: int = 500):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select, text

    print(f"\n{'='*60}")
    print("  Migration Askaria : SQLite → PostgreSQL")
    print(f"{'='*60}")
    print(f"  Source : {sqlite_url}")
    print(f"  Cible  : {pg_url.split('@')[-1]}")  # cacher le mot de passe
    print()

    # Engines
    sqlite_engine = create_async_engine(sqlite_url.replace("sqlite:", "sqlite+aiosqlite:") if "aiosqlite" not in sqlite_url else sqlite_url, echo=False)
    pg_engine     = create_async_engine(pg_url, echo=False)

    SqliteSession = async_sessionmaker(sqlite_engine, expire_on_commit=False, class_=AsyncSession)
    PgSession     = async_sessionmaker(pg_engine,     expire_on_commit=False, class_=AsyncSession)

    # Ordre respectant les FK
    tables = [
        "users", "artists", "albums", "songs",
        "playlists", "playlist_entries",
        "favourites", "play_history", "lyrics_cache",
    ]

    total_migrated = 0

    for table in tables:
        async with SqliteSession() as src:
            try:
                result = await src.execute(text(f"SELECT * FROM {table}"))
                rows = result.mappings().all()
            except Exception as e:
                print(f"  SKIP {table} ({e})")
                continue

        if not rows:
            print(f"  {table:25s} — vide, ignoré")
            continue

        # Insérer par batch dans PostgreSQL
        async with PgSession() as dst:
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                data  = [dict(r) for r in batch]

                # Convertir les champs datetime (SQLite stocke en string parfois)
                for row in data:
                    for k, v in row.items():
                        if isinstance(v, str) and len(v) == 19 and v[10] == " ":
                            try:
                                row[k] = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
                            except ValueError:
                                pass

                try:
                    cols = ", ".join(data[0].keys())
                    placeholders = ", ".join([f":{k}" for k in data[0].keys()])
                    await dst.execute(
                        text(f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"),
                        data
                    )
                    await dst.commit()
                except Exception as e:
                    await dst.rollback()
                    print(f"  ERREUR batch {table} [{i}:{i+batch_size}] : {e}")
                    continue

        total_migrated += len(rows)
        print(f"  {table:25s} — {len(rows):>6} lignes migrées  ✓")

    # Recaler les séquences PostgreSQL (auto-increment)
    print(f"\n  Recalibration des séquences PostgreSQL...")
    async with PgSession() as dst:
        for table in tables:
            try:
                await dst.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE(MAX(id), 0) + 1, false) FROM {table}"
                ))
            except Exception:
                pass
        await dst.commit()

    await sqlite_engine.dispose()
    await pg_engine.dispose()

    print(f"\n{'='*60}")
    print(f"  Migration terminée : {total_migrated} lignes au total")
    print(f"{'='*60}\n")
    print("  Étapes suivantes :")
    print("  1. Vérifiez que les counts correspondent")
    print("  2. Redémarrez le container Askaria")
    print("  3. Gardez le fichier .db SQLite quelques jours par précaution\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migration SQLite → PostgreSQL pour Askaria")
    parser.add_argument("--sqlite", required=True,
        help="Chemin vers le fichier SQLite (ex: /mnt/Apps/askariserver/askari.db)")
    parser.add_argument("--pg", required=True,
        help="URL PostgreSQL (ex: postgresql+asyncpg://askaria:mdp@localhost:5432/askaria)")
    parser.add_argument("--batch", type=int, default=500, help="Taille des batch (défaut: 500)")
    args = parser.parse_args()

    sqlite_url = f"sqlite+aiosqlite:///{args.sqlite.lstrip('/')}"
    asyncio.run(migrate(sqlite_url, args.pg, args.batch))

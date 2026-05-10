"""require email for login — rend email NOT NULL

Revision ID: 003_require_email
Revises: 002_song_artists
Create Date: 2026-05-10

⚠️  ATTENTION : ne lancer cette migration QUE lorsque tous les utilisateurs
    ont renseigné une adresse email. Vérifier d'abord avec :
    SELECT id, username FROM users WHERE email IS NULL;
"""
from alembic import op
import sqlalchemy as sa

revision = '003_require_email'
down_revision = '002_song_artists'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Vérification préalable : refuser si des users n'ont pas d'email
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT COUNT(*) FROM users WHERE email IS NULL")
    )
    count = result.scalar()
    if count > 0:
        raise RuntimeError(
            f"Migration annulée : {count} utilisateur(s) n'ont pas encore "
            f"d'email. Demandez-leur d'en renseigner un via leur profil, "
            f"puis relancez la migration.\n"
            f"Commande pour voir qui : SELECT id, username FROM users WHERE email IS NULL;"
        )

    # Rendre email NOT NULL (tous les users en ont un à ce stade)
    op.alter_column('users', 'email',
                    existing_type=sa.String(200),
                    nullable=False)


def downgrade() -> None:
    # Repasser email en nullable (rollback)
    op.alter_column('users', 'email',
                    existing_type=sa.String(200),
                    nullable=True)

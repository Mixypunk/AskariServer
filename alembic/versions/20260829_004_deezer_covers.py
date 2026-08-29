"""Deezer covers — store cover URL in albums, remove image/image_hash from songs

Revision ID: 004_deezer_covers
Revises: 003_require_email
Create Date: 2026-08-29
"""
from alembic import op
import sqlalchemy as sa

revision = '004_deezer_covers'
down_revision = '003_require_email'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ajouter la colonne deezer_cover_url sur albums
    op.add_column('albums', sa.Column('deezer_cover_url', sa.String(500), nullable=True))

    # Supprimer les colonnes de cache image sur songs
    with op.batch_alter_table('songs') as batch_op:
        try:
            batch_op.drop_index('ix_songs_image_hash')
        except Exception:
            pass
        batch_op.drop_column('image')
        batch_op.drop_column('image_hash')


def downgrade() -> None:
    # Restaurer les colonnes sur songs
    with op.batch_alter_table('songs') as batch_op:
        batch_op.add_column(sa.Column('image', sa.String(500), nullable=True))
        batch_op.add_column(sa.Column('image_hash', sa.String(32), nullable=True))
        try:
            batch_op.create_index('ix_songs_image_hash', ['image_hash'])
        except Exception:
            pass

    # Supprimer deezer_cover_url
    op.drop_column('albums', 'deezer_cover_url')

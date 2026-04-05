"""add song_artists many-to-many table

Revision ID: 002_song_artists
Revises: 001_profile
Create Date: 2026-03-22
"""
from alembic import op
import sqlalchemy as sa

revision = '002_song_artists'
down_revision = '001_profile'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'song_artists',
        sa.Column('id',        sa.Integer, primary_key=True, index=True),
        sa.Column('song_id',   sa.Integer, sa.ForeignKey('songs.id'),   nullable=False, index=True),
        sa.Column('artist_id', sa.Integer, sa.ForeignKey('artists.id'), nullable=False, index=True),
        sa.Column('role',      sa.String(20),  default='main'),
        sa.Column('position',  sa.Integer,     default=0),
        sa.UniqueConstraint('song_id', 'artist_id', 'role', name='uq_song_artist_role'),
    )
    # Remplir depuis les données existantes (artist_id sur Song)
    op.execute("""
        INSERT INTO song_artists (song_id, artist_id, role, position)
        SELECT id, artist_id, 'main', 0
        FROM songs
        WHERE artist_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table('song_artists')

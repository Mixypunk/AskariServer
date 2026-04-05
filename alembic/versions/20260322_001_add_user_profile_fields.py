"""add user profile fields

Revision ID: 001_profile
Revises:
Create Date: 2026-03-22
"""
from alembic import op
import sqlalchemy as sa

revision = '001_profile'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ajouter les nouvelles colonnes (nullable = pas de valeur requise pour les users existants)
    op.add_column('users', sa.Column('email',      sa.String(200), nullable=True))
    op.add_column('users', sa.Column('birth_date', sa.String(20),  nullable=True))
    op.add_column('users', sa.Column('bio',        sa.String(300), nullable=True))
    # Index unique sur email (ignore les NULL)
    op.create_index('ix_users_email', 'users', ['email'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_users_email', table_name='users')
    op.drop_column('users', 'bio')
    op.drop_column('users', 'birth_date')
    op.drop_column('users', 'email')

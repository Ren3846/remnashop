"""update lava gateway settings schema for lava.top

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-13

"""
from alembic import op

revision = "a022"
down_revision = "a021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE payment_gateways
        SET settings = '{"api_key": null, "offer_id": null, "webhook_secret": null}'::jsonb
        WHERE type = 'LAVA'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE payment_gateways
        SET settings = '{"shop_id": null, "secret_key": null}'::jsonb
        WHERE type = 'LAVA'
    """)

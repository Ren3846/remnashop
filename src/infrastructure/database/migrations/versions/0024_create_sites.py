from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a024"
down_revision: Union[str, None] = "a023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("base_domain", sa.String(length=253), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.UniqueConstraint("base_domain", name="uq_sites_base_domain"),
    )
    op.create_index("ix_sites_base_domain", "sites", ["base_domain"])

    op.create_table(
        "site_subdomains",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.UniqueConstraint("host", name="uq_site_subdomains_host"),
    )
    op.create_index("ix_site_subdomains_site_id", "site_subdomains", ["site_id"])
    op.create_index("ix_site_subdomains_host", "site_subdomains", ["host"])

    op.create_table(
        "site_keywords",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subdomain_id",
            sa.Integer(),
            sa.ForeignKey("site_subdomains.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("keyword", sa.String(length=256), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.UniqueConstraint("subdomain_id", "keyword", name="uq_site_keywords_pair"),
    )
    op.create_index("ix_site_keywords_subdomain_id", "site_keywords", ["subdomain_id"])


def downgrade() -> None:
    op.drop_index("ix_site_keywords_subdomain_id", table_name="site_keywords")
    op.drop_table("site_keywords")
    op.drop_index("ix_site_subdomains_host", table_name="site_subdomains")
    op.drop_index("ix_site_subdomains_site_id", table_name="site_subdomains")
    op.drop_table("site_subdomains")
    op.drop_index("ix_sites_base_domain", table_name="sites")
    op.drop_table("sites")

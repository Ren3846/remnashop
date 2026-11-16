"""subscriptions.user_remna_id: Remnawave 2.x uuid -> Remnawave 3.x numeric id (BIGINT)

Revision ID: a026
Revises: 0040
Create Date: 2026-11-16

Remnawave 3.0 dropped the user uuid and addresses users by a numeric id. This migration
re-links every subscription through an exact, pre-verified map ``old uuid -> new id``.
It never matches by username, Telegram ID, email or row order.

Inputs (environment):
  REMNA_ID_MAP_PATH              CSV with header ``uuid,remna_id`` (required when the table
                                 has rows to convert).
  REMNA_ID_MAP_SHA256            Expected sha256 of that file (required with the path).
  REMNA_ID_MAP_ALLOW_UNRESOLVED  Comma-separated local subscription ids that are allowed to
                                 stay unlinked (user_remna_id NULL). Every other uuid must be
                                 in the map, otherwise the migration aborts and rolls back.

The original uuid is preserved in ``user_remna_uuid_legacy`` for audit and restore.
"""

import csv
import hashlib
import os
from typing import Sequence, Union
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a026"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INT64_MAX = 2**63 - 1


class RemnaIdMigrationError(RuntimeError):
    pass


def _load_map() -> dict[UUID, int]:
    path = os.environ.get("REMNA_ID_MAP_PATH")
    expected_sha = os.environ.get("REMNA_ID_MAP_SHA256", "").strip().lower()
    if not path:
        raise RemnaIdMigrationError("REMNA_ID_MAP_PATH is not set")
    if not expected_sha:
        raise RemnaIdMigrationError("REMNA_ID_MAP_SHA256 is not set")

    with open(path, "rb") as f:
        raw = f.read()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        raise RemnaIdMigrationError(
            f"Map checksum mismatch: expected {expected_sha}, got {actual_sha}"
        )

    mapping: dict[UUID, int] = {}
    seen_ids: set[int] = set()
    reader = csv.DictReader(raw.decode("utf-8").splitlines())
    if reader.fieldnames != ["uuid", "remna_id"]:
        raise RemnaIdMigrationError(f"Unexpected map header: {reader.fieldnames}")
    for row in reader:
        uuid = UUID(row["uuid"])
        remna_id = int(row["remna_id"])  # int(), never float: keeps BIGINT precision
        if not 0 < remna_id <= _INT64_MAX:
            raise RemnaIdMigrationError(f"Panel id out of range for uuid {uuid}")
        if uuid in mapping:
            raise RemnaIdMigrationError(f"Duplicate uuid in map: {uuid}")
        if remna_id in seen_ids:
            raise RemnaIdMigrationError(f"Duplicate panel id in map: {remna_id}")
        mapping[uuid] = remna_id
        seen_ids.add(remna_id)
    return mapping


def _allowed_unresolved() -> set[int]:
    raw = os.environ.get("REMNA_ID_MAP_ALLOW_UNRESOLVED", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "subscriptions",
        sa.Column("user_remna_uuid_legacy", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("subscriptions", sa.Column("user_remna_id_new", sa.BigInteger(), nullable=True))
    conn.execute(sa.text("UPDATE subscriptions SET user_remna_uuid_legacy = user_remna_id"))

    rows = conn.execute(
        sa.text("SELECT id, user_remna_uuid_legacy FROM subscriptions ORDER BY id")
    ).all()

    if rows:
        mapping = _load_map()
        allowed = _allowed_unresolved()
        unresolved = [sub_id for sub_id, uuid in rows if uuid is not None and uuid not in mapping]

        unexpected = sorted(set(unresolved) - allowed)
        if unexpected:
            raise RemnaIdMigrationError(
                f"{len(unexpected)} subscription(s) have no exact map entry and are not "
                f"allowed to stay unlinked: {unexpected}"
            )
        stale_allow = sorted(allowed - set(unresolved))
        if stale_allow:
            raise RemnaIdMigrationError(
                f"REMNA_ID_MAP_ALLOW_UNRESOLVED lists subscriptions that do resolve: {stale_allow}"
            )

        conn.execute(
            sa.text(
                "CREATE TEMP TABLE _remna_id_map "
                "(uuid uuid PRIMARY KEY, remna_id bigint NOT NULL UNIQUE) ON COMMIT DROP"
            )
        )
        conn.execute(
            sa.text("INSERT INTO _remna_id_map (uuid, remna_id) VALUES (:uuid, :remna_id)"),
            [{"uuid": u, "remna_id": i} for u, i in mapping.items()],
        )
        matched = conn.execute(
            sa.text(
                "UPDATE subscriptions s SET user_remna_id_new = m.remna_id "
                "FROM _remna_id_map m WHERE m.uuid = s.user_remna_uuid_legacy"
            )
        ).rowcount

        expected = len(rows) - len(unresolved)
        if matched != expected:
            raise RemnaIdMigrationError(f"Matched {matched} rows, expected {expected}")

        mismatched = conn.execute(
            sa.text(
                "SELECT count(*) FROM subscriptions WHERE user_remna_id_new IS NULL "
                "AND NOT (id = ANY(:allowed))"
            ),
            {"allowed": sorted(allowed) or [0]},
        ).scalar_one()
        if mismatched:
            raise RemnaIdMigrationError(f"{mismatched} non-allowed subscriptions left unlinked")

    op.drop_index("ix_subscriptions_user_remna_id", table_name="subscriptions")
    op.drop_column("subscriptions", "user_remna_id")
    op.alter_column("subscriptions", "user_remna_id_new", new_column_name="user_remna_id")
    op.create_index("ix_subscriptions_user_remna_id", "subscriptions", ["user_remna_id"])


def downgrade() -> None:
    # Restores the 2.x uuid from the preserved legacy column. Rows created after the upgrade
    # have no legacy uuid: refuse instead of losing them silently.
    conn = op.get_bind()
    orphans = conn.execute(
        sa.text("SELECT count(*) FROM subscriptions WHERE user_remna_uuid_legacy IS NULL")
    ).scalar_one()
    if orphans:
        raise RemnaIdMigrationError(
            f"{orphans} subscription(s) were created after the upgrade and have no 2.x uuid; "
            f"reconcile them manually before downgrading"
        )

    op.drop_index("ix_subscriptions_user_remna_id", table_name="subscriptions")
    op.alter_column("subscriptions", "user_remna_id", new_column_name="user_remna_id_v3")
    op.alter_column(
        "subscriptions", "user_remna_uuid_legacy", new_column_name="user_remna_id", nullable=False
    )
    op.drop_column("subscriptions", "user_remna_id_v3")
    op.create_index("ix_subscriptions_user_remna_id", "subscriptions", ["user_remna_id"])

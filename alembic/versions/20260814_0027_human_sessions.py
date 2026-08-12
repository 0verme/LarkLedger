"""Human session management for the Web dashboard (P37).

Extends ``dashboard_sessions`` — the existing browser-session table — into a
first-class multi-device human credential store:

* ``user_agent`` — bounded device context for the session-management UI and
  incident analysis (``String(512)``, nullable; legacy rows stay NULL).
* ``created_ip_hash`` — SHA-256 digest of the client IP only; the raw IP is
  never persisted (``String(64)``, nullable).
* ``ix_dashboard_sessions_revoked`` — supports the Cleanup Worker's
  revoked-or-expired sweep without scanning the whole table.

No credential material is added: the raw session secret remains browser-only
and the database keeps storing only its SHA-256 digest in ``token_hash``.

Downgrade drops the two columns and the index; existing session rows need no
repair.

Revision ID: 20260814_0027
Revises: 20260813_0026
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260814_0027"
down_revision: str | None = "20260813_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dashboard_sessions",
        sa.Column("user_agent", sa.String(512), nullable=True),
    )
    op.add_column(
        "dashboard_sessions",
        sa.Column("created_ip_hash", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_dashboard_sessions_revoked",
        "dashboard_sessions",
        ["revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dashboard_sessions_revoked", table_name="dashboard_sessions")
    op.drop_column("dashboard_sessions", "created_ip_hash")
    op.drop_column("dashboard_sessions", "user_agent")

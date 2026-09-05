from alembic import op
import sqlalchemy as sa

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.UniqueConstraint("id", "organization_id", name="uq_users_organization"),
        sa.CheckConstraint("role IN ('reader', 'operator', 'admin')", name="ck_users_role"),
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("csrf_token", sa.String(43), nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    limits = op.create_table(
        "login_limits",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("window", sa.BigInteger, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
    )
    op.bulk_insert(limits, [{"key": "global", "window": 0, "attempts": 0}])
    op.create_table(
        "identity_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_index("ix_identity_events_organization_id", "identity_events", ["organization_id"])
    for name in ("fields", "crops", "subscriptions", "jobs"):
        additions = []
        if name == "subscriptions":
            additions = [
                sa.Column("field_id", sa.String(36), nullable=False),
                sa.ForeignKeyConstraint(
                    ["field_id", "owner_id", "organization_id"],
                    ["fields.id", "fields.owner_id", "fields.organization_id"],
                    name="fk_subscriptions_field_owner", ondelete="CASCADE",
                ),
            ]
        if name == "jobs":
            additions = [
                sa.Column("status", sa.String(16), nullable=False),
                sa.CheckConstraint(
                    "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_jobs_status",
                ),
            ]
        op.create_table(
            name,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("organization_id", sa.String(36), nullable=False),
            sa.Column("owner_id", sa.String(36), nullable=False),
            sa.Column("data", sa.JSON, nullable=False),
            sa.Column("created_at", sa.BigInteger, nullable=False),
            sa.Column("updated_at", sa.BigInteger, nullable=False),
            sa.ForeignKeyConstraint(
                ["owner_id", "organization_id"], ["users.id", "users.organization_id"],
                name=f"fk_{name}_owner_organization",
            ),
            sa.UniqueConstraint("id", "owner_id", "organization_id", name=f"uq_{name}_owner_organization"),
            *additions,
        )
        op.create_index(f"ix_{name}_owner_id", name, ["owner_id"])


def downgrade():
    for name in ("jobs", "subscriptions", "crops", "fields", "identity_events", "login_limits", "sessions", "users", "organizations"):
        op.drop_table(name)

from alembic import op
import sqlalchemy as sa

revision = "0004_durable_queue"
down_revision = "0003_persistent_state"
branch_labels = None
depends_on = None

ACTIVE_QUEUE_FILTER = "queue_kind IS NOT NULL AND status IN ('queued', 'running')"

QUEUE_COLUMNS = (
    ("queue_kind", sa.String(32)),
    ("dedup_sha256", sa.String(64)),
    ("next_retry_at", sa.BigInteger()),
    ("lease_owner", sa.String(128)),
    ("lease_expires_at", sa.BigInteger()),
    ("heartbeat_at", sa.BigInteger()),
    ("deadline_at", sa.BigInteger()),
    ("cancel_requested_at", sa.BigInteger()),
    ("parent_id", sa.String(36)),
    ("started_at", sa.BigInteger()),
    ("finished_at", sa.BigInteger()),
    ("result_checksum", sa.String(64)),
)


def upgrade():
    with op.batch_alter_table("jobs", recreate="auto") as batch:
        for name, column_type in QUEUE_COLUMNS:
            batch.add_column(sa.Column(name, column_type, nullable=True))
        batch.add_column(sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"))
        batch.create_foreign_key("fk_jobs_parent_queue", "jobs", ["parent_id"], ["id"], ondelete="CASCADE")
        batch.create_check_constraint("ck_jobs_attempts", "attempts >= 0")
        batch.create_check_constraint("ck_jobs_max_attempts", "max_attempts BETWEEN 1 AND 5")
    op.create_index(
        "uq_jobs_active_dedup", "jobs", ["organization_id", "dedup_sha256"], unique=True,
        postgresql_where=sa.text(ACTIVE_QUEUE_FILTER), sqlite_where=sa.text(ACTIVE_QUEUE_FILTER),
    )
    op.create_index(
        "ix_jobs_claim", "jobs", ["status", "next_retry_at", "created_at", "id"],
        postgresql_where=sa.text("queue_kind IS NOT NULL"), sqlite_where=sa.text("queue_kind IS NOT NULL"),
    )
    op.create_index(
        "ix_jobs_lease_expiry", "jobs", ["status", "lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"), sqlite_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "queue_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("job_id", "sequence", name="uq_queue_event_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_queue_event_sequence"),
    )
    op.create_index("ix_queue_events_job_id", "queue_events", ["job_id"])


def downgrade():
    op.drop_index("ix_queue_events_job_id", table_name="queue_events")
    op.drop_table("queue_events")
    op.drop_index("ix_jobs_lease_expiry", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_index("uq_jobs_active_dedup", table_name="jobs")
    with op.batch_alter_table("jobs", recreate="auto") as batch:
        batch.drop_constraint("ck_jobs_max_attempts", type_="check")
        batch.drop_constraint("ck_jobs_attempts", type_="check")
        batch.drop_constraint("fk_jobs_parent_queue", type_="foreignkey")
        batch.drop_column("max_attempts")
        batch.drop_column("attempts")
        for name, _ in reversed(QUEUE_COLUMNS):
            batch.drop_column(name)

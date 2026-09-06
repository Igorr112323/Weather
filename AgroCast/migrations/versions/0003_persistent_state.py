from alembic import op
import sqlalchemy as sa

revision = "0003_persistent_state"
down_revision = "0002_crop_revision"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "publications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
        sa.ForeignKeyConstraint(["owner_id", "organization_id"], ["users.id", "users.organization_id"], name="fk_publications_owner_organization"),
        sa.ForeignKeyConstraint(["job_id", "owner_id", "organization_id"], ["jobs.id", "jobs.owner_id", "jobs.organization_id"], name="fk_publications_job_owner"),
        sa.UniqueConstraint("id", "owner_id", "organization_id", name="uq_publications_owner_organization"),
        sa.UniqueConstraint("job_id", name="uq_publications_job"),
    )
    op.create_index("ix_publications_owner_id", "publications", ["owner_id"])
    op.create_table(
        "migration_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("namespace", sa.String(80), nullable=False, unique=True),
        sa.Column("source_checksum", sa.String(64), nullable=False),
        sa.Column("mapping_checksum", sa.String(64), nullable=False),
        sa.Column("report", sa.JSON, nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_table(
        "legacy_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("migration_id", sa.String(36), sa.ForeignKey("migration_runs.id"), nullable=False),
        sa.Column("source_key", sa.String(240), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("targets", sa.JSON, nullable=False),
        sa.UniqueConstraint("migration_id", "source_key", name="uq_legacy_record_source"),
        sa.CheckConstraint("disposition IN ('imported', 'quarantined')", name="ck_legacy_disposition"),
    )


def downgrade():
    op.drop_table("legacy_records")
    op.drop_table("migration_runs")
    op.drop_table("publications")

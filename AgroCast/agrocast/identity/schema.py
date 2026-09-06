from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, ForeignKey, ForeignKeyConstraint,
    Integer, JSON, MetaData, String, Table, UniqueConstraint,
)

REVISION = "0003_persistent_state"
metadata = MetaData()
organizations = Table(
    "organizations", metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)
users = Table(
    "users", metadata,
    Column("id", String(36), primary_key=True),
    Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False),
    Column("username", String(64), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("role", String(16), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("id", "organization_id", name="uq_users_organization"),
    CheckConstraint("role IN ('reader', 'operator', 'admin')", name="ck_users_role"),
)
sessions = Table(
    "sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("csrf_token", String(43), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False, index=True),
)
login_limits = Table(
    "login_limits", metadata,
    Column("key", String(80), primary_key=True),
    Column("window", BigInteger, nullable=False),
    Column("attempts", Integer, nullable=False),
)
audit_events = Table(
    "identity_events", metadata,
    Column("id", String(36), primary_key=True),
    Column("organization_id", String(36), ForeignKey("organizations.id"), nullable=False, index=True),
    Column("actor_id", String(36), nullable=False),
    Column("action", String(64), nullable=False),
    Column("subject_id", String(36), nullable=False),
    Column("created_at", BigInteger, nullable=False),
)


def owned_table(name):
    return Table(
        name, metadata,
        Column("id", String(36), primary_key=True),
        Column("organization_id", String(36), nullable=False),
        Column("owner_id", String(36), nullable=False, index=True),
        Column("data", JSON, nullable=False),
        Column("created_at", BigInteger, nullable=False),
        Column("updated_at", BigInteger, nullable=False),
        ForeignKeyConstraint(
            ["owner_id", "organization_id"], ["users.id", "users.organization_id"],
            name=f"fk_{name}_owner_organization",
        ),
        UniqueConstraint("id", "owner_id", "organization_id", name=f"uq_{name}_owner_organization"),
    )


fields = owned_table("fields")
crops = owned_table("crops")
crops.append_column(Column("revision", Integer, nullable=False, server_default="1"))
crops.append_constraint(CheckConstraint("revision >= 1", name="ck_crops_revision"))
subscriptions = owned_table("subscriptions")
subscriptions.append_column(Column("field_id", String(36), nullable=False))
subscriptions.append_constraint(ForeignKeyConstraint(
    ["field_id", "owner_id", "organization_id"],
    ["fields.id", "fields.owner_id", "fields.organization_id"],
    name="fk_subscriptions_field_owner", ondelete="CASCADE",
))
jobs = owned_table("jobs")
jobs.append_column(Column("status", String(16), nullable=False))
jobs.append_constraint(CheckConstraint(
    "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_jobs_status",
))
publications = owned_table("publications")
publications.append_column(Column("job_id", String(36), nullable=False))
publications.append_column(Column("checksum", String(64), nullable=False))
publications.append_constraint(ForeignKeyConstraint(
    ["job_id", "owner_id", "organization_id"], ["jobs.id", "jobs.owner_id", "jobs.organization_id"], name="fk_publications_job_owner",
))
publications.append_constraint(UniqueConstraint("job_id", name="uq_publications_job"))
migration_runs = Table(
    "migration_runs", metadata,
    Column("id", String(36), primary_key=True),
    Column("namespace", String(80), nullable=False, unique=True),
    Column("source_checksum", String(64), nullable=False),
    Column("mapping_checksum", String(64), nullable=False),
    Column("report", JSON, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)
legacy_records = Table(
    "legacy_records", metadata,
    Column("id", String(36), primary_key=True),
    Column("migration_id", String(36), ForeignKey("migration_runs.id"), nullable=False),
    Column("source_key", String(240), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("disposition", String(16), nullable=False),
    Column("data", JSON, nullable=False),
    Column("checksum", String(64), nullable=False),
    Column("targets", JSON, nullable=False),
    UniqueConstraint("migration_id", "source_key", name="uq_legacy_record_source"),
    CheckConstraint("disposition IN ('imported', 'quarantined')", name="ck_legacy_disposition"),
)
RESOURCES = {"fields": fields, "crops": crops, "subscriptions": subscriptions, "jobs": jobs, "publications": publications}

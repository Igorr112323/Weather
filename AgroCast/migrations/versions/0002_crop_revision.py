from alembic import op
import sqlalchemy as sa

revision = "0002_crop_revision"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("crops") as batch:
        batch.add_column(sa.Column("revision", sa.Integer, nullable=False, server_default="1"))
        batch.create_check_constraint("ck_crops_revision", "revision >= 1")


def downgrade():
    with op.batch_alter_table("crops") as batch:
        batch.drop_constraint("ck_crops_revision", type_="check")
        batch.drop_column("revision")

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select

from agrocast.identity.database import check_schema, migrate
from agrocast.identity.schema import crops, organizations, users
from agrocast.identity.credentials import IdentityError
from agrocast.store.results import fingerprint


def test_crop_revision_migration_preserves_existing_records(identity_engine, account_password_hash):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    with identity_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_identity")
    with pytest.raises(ValueError):
        check_schema(identity_engine)
    organization_id, user_id, crop_id = (str(uuid4()) for _ in range(3))
    payload = {"name": 'Архив "<сорт>"', "breeder": "Селекционер"}
    with identity_engine.begin() as connection:
        connection.execute(insert(organizations).values(id=organization_id, name="Org", active=True, created_at=1))
        connection.execute(insert(users).values(id=user_id, organization_id=organization_id, username="migration_user", password_hash=account_password_hash, role="admin", active=True, created_at=1))
        connection.execute(insert(crops).values(id=crop_id, organization_id=organization_id, owner_id=user_id, data=payload, created_at=1, updated_at=1))
    migrate(identity_engine)
    check_schema(identity_engine)
    with identity_engine.connect() as connection:
        row = connection.execute(select(crops)).mappings().one()
        assert row["id"] == crop_id and row["owner_id"] == user_id and row["organization_id"] == organization_id
        assert row["data"] == payload and row["revision"] == 1
        assert row["created_at"] == row["updated_at"] == 1
    migrate(identity_engine)
    with identity_engine.connect() as connection:
        assert len(connection.execute(select(crops)).all()) == 1


def test_variety_snapshot_is_scoped_and_contains_actual_revision_and_content(identity, clients, account_password):
    admin = clients("admin_a")
    crop = admin.post("/api/crops", json={"name": "Гибрид", "gdd": 2200}).json()["crop"]
    actor = identity.login("operator_a", account_password).principal
    snapshot = identity.variety_snapshot(actor, crop["id"], 1)
    assert snapshot.content_sha256 == fingerprint(crop["data"])
    admin.put("/api/crops/" + crop["id"], json={"name": "Гибрид", "gdd": 2300})
    with pytest.raises(IdentityError) as stale:
        identity.variety_snapshot(actor, crop["id"], 1)
    assert stale.value.status == 409
    assert identity.variety_snapshot(actor, crop["id"], 2).content_sha256 != snapshot.content_sha256
    foreign = identity.login("operator_b", account_password).principal
    with pytest.raises(IdentityError) as denied:
        identity.variety_snapshot(foreign, crop["id"], 2)
    assert denied.value.status == 404

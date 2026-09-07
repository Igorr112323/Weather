import hashlib
import re
import secrets
import time
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from agrocast.identity.credentials import IdentityError, LoginResult, Principal, Role, passwords
from agrocast.identity.database import IdentitySettings, check_schema
from agrocast.identity.schema import (
    RESOURCES, audit_events, fields, login_limits, organizations, sessions, users,
)

ALL_ROLES = frozenset(Role)
WRITE_ROLES = frozenset({Role.OPERATOR, Role.ADMIN})
ADMIN_ROLES = frozenset({Role.ADMIN})


def username_key(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.@-]{1,64}", value):
        raise IdentityError("invalid_username", 422)
    return value


def token_hash(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class IdentityService:
    def __init__(self, engine, public_origin, session_seconds=28800, clock=time.time):
        IdentitySettings("", public_origin, session_seconds)
        check_schema(engine)
        self.engine = engine
        self.public_origin = public_origin
        self.session_seconds = session_seconds
        self.clock = clock

    def _now(self):
        return int(self.clock())

    def _event(self, connection, actor_id, organization_id, action, subject_id):
        connection.execute(insert(audit_events).values(
            id=str(uuid4()), organization_id=organization_id, actor_id=actor_id,
            action=action, subject_id=subject_id, created_at=self._now(),
        ))

    def _lock_organization(self, connection, organization_id):
        row = connection.execute(select(organizations.c.active).where(
            organizations.c.id == organization_id,
        ).with_for_update()).first()
        if row is None or not row.active:
            raise IdentityError("auth_required", 401)

    def _principal(self, connection, digest):
        row = connection.execute(select(
            users.c.id, users.c.organization_id, users.c.username, users.c.role,
            sessions.c.token_hash.label("session_hash"), sessions.c.csrf_token,
        ).select_from(sessions.join(users).join(organizations)).where(
            sessions.c.token_hash == digest, sessions.c.expires_at > self._now(),
            users.c.active.is_(True), organizations.c.active.is_(True),
        )).mappings().first()
        if row is None:
            raise IdentityError("auth_required", 401)
        return Principal(**{**row, "role": Role(row["role"])})

    def authenticate(self, token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise IdentityError("auth_required", 401)
        with self.engine.connect() as connection:
            return self._principal(connection, token_hash(token))

    def _actor(self, connection, principal, roles=ALL_ROLES):
        current = self._principal(connection, principal.session_hash)
        if current.id != principal.id or current.organization_id != principal.organization_id:
            raise IdentityError("auth_required", 401)
        if current.role not in roles:
            raise IdentityError("role_forbidden", 403)
        return current

    def _reserve_attempt(self, key):
        now = self._now()
        window = now // 60
        with self.engine.begin() as connection:
            connection.execute(select(login_limits.c.key).where(login_limits.c.key == "global").with_for_update()).one()
            connection.execute(delete(login_limits).where(login_limits.c.key != "global", login_limits.c.window < window - 1))
            for bucket, maximum in (("global", 30), ("user:" + hashlib.sha256(key.encode()).hexdigest(), 5)):
                row = connection.execute(select(login_limits).where(login_limits.c.key == bucket)).mappings().first()
                attempts = row["attempts"] if row and row["window"] == window else 0
                if attempts >= maximum:
                    raise IdentityError("authentication_rate_limited", 429, 60 - now % 60)
                values = {"window": window, "attempts": attempts + 1}
                if row:
                    connection.execute(update(login_limits).where(login_limits.c.key == bucket).values(**values))
                else:
                    connection.execute(insert(login_limits).values(key=bucket, **values))

    def login(self, username, password):
        username = username_key(username)
        if not 1 <= len(password) <= 128:
            raise IdentityError("invalid_credentials", 401)
        self._reserve_attempt(username)
        with self.engine.connect() as connection:
            row = connection.execute(select(users).where(users.c.username == username)).mappings().first()
        encoded = row["password_hash"] if row else passwords.dummy_hash
        valid = passwords.verify(encoded, password)
        if not valid or row is None or not row["active"]:
            raise IdentityError("invalid_credentials", 401)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = self._now()
        with self.engine.begin() as connection:
            self._lock_organization(connection, row["organization_id"])
            current = connection.execute(select(users).where(users.c.id == row["id"])).mappings().one()
            if not current["active"] or not secrets.compare_digest(encoded, current["password_hash"]):
                raise IdentityError("invalid_credentials", 401)
            connection.execute(delete(sessions).where(sessions.c.expires_at <= now))
            stale = list(connection.execute(select(sessions.c.token_hash).where(
                sessions.c.user_id == current["id"],
            ).order_by(sessions.c.created_at.desc(), sessions.c.token_hash).offset(4)).scalars())
            if stale:
                connection.execute(delete(sessions).where(sessions.c.token_hash.in_(stale)))
            connection.execute(insert(sessions).values(
                token_hash=token_hash(token), user_id=current["id"], csrf_token=csrf,
                created_at=now, expires_at=now + self.session_seconds,
            ))
            self._event(connection, current["id"], current["organization_id"], "session.login", current["id"])
            principal = self._principal(connection, token_hash(token))
        return LoginResult(principal, token, now + self.session_seconds)

    def logout(self, principal):
        with self.engine.begin() as connection:
            self._lock_organization(connection, principal.organization_id)
            self._actor(connection, principal)
            connection.execute(delete(sessions).where(sessions.c.token_hash == principal.session_hash))
            self._event(connection, principal.id, principal.organization_id, "session.logout", principal.id)

    def bootstrap(self, organization_name, username, password):
        username = username_key(username)
        organization_name = organization_name.strip()
        if not 1 <= len(organization_name) <= 120:
            raise IdentityError("invalid_organization", 422)
        encoded = passwords.hash(password)
        organization_id, user_id = str(uuid4()), str(uuid4())
        try:
            with self.engine.begin() as connection:
                connection.execute(insert(organizations).values(
                    id=organization_id, name=organization_name, active=True, created_at=self._now(),
                ))
                connection.execute(insert(users).values(
                    id=user_id, organization_id=organization_id, username=username,
                    password_hash=encoded, role=Role.ADMIN.value, active=True, created_at=self._now(),
                ))
                self._event(connection, user_id, organization_id, "organization.bootstrap", user_id)
        except IntegrityError:
            raise IdentityError("identity_conflict", 409) from None
        return {"id": user_id, "organization_id": organization_id, "username": username, "role": "admin"}

    def list_users(self, principal, limit=100):
        with self.engine.connect() as connection:
            current = self._actor(connection, principal, ADMIN_ROLES)
            return [dict(row) for row in connection.execute(select(
                users.c.id, users.c.username, users.c.role, users.c.active, users.c.organization_id,
            ).where(users.c.organization_id == current.organization_id).order_by(users.c.username).limit(limit)).mappings()]

    def create_user(self, principal, username, password, role):
        role = Role(role)
        username = username_key(username)
        with self.engine.connect() as connection:
            self._actor(connection, principal, ADMIN_ROLES)
        encoded = passwords.hash(password)
        user_id = str(uuid4())
        try:
            with self.engine.begin() as connection:
                self._lock_organization(connection, principal.organization_id)
                self._actor(connection, principal, ADMIN_ROLES)
                connection.execute(insert(users).values(
                    id=user_id, organization_id=principal.organization_id, username=username,
                    password_hash=encoded, role=role.value, active=True, created_at=self._now(),
                ))
                self._event(connection, principal.id, principal.organization_id, "user.created", user_id)
        except IntegrityError:
            raise IdentityError("identity_conflict", 409) from None
        return {"id": user_id, "organization_id": principal.organization_id, "username": username, "role": role.value, "active": True}

    def update_user(self, principal, user_id, role=None, active=None):
        with self.engine.begin() as connection:
            self._lock_organization(connection, principal.organization_id)
            self._actor(connection, principal, ADMIN_ROLES)
            target = connection.execute(select(users).where(
                users.c.id == user_id, users.c.organization_id == principal.organization_id,
            )).mappings().first()
            if target is None:
                raise IdentityError("resource_not_found", 404)
            next_role = Role(role) if role is not None else Role(target["role"])
            next_active = active if active is not None else target["active"]
            if target["active"] and target["role"] == "admin" and (not next_active or next_role != Role.ADMIN):
                count = connection.execute(select(func.count()).select_from(users).where(
                    users.c.organization_id == principal.organization_id, users.c.role == "admin", users.c.active.is_(True),
                )).scalar_one()
                if count <= 1:
                    raise IdentityError("last_admin_required", 409)
            connection.execute(update(users).where(users.c.id == user_id).values(role=next_role.value, active=next_active))
            connection.execute(delete(sessions).where(sessions.c.user_id == user_id))
            self._event(connection, principal.id, principal.organization_id, "user.access_changed", user_id)
        return {"id": user_id, "organization_id": principal.organization_id, "username": target["username"], "role": next_role.value, "active": next_active}

    def change_password(self, principal, current_password, new_password):
        self._reserve_attempt("password:" + principal.id)
        with self.engine.connect() as connection:
            self._actor(connection, principal)
            encoded = connection.execute(select(users.c.password_hash).where(users.c.id == principal.id)).scalar_one()
        if not passwords.verify(encoded, current_password):
            raise IdentityError("invalid_credentials", 401)
        replacement = passwords.hash(new_password)
        with self.engine.begin() as connection:
            self._lock_organization(connection, principal.organization_id)
            self._actor(connection, principal)
            current = connection.execute(select(users.c.password_hash).where(users.c.id == principal.id)).scalar_one()
            if not secrets.compare_digest(encoded, current):
                raise IdentityError("auth_required", 401)
            connection.execute(update(users).where(users.c.id == principal.id).values(password_hash=replacement))
            connection.execute(delete(sessions).where(sessions.c.user_id == principal.id))
            self._event(connection, principal.id, principal.organization_id, "user.password_changed", principal.id)

    def events(self, principal, limit=100):
        with self.engine.connect() as connection:
            current = self._actor(connection, principal, ADMIN_ROLES)
            return [dict(row) for row in connection.execute(select(audit_events).where(
                audit_events.c.organization_id == current.organization_id,
            ).order_by(audit_events.c.created_at.desc(), audit_events.c.id).limit(limit)).mappings()]

    def _scope(self, table, principal):
        condition = table.c.organization_id == principal.organization_id
        return condition if table.name == "crops" else condition & (table.c.owner_id == principal.id)

    def list_resources(self, kind, principal, limit=100):
        table = RESOURCES[kind]
        with self.engine.connect() as connection:
            current = self._actor(connection, principal)
            return [dict(row) for row in connection.execute(select(table).where(
                self._scope(table, current),
            ).order_by(table.c.created_at.desc(), table.c.id).limit(limit)).mappings()]

    def get_resource(self, kind, principal, resource_id):
        table = RESOURCES[kind]
        with self.engine.connect() as connection:
            current = self._actor(connection, principal)
            row = connection.execute(select(table).where(
                table.c.id == resource_id, self._scope(table, current),
            )).mappings().first()
            if row is None:
                raise IdentityError("resource_not_found", 404)
            return dict(row)

    def write_resource(self, kind, principal, data, resource_id=None):
        if kind not in {"fields", "crops", "subscriptions"}:
            raise IdentityError("pilot_operation_disabled", 403)
        table = RESOURCES[kind]
        roles = ADMIN_ROLES if kind == "crops" else WRITE_ROLES
        with self.engine.begin() as connection:
            self._lock_organization(connection, principal.organization_id)
            current = self._actor(connection, principal, roles)
            existing = None
            if resource_id is not None:
                existing = connection.execute(select(table).where(
                    table.c.id == resource_id, self._scope(table, current),
                )).mappings().first()
                if existing is None:
                    raise IdentityError("resource_not_found", 404)
            values = {"data": data, "updated_at": self._now()}
            if kind == "crops" and existing:
                values["revision"] = table.c.revision + 1
            if kind == "subscriptions":
                owned_field = connection.execute(select(fields.c.id).where(
                    fields.c.id == data["field_id"], self._scope(fields, current),
                )).first()
                if owned_field is None:
                    raise IdentityError("resource_not_found", 404)
                if data.get("active") is not False:
                    raise IdentityError("pilot_operation_disabled", 403)
                values["field_id"] = data["field_id"]
                if data.get("variety_id") is not None:
                    crop_table = RESOURCES["crops"]
                    crop = connection.execute(select(crop_table).where(
                        crop_table.c.id == data["variety_id"], self._scope(crop_table, current),
                    )).mappings().first()
                    if crop is None:
                        raise IdentityError("resource_not_found", 404)
                    if crop["revision"] != data.get("variety_revision"):
                        raise IdentityError("resource_version_conflict", 409)
            if existing:
                connection.execute(update(table).where(table.c.id == resource_id, self._scope(table, current)).values(**values))
            else:
                resource_id = str(uuid4())
                connection.execute(insert(table).values(
                    id=resource_id, owner_id=current.id, organization_id=current.organization_id,
                    created_at=self._now(), **values,
                ))
            self._event(connection, current.id, current.organization_id, kind + (".updated" if existing else ".created"), resource_id)
            return dict(connection.execute(select(table).where(table.c.id == resource_id)).mappings().one())

    def delete_resource(self, kind, principal, resource_id):
        if kind == "publications":
            raise IdentityError("immutable_publication", 403)
        table = RESOURCES[kind]
        roles = ADMIN_ROLES if kind == "crops" else WRITE_ROLES
        with self.engine.begin() as connection:
            self._lock_organization(connection, principal.organization_id)
            current = self._actor(connection, principal, roles)
            row = connection.execute(select(table).where(
                table.c.id == resource_id, self._scope(table, current),
            )).mappings().first()
            if row is None:
                raise IdentityError("resource_not_found", 404)
            if kind == "jobs" and row["status"] in {"queued", "running"}:
                raise IdentityError("job_still_active", 409)
            if kind == "jobs" and connection.execute(select(RESOURCES["publications"].c.id).where(RESOURCES["publications"].c.job_id == resource_id)).first():
                raise IdentityError("job_has_publication", 409)
            connection.execute(delete(table).where(table.c.id == resource_id, self._scope(table, current)))
            self._event(connection, current.id, current.organization_id, kind + ".deleted", resource_id)


    def export_user(self, principal):
        def plain(value):
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            if isinstance(value, dict):
                return {key: plain(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [plain(item) for item in value]
            return str(value)

        with self.engine.connect() as connection:
            actor = self._actor(connection, principal, ALL_ROLES)
            user_row = connection.execute(select(users).where(users.c.id == actor.id)).mappings().one()
            out = {
                "user": {key: plain(user_row[key]) for key in user_row.keys() if key not in ("password_hash", "password_salt")},
                "resources": {},
            }
            for kind, table in RESOURCES.items():
                if "owner_id" in table.c:
                    rows = connection.execute(select(table).where(table.c.owner_id == actor.id)).mappings().all()
                elif kind == "crops":
                    rows = connection.execute(select(table).where(table.c.organization_id == actor.organization_id)).mappings().all()
                else:
                    rows = []
                out["resources"][kind] = [{key: plain(row[key]) for key in row.keys()} for row in rows]
            out["sessions_active"] = connection.execute(select(func.count()).select_from(sessions).where(sessions.c.user_id == actor.id)).scalar_one()
        return out

    def delete_account(self, principal, password):
        with self.engine.begin() as connection:
            user_row = connection.execute(select(users).where(users.c.id == principal.id)).mappings().first()
            if user_row is None or not passwords.verify(user_row["password_hash"], password):
                raise IdentityError("invalid_credentials", 403)
            self._lock_organization(connection, principal.organization_id)
            actor = self._actor(connection, principal, ALL_ROLES)
            jobs = RESOURCES["jobs"]
            busy = connection.execute(select(func.count()).select_from(jobs).where(
                jobs.c.owner_id == actor.id, jobs.c.status.in_(("queued", "running")),
            )).scalar_one()
            if busy:
                raise IdentityError("job_still_active", 409)
            counts = {}
            self._event(connection, actor.id, actor.organization_id, "account.deleted", actor.id)
            for kind in ("publications", "jobs", "subscriptions", "fields"):
                table = RESOURCES[kind]
                result = connection.execute(delete(table).where(table.c.owner_id == actor.id))
                counts[kind] = result.rowcount
            crops = RESOURCES["crops"]
            counts["crops"] = connection.execute(delete(crops).where(crops.c.owner_id == actor.id)).rowcount
            connection.execute(delete(sessions).where(sessions.c.user_id == actor.id))
            connection.execute(delete(users).where(users.c.id == actor.id))
            remaining = connection.execute(select(func.count()).select_from(users).where(users.c.organization_id == actor.organization_id)).scalar_one()
            if remaining == 0:
                connection.execute(delete(organizations).where(organizations.c.id == actor.organization_id))
                counts["organizations"] = 1
            counts["sessions"] = 0
        return {"deleted": counts}


    def variety_snapshot(self, principal, variety_id, revision):
        from agrocast.store.results import VarietySnapshot, fingerprint

        crop = self.get_resource("crops", principal, str(variety_id))
        if crop["revision"] != revision:
            raise IdentityError("resource_version_conflict", 409)
        return VarietySnapshot(id=crop["id"], revision=crop["revision"], content_sha256=fingerprint(crop["data"]))

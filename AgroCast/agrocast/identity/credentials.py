import secrets
import threading
from dataclasses import dataclass, field
from enum import StrEnum

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


class Role(StrEnum):
    READER = "reader"
    OPERATOR = "operator"
    ADMIN = "admin"


class IdentityError(Exception):
    def __init__(self, code: str, status: int, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Principal:
    id: str
    organization_id: str
    username: str
    role: Role
    session_hash: str = field(repr=False)
    csrf_token: str = field(repr=False)

    def public(self):
        return {"id": self.id, "organization_id": self.organization_id, "username": self.username, "role": self.role.value}


@dataclass(frozen=True)
class LoginResult:
    principal: Principal
    token: str = field(repr=False)
    expires_at: int


class Passwords:
    def __init__(self):
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
        self.capacity = threading.BoundedSemaphore(2)
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    def hash(self, password: str) -> str:
        if not 15 <= len(password) <= 128 or password.isspace():
            raise IdentityError("password_policy", 422)
        if not self.capacity.acquire(blocking=False):
            raise IdentityError("authentication_busy", 503, 2)
        try:
            return self.hasher.hash(password)
        finally:
            self.capacity.release()

    def verify(self, encoded: str, password: str) -> bool:
        if not self.capacity.acquire(blocking=False):
            raise IdentityError("authentication_busy", 503, 2)
        try:
            return self.hasher.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False
        finally:
            self.capacity.release()


passwords = Passwords()

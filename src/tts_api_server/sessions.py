"""Persist anonymous bearer credentials as hashes, with bounded admission."""

import hashlib
import secrets
import uuid

from .assets import timestamp
from .errors import DomainError


class Sessions:
    def __init__(self, store, lock, settings):
        self.store, self.lock, self.settings = store, lock, settings

    def create(self):
        if not self.settings.anonymous_sessions:
            raise DomainError("unsupported_feature", "Anonymous sessions are not enabled.")
        with self.lock:
            now = self.settings.clock()
            self.store.db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            count = self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            if count >= self.settings.max_sessions:
                raise DomainError(
                    "session_limit_reached", "Active session limit reached.", retryable=True
                )
            token = secrets.token_urlsafe(32)
            identifier = "session_" + uuid.uuid4().hex
            expires = now + self.settings.session_ttl
            self.store.db.execute(
                "INSERT INTO sessions VALUES (?,?,?)", (self.digest(token), identifier, expires)
            )
        return {
            "session_id": identifier,
            "access_token": token,
            "token_type": "Bearer",
            "expires_at": timestamp(expires),
        }

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token):
        if not token or len(token) > 512:
            return None
        with self.lock:
            row = self.store.db.execute(
                "SELECT id,expires FROM sessions WHERE token_hash=?", (self.digest(token),)
            ).fetchone()
            return row[0] if row and row[1] > self.settings.clock() else None

    def active(self, identifier):
        with self.lock:
            row = self.store.db.execute(
                "SELECT expires FROM sessions WHERE id=?", (identifier,)
            ).fetchone()
            return bool(row and row[0] > self.settings.clock())

    def revoke(self, identifier):
        if not self.settings.anonymous_sessions:
            raise DomainError("unsupported_feature", "Anonymous sessions are not enabled.")
        with self.lock:
            self.store.db.execute("DELETE FROM sessions WHERE id=?", (identifier,))

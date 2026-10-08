#!/usr/bin/env python3
"""Persistent, process-safe leases for the JST print coordinator.

Local task reservations and permanent exclusions survive application restarts.
SQLite BEGIN IMMEDIATE serializes local threads and processes before any
browser write or print. This module requires no network service.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


ACTIVE = "ACTIVE"
RELEASED = "RELEASED"
COMPLETED = "COMPLETED"
EXPIRED = "EXPIRED"
MAX_ACTIVE_PER_WORKSTATION = 10
COMPLETION_REASONS = frozenset(
    {"PRINTED", "TERMINAL", "OPERATOR_SKIPPED", "UNCERTAIN_ACTION"}
)


class LeaseConflict(RuntimeError):
    """The caller does not own a current active lease."""


def utc_text(timestamp: Optional[float] = None) -> str:
    value = time.time() if timestamp is None else float(timestamp)
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class Lease:
    o_id: str
    io_id: str
    workstation_id: str
    claim_token: str
    state: str
    expires_epoch: float
    lease_ttl_seconds: int
    completion_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "o_id": self.o_id,
            "io_id": self.io_id,
            "workstation_id": self.workstation_id,
            "claim_token": self.claim_token,
            "state": self.state,
            "expires_at": utc_text(self.expires_epoch),
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "completion_reason": self.completion_reason,
        }


class LeaseStore:
    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: int = 300,
        clock=time.time,
    ) -> None:
        if not 30 <= int(ttl_seconds) <= 3600:
            raise ValueError("lease TTL must be between 30 and 3600 seconds")
        self.path = Path(path)
        self.ttl_seconds = int(ttl_seconds)
        self.clock = clock
        self._schema_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _init_schema(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS leases (
                    o_id TEXT NOT NULL,
                    io_id TEXT NOT NULL,
                    workstation_id TEXT NOT NULL,
                    claim_token TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN ('ACTIVE', 'RELEASED', 'COMPLETED', 'EXPIRED')
                    ),
                    claimed_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    completed_at REAL,
                    completion_reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (o_id, io_id)
                );
                CREATE INDEX IF NOT EXISTS leases_state_expiry
                    ON leases(state, expires_at);
                CREATE TABLE IF NOT EXISTS operator_skips (
                    o_id TEXT NOT NULL,
                    io_id TEXT NOT NULL,
                    workstation_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    skipped_at REAL NOT NULL,
                    PRIMARY KEY (o_id, io_id)
                );
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(leases)").fetchall()
            }
            if "completion_reason" not in columns:
                connection.execute(
                    "ALTER TABLE leases ADD COLUMN "
                    "completion_reason TEXT NOT NULL DEFAULT ''"
                )
            now = float(self.clock())
            self._begin(connection)
            self._expire_stale(connection, now)
            # V2 allows a workstation to hold one bounded print batch. The
            # exact pair/token remains independently leased and recoverable.
            duplicate_orders = connection.execute(
                """SELECT o_id FROM leases
                   WHERE state='ACTIVE'
                   GROUP BY o_id HAVING COUNT(*) > 1"""
            ).fetchall()
            for row in duplicate_orders:
                connection.execute(
                    """UPDATE leases SET state='EXPIRED', updated_at=?
                       WHERE o_id=? AND state='ACTIVE'""",
                    (now, str(row["o_id"])),
                )
            connection.execute("DROP INDEX IF EXISTS leases_one_active_per_workstation")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS
                   leases_one_active_per_order
                   ON leases(o_id) WHERE state='ACTIVE'"""
            )
            connection.commit()

    def _begin(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _expire_stale(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """UPDATE leases SET state='EXPIRED', updated_at=?
               WHERE state='ACTIVE' AND expires_at<=?""",
            (now, now),
        )

    @staticmethod
    def _workstation_at_capacity(
        connection: sqlite3.Connection,
        workstation_id: str,
        o_id: str,
        io_id: str,
    ) -> bool:
        row = connection.execute(
            """SELECT COUNT(*) AS active_count FROM leases
               WHERE workstation_id=? AND state='ACTIVE'
                 AND NOT (o_id=? AND io_id=?)""",
            (str(workstation_id), str(o_id), str(io_id)),
        ).fetchone()
        return int(row["active_count"] if row is not None else 0) >= MAX_ACTIVE_PER_WORKSTATION

    @staticmethod
    def _other_active_for_order(
        connection: sqlite3.Connection,
        o_id: str,
        io_id: str,
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            """SELECT * FROM leases
               WHERE o_id=? AND state='ACTIVE' AND io_id<>?
               LIMIT 1""",
            (str(o_id), str(io_id)),
        ).fetchone()

    def _lease_from_row(self, row: sqlite3.Row) -> Lease:
        return Lease(
            o_id=str(row["o_id"]),
            io_id=str(row["io_id"]),
            workstation_id=str(row["workstation_id"]),
            claim_token=str(row["claim_token"]),
            state=str(row["state"]),
            expires_epoch=float(row["expires_at"]),
            lease_ttl_seconds=self.ttl_seconds,
            completion_reason=str(row["completion_reason"] or ""),
        )

    def claim(self, o_id: str, io_id: str, workstation_id: str) -> Optional[Lease]:
        """Atomically claim a pair with retry-safe workstation idempotency.

        The same workstation retrying the same unexpired pair receives the
        original token and expiry. A workstation may hold at most one bounded
        batch, while another workstation never receives an existing pair.
        """

        now = float(self.clock())
        expires = now + self.ttl_seconds
        with self._connect() as connection:
            self._begin(connection)
            self._expire_stale(connection, now)
            if connection.execute(
                "SELECT 1 FROM operator_skips WHERE o_id=? AND io_id=?",
                (str(o_id), str(io_id)),
            ).fetchone() is not None:
                connection.commit()
                return None
            row = connection.execute(
                "SELECT * FROM leases WHERE o_id=? AND io_id=?",
                (str(o_id), str(io_id)),
            ).fetchone()
            if row is not None:
                state = str(row["state"])
                if state == COMPLETED:
                    connection.commit()
                    return None
                if state == ACTIVE:
                    if str(row["workstation_id"]) == str(workstation_id):
                        connection.commit()
                        return self._lease_from_row(row)
                    connection.commit()
                    return None
            if self._other_active_for_order(
                connection, str(o_id), str(io_id)
            ) is not None:
                connection.commit()
                return None
            if self._workstation_at_capacity(
                connection, workstation_id, str(o_id), str(io_id)
            ):
                connection.commit()
                return None
            token = secrets.token_urlsafe(32)
            if row is not None:
                connection.execute(
                    """UPDATE leases
                       SET workstation_id=?, claim_token=?, state='ACTIVE',
                           claimed_at=?, updated_at=?, expires_at=?, completed_at=NULL,
                           completion_reason=''
                       WHERE o_id=? AND io_id=?""",
                    (
                        workstation_id,
                        token,
                        now,
                        now,
                        expires,
                        str(o_id),
                        str(io_id),
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO leases
                       (o_id, io_id, workstation_id, claim_token, state,
                        claimed_at, updated_at, expires_at, completed_at)
                       VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?, NULL)""",
                    (
                        str(o_id),
                        str(io_id),
                        workstation_id,
                        token,
                        now,
                        now,
                        expires,
                    ),
                )
            connection.commit()
        return Lease(
            o_id=str(o_id),
            io_id=str(io_id),
            workstation_id=workstation_id,
            claim_token=token,
            state=ACTIVE,
            expires_epoch=expires,
            lease_ttl_seconds=self.ttl_seconds,
            completion_reason="",
        )

    def force_skip(
        self, o_id: str, io_id: str, workstation_id: str, reason: str
    ) -> dict[str, object]:
        """Persist a manual exclusion independently of lease/proof availability.

        Updating the active lease in the same transaction revokes further
        renew/inspect attempts. Preserve prior completed print evidence.
        The separate exclusion survives expiry and future lease cleanup.
        """

        now = float(self.clock())
        with self._connect() as connection:
            self._begin(connection)
            connection.execute(
                """INSERT INTO operator_skips
                   (o_id, io_id, workstation_id, reason, skipped_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(o_id, io_id) DO NOTHING""",
                (str(o_id), str(io_id), workstation_id, reason, now),
            )
            connection.execute(
                """UPDATE leases SET state='COMPLETED', updated_at=?,
                   expires_at=?, completed_at=?, completion_reason='OPERATOR_SKIPPED'
                   WHERE o_id=? AND io_id=? AND state!='COMPLETED'""",
                (now, now, now, str(o_id), str(io_id)),
            )
            row = connection.execute(
                "SELECT * FROM operator_skips WHERE o_id=? AND io_id=?",
                (str(o_id), str(io_id)),
            ).fetchone()
            connection.commit()
        assert row is not None
        return dict(row)

    def active_for_workstation(self, workstation_id: str) -> list[Lease]:
        """Return the workstation's bounded active batch after expiry cleanup.

        Planning uses this before scanning candidates so an HTTP retry returns
        the original pair and token instead of silently switching orders.
        """

        now = float(self.clock())
        with self._connect() as connection:
            self._begin(connection)
            self._expire_stale(connection, now)
            rows = connection.execute(
                """SELECT * FROM leases
                   WHERE workstation_id=? AND state='ACTIVE'
                   ORDER BY claimed_at LIMIT ?""",
                (str(workstation_id), MAX_ACTIVE_PER_WORKSTATION + 1),
            ).fetchall()
            if len(rows) > MAX_ACTIVE_PER_WORKSTATION:
                connection.rollback()
                raise LeaseConflict("workstation_batch_exceeds_limit")
            connection.commit()
        return [self._lease_from_row(row) for row in rows]

    def _owned_active(
        self,
        connection: sqlite3.Connection,
        o_id: str,
        io_id: str,
        workstation_id: str,
        claim_token: str,
        now: float,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM leases WHERE o_id=? AND io_id=?",
            (str(o_id), str(io_id)),
        ).fetchone()
        if row is None:
            raise LeaseConflict("lease_not_found")
        if not secrets.compare_digest(str(row["claim_token"]), str(claim_token)):
            raise LeaseConflict("lease_token_mismatch")
        if str(row["workstation_id"]) != str(workstation_id):
            raise LeaseConflict("lease_owner_mismatch")
        if str(row["state"]) != ACTIVE:
            raise LeaseConflict(f"lease_not_active:{row['state']}")
        if float(row["expires_at"]) <= now:
            connection.execute(
                "UPDATE leases SET state='EXPIRED', updated_at=? "
                "WHERE o_id=? AND io_id=? AND claim_token=?",
                (now, str(o_id), str(io_id), str(claim_token)),
            )
            raise LeaseConflict("lease_expired")
        return row

    def require_active(
        self,
        o_id: str,
        io_id: str,
        workstation_id: str,
        claim_token: str,
    ) -> Lease:
        now = float(self.clock())
        with self._connect() as connection:
            self._begin(connection)
            self._expire_stale(connection, now)
            try:
                row = self._owned_active(
                    connection, o_id, io_id, workstation_id, claim_token, now
                )
            except LeaseConflict:
                connection.commit()
                raise
            connection.commit()
        return self._lease_from_row(row)

    def renew(
        self,
        o_id: str,
        io_id: str,
        workstation_id: str,
        claim_token: str,
    ) -> Lease:
        now = float(self.clock())
        expires = now + self.ttl_seconds
        with self._connect() as connection:
            self._begin(connection)
            self._expire_stale(connection, now)
            row = connection.execute(
                "SELECT * FROM leases WHERE o_id=? AND io_id=?",
                (str(o_id), str(io_id)),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LeaseConflict("lease_not_found")
            if not secrets.compare_digest(
                str(row["claim_token"]), str(claim_token)
            ):
                connection.rollback()
                raise LeaseConflict("lease_token_mismatch")
            if str(row["workstation_id"]) != str(workstation_id):
                connection.rollback()
                raise LeaseConflict("lease_owner_mismatch")
            if str(row["state"]) not in {ACTIVE, EXPIRED}:
                connection.rollback()
                raise LeaseConflict(f"lease_not_renewable:{row['state']}")
            if self._other_active_for_order(
                connection, str(o_id), str(io_id)
            ) is not None:
                connection.rollback()
                raise LeaseConflict("order_has_other_active_lease")
            if self._workstation_at_capacity(
                connection, workstation_id, str(o_id), str(io_id)
            ):
                connection.rollback()
                raise LeaseConflict("workstation_batch_exceeds_limit")
            # An expired owner may reclaim only while the same token is still
            # present.  BEGIN IMMEDIATE serializes this with a competing claim;
            # once another workstation takes over, its new token makes this 409.
            connection.execute(
                """UPDATE leases SET state='ACTIVE', expires_at=?, updated_at=?
                   WHERE o_id=? AND io_id=? AND claim_token=?""",
                (expires, now, str(o_id), str(io_id), str(claim_token)),
            )
            row = connection.execute(
                "SELECT * FROM leases WHERE o_id=? AND io_id=?",
                (str(o_id), str(io_id)),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._lease_from_row(row)

    def renew_many(
        self,
        credentials: list[tuple[str, str, str, str]],
    ) -> list[Lease]:
        """Atomically validate and renew one bounded workstation batch.

        No expiry is extended until every exact pair, owner and token has been
        validated.  A conflict on any member rolls back the whole batch so a
        browser batch can never proceed with a partially renewed lease set.
        """

        if not 1 <= len(credentials) <= MAX_ACTIVE_PER_WORKSTATION:
            raise ValueError("renew_many requires between 1 and 10 credentials")
        normalized = [tuple(map(str, credential)) for credential in credentials]
        pairs = [(o_id, io_id) for o_id, io_id, _workstation, _token in normalized]
        if len(set(pairs)) != len(pairs):
            raise ValueError("renew_many pair identities must be unique")
        if len({o_id for o_id, _io_id in pairs}) != len(pairs):
            raise ValueError("renew_many internal order identities must be unique")

        now = float(self.clock())
        expires = now + self.ttl_seconds
        renewed_rows: list[sqlite3.Row] = []
        with self._connect() as connection:
            self._begin(connection)
            self._expire_stale(connection, now)
            try:
                # Complete the validation pass before changing any expiry.
                for o_id, io_id, workstation_id, claim_token in normalized:
                    row = connection.execute(
                        "SELECT * FROM leases WHERE o_id=? AND io_id=?",
                        (o_id, io_id),
                    ).fetchone()
                    if row is None:
                        raise LeaseConflict("lease_not_found")
                    if not secrets.compare_digest(
                        str(row["claim_token"]), claim_token
                    ):
                        raise LeaseConflict("lease_token_mismatch")
                    if str(row["workstation_id"]) != workstation_id:
                        raise LeaseConflict("lease_owner_mismatch")
                    if str(row["state"]) not in {ACTIVE, EXPIRED}:
                        raise LeaseConflict(f"lease_not_renewable:{row['state']}")
                    if self._other_active_for_order(
                        connection, o_id, io_id
                    ) is not None:
                        raise LeaseConflict("order_has_other_active_lease")

                # Expired leases retain their original token until another
                # workstation claims the pair.  Reclaiming such a token must
                # still respect the workstation's bounded active batch: a
                # caller may already own newer ACTIVE pairs that are not part
                # of this renewal request.  Compute the complete post-renewal
                # set while BEGIN IMMEDIATE still serializes every claimant.
                requested_by_workstation: dict[str, set[tuple[str, str]]] = {}
                for o_id, io_id, workstation_id, _claim_token in normalized:
                    requested_by_workstation.setdefault(workstation_id, set()).add(
                        (o_id, io_id)
                    )
                for workstation_id, requested_pairs in requested_by_workstation.items():
                    active_rows = connection.execute(
                        """SELECT o_id, io_id FROM leases
                           WHERE workstation_id=? AND state='ACTIVE'""",
                        (workstation_id,),
                    ).fetchall()
                    final_pairs = {
                        (str(row["o_id"]), str(row["io_id"]))
                        for row in active_rows
                    } | requested_pairs
                    if len(final_pairs) > MAX_ACTIVE_PER_WORKSTATION:
                        raise LeaseConflict("workstation_batch_exceeds_limit")

                for o_id, io_id, _workstation_id, claim_token in normalized:
                    connection.execute(
                        """UPDATE leases SET state='ACTIVE', expires_at=?, updated_at=?
                           WHERE o_id=? AND io_id=? AND claim_token=?""",
                        (expires, now, o_id, io_id, claim_token),
                    )
                    row = connection.execute(
                        "SELECT * FROM leases WHERE o_id=? AND io_id=?",
                        (o_id, io_id),
                    ).fetchone()
                    assert row is not None
                    renewed_rows.append(row)
            except LeaseConflict:
                connection.rollback()
                raise
            connection.commit()
        return [self._lease_from_row(row) for row in renewed_rows]

    def transition(
        self,
        target_state: str,
        o_id: str,
        io_id: str,
        workstation_id: str,
        claim_token: str,
        *,
        completion_reason: str = "",
    ) -> Lease:
        if target_state not in {RELEASED, COMPLETED}:
            raise ValueError("unsupported lease transition")
        normalized_reason = str(completion_reason)
        if target_state == COMPLETED and normalized_reason not in COMPLETION_REASONS:
            raise ValueError("invalid completion reason")
        if target_state == RELEASED and normalized_reason:
            raise ValueError("release cannot carry a completion reason")
        now = float(self.clock())
        with self._connect() as connection:
            self._begin(connection)
            self._expire_stale(connection, now)
            existing = connection.execute(
                "SELECT * FROM leases WHERE o_id=? AND io_id=?",
                (str(o_id), str(io_id)),
            ).fetchone()
            if existing is None:
                connection.rollback()
                raise LeaseConflict("lease_not_found")
            if not secrets.compare_digest(
                str(existing["claim_token"]), str(claim_token)
            ):
                connection.rollback()
                raise LeaseConflict("lease_token_mismatch")
            if str(existing["workstation_id"]) != str(workstation_id):
                connection.rollback()
                raise LeaseConflict("lease_owner_mismatch")
            if str(existing["state"]) == target_state:
                # Network retries after a committed release/complete are safe.
                if target_state == COMPLETED and str(
                    existing["completion_reason"] or ""
                ) != normalized_reason:
                    connection.rollback()
                    raise LeaseConflict("completion_reason_mismatch")
                connection.commit()
                return self._lease_from_row(existing)
            try:
                self._owned_active(
                    connection, o_id, io_id, workstation_id, claim_token, now
                )
            except LeaseConflict:
                connection.commit()
                raise
            connection.execute(
                """UPDATE leases
                   SET state=?, updated_at=?, expires_at=?, completed_at=?,
                       completion_reason=?
                   WHERE o_id=? AND io_id=? AND claim_token=?""",
                (
                    target_state,
                    now,
                    now,
                    now if target_state == COMPLETED else None,
                    normalized_reason,
                    str(o_id),
                    str(io_id),
                    str(claim_token),
                ),
            )
            row = connection.execute(
                "SELECT * FROM leases WHERE o_id=? AND io_id=?",
                (str(o_id), str(io_id)),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._lease_from_row(row)

    def release(
        self,
        o_id: str,
        io_id: str,
        workstation_id: str,
        claim_token: str,
    ) -> Lease:
        return self.transition(
            RELEASED, o_id, io_id, workstation_id, claim_token
        )

    def completed_for_retry(
        self,
        o_id: str,
        io_id: str,
        workstation_id: str,
        claim_token: str,
        completion_reason: str,
    ) -> Optional[Lease]:
        """Return an already committed identical completion retry.

        This is checked before an automatic completion performs a new live
        readback, because a committed lease is intentionally no longer ACTIVE.
        A different reason can never rewrite the persistent audit decision.
        """

        normalized_reason = str(completion_reason)
        if normalized_reason not in COMPLETION_REASONS:
            raise ValueError("invalid completion reason")
        now = float(self.clock())
        with self._connect() as connection:
            self._begin(connection)
            self._expire_stale(connection, now)
            row = connection.execute(
                "SELECT * FROM leases WHERE o_id=? AND io_id=?",
                (str(o_id), str(io_id)),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LeaseConflict("lease_not_found")
            if not secrets.compare_digest(
                str(row["claim_token"]), str(claim_token)
            ):
                connection.rollback()
                raise LeaseConflict("lease_token_mismatch")
            if str(row["workstation_id"]) != str(workstation_id):
                connection.rollback()
                raise LeaseConflict("lease_owner_mismatch")
            if str(row["state"]) != COMPLETED:
                connection.commit()
                return None
            if str(row["completion_reason"] or "") != normalized_reason:
                connection.rollback()
                raise LeaseConflict("completion_reason_mismatch")
            connection.commit()
        return self._lease_from_row(row)

    def complete(
        self,
        o_id: str,
        io_id: str,
        workstation_id: str,
        claim_token: str,
        completion_reason: str,
    ) -> Lease:
        return self.transition(
            COMPLETED,
            o_id,
            io_id,
            workstation_id,
            claim_token,
            completion_reason=completion_reason,
        )


def migrate_event_store(store: LeaseStore, event_path: Path, workstation_id: str) -> None:
    """Import this PC's old decisions and active tokens without replaying actions.

    The event DB remains unchanged. Imported PREPARING/RUNNING states stay in
    the event DB and the engine must recover them through current JST readback.
    Server-only decisions are not present here and require a separate export.
    """
    import json
    import re

    event_path = Path(event_path)
    if not event_path.exists():
        return
    with store._connect() as target:
        target.execute('CREATE TABLE IF NOT EXISTS local_migrations (name TEXT PRIMARY KEY)')
        if target.execute('SELECT 1 FROM local_migrations WHERE name=?', ('event-store-v1',)).fetchone():
            return
    with sqlite3.connect(event_path.as_uri() + '?mode=ro', uri=True) as source:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        exclusions = source.execute('SELECT o_id, io_id, reason FROM excluded_jobs').fetchall() if 'excluded_jobs' in tables else []
        jobs = source.execute('SELECT o_id, io_id, status, plan_json FROM jobs').fetchall() if 'jobs' in tables else []
    active = {'PENDING', 'PAUSED', 'PREPARING', 'RUNNING'}
    retryable = {'RELEASED_EXTERNAL_ORDER', 'RELEASED_PROFILE_MISMATCH',
                 'RELEASED_STATUS_CHANGED', 'RELEASED_LEASE_LOST'}
    for o_id, io_id, status, _plan in jobs:
        if status not in active | retryable:
            exclusions.append((o_id, io_id, '迁移本机终态记录：' + status))
    for o_id, io_id, reason in exclusions:
        if re.fullmatch(r'[0-9]{1,20}', str(o_id)) and re.fullmatch(r'[0-9]{1,20}', str(io_id)):
            store.force_skip(str(o_id), str(io_id), workstation_id, reason or '迁移本机永久排除')
    now = store.clock()
    with store._connect() as target:
        target.execute('BEGIN IMMEDIATE')
        for o_id, io_id, status, raw in jobs:
            if status not in active:
                continue
            plan = json.loads(raw)
            token = plan.get('claim_token')
            if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token)
                    or str(plan.get('o_id')) != str(o_id) or str(plan.get('io_id')) != str(io_id)):
                raise RuntimeError('旧任务身份或令牌无效，迁移已停止；请先核对旧任务')
            if target.execute('SELECT 1 FROM operator_skips WHERE o_id=? AND io_id=?', (o_id, io_id)).fetchone():
                continue
            target.execute('''INSERT OR IGNORE INTO leases
                (o_id, io_id, workstation_id, claim_token, state, claimed_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)''',
                (o_id, io_id, workstation_id, token, now, now, now + store.ttl_seconds))
        target.execute('INSERT OR IGNORE INTO local_migrations(name) VALUES (?)', ('event-store-v1',))
        target.commit()

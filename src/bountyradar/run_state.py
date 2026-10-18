"""Durable scan findings, completed-scan checkpoint and cross-process run lock."""

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bountyradar.models import BountyCandidate, SearchProgress
from bountyradar.verification_models import VerificationResult


class RunAlreadyActive(Exception):
    """Another process owns the project run lock."""


@contextmanager
def run_lock(database_path: Path):
    """Prevent overlapping scans, including scans started outside systemd."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database_path.with_name(database_path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunAlreadyActive("another scan is already running") from exc
        yield
    finally:
        os.close(descriptor)


class RunState:
    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database_path, timeout=10)
        os.chmod(database_path, 0o600)
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS scan_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovered_issues (
                source_issue_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_results (
                source_issue_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                checked_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS issues_updated ON discovered_issues(
                json_extract(payload_json, '$.updated_at') DESC);
            CREATE INDEX IF NOT EXISTS verification_checked ON verification_results(checked_at);
        """)

    def close(self) -> None:
        self.db.close()

    def last_complete_at(self, source: str = "github") -> datetime | None:
        key = f"last_complete_at:{source}"
        row = self.db.execute(
            "SELECT value FROM scan_state WHERE key=?", (key,)
        ).fetchone()
        if row is None and source == "github":
            row = self.db.execute(
                "SELECT value FROM scan_state WHERE key='last_complete_at'"
            ).fetchone()
        if not row:
            return None
        try:
            value = datetime.fromisoformat(row[0])
            return value.astimezone(UTC) if value.tzinfo else None
        except (ValueError, TypeError):
            return None

    def tracked_eligible_candidates(self, limit: int) -> list[BountyCandidate]:
        """Recheck a bounded set of previously eligible issues even if search omits them."""
        table = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_issue_state'"
        ).fetchone()
        if table is None:
            return []
        rows = self.db.execute(
            """SELECT v.payload_json FROM verification_results AS v
            JOIN notification_issue_state AS n USING (source_issue_id)
            WHERE n.eligible=1 ORDER BY v.checked_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [VerificationResult.model_validate_json(row[0]).candidate for row in rows]

    def review_candidates(self, limit: int) -> list[BountyCandidate]:
        """Rotate through unverified prior candidates that may gain external funding."""
        rows = self.db.execute(
            """SELECT payload_json FROM verification_results
            WHERE json_extract(payload_json, '$.category') IN ('REWARD_CLAIMED','UNCERTAIN')
            ORDER BY checked_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [VerificationResult.model_validate_json(row[0]).candidate for row in rows]

    def verification_queue(
        self, limit: int, *, now: datetime | None = None, refresh_minutes: float = 60,
    ) -> list[BountyCandidate]:
        """Reserve 25% for oldest work; prioritize recent monetary changes otherwise.

        Recently discovered catch-up candidates qualify even with old issue dates.
        Unchanged complete results are revisited hourly, incomplete results after 5m.
        A one-slot budget serves historical work every fourth run, across restarts.
        """
        if limit <= 0:
            return []
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(hours=8)).isoformat()
        retry = (now - timedelta(minutes=5)).isoformat()
        refresh = (now - timedelta(minutes=refresh_minutes)).isoformat()
        arrived = (now - timedelta(minutes=10)).isoformat()
        due = """(v.source_issue_id IS NULL OR
            julianday(json_extract(d.payload_json, '$.updated_at')) > julianday(v.checked_at)
            OR julianday(v.checked_at) <= julianday(?) OR
            (json_extract(v.payload_json, '$.availability')='UNKNOWN'
                AND julianday(v.checked_at) <= julianday(?)))"""
        base = """SELECT d.source_issue_id,d.payload_json FROM discovered_issues d
                  LEFT JOIN verification_results v USING (source_issue_id) WHERE """ + due
        row = self.db.execute("SELECT value FROM scan_state WHERE key='queue_round'").fetchone()
        round_number = int(row[0]) if row else 0
        reserve = max(1, limit // 4) if limit > 1 else int(round_number % 4 == 3)
        recent = self.db.execute(
            base + """ AND (julianday(d.seen_at) >= julianday(?) OR
                julianday(json_extract(d.payload_json, '$.updated_at')) >= julianday(?))
                ORDER BY CASE WHEN v.source_issue_id IS NULL AND
                    julianday(d.seen_at) >= julianday(?) THEN 0 ELSE 1 END,
                json_extract(d.payload_json, '$.updated_at') DESC,
                CASE WHEN CAST(json_extract(d.payload_json,
                    '$.potential_reward_amount') AS REAL)>0 THEN 0 ELSE 1 END,
                v.checked_at, d.source_issue_id LIMIT ?""",
            (refresh, retry, cutoff, cutoff, arrived, limit - reserve),
        ).fetchall()
        ids = [row[0] for row in recent]
        placeholders = ','.join('?' for _ in ids) or "''"
        older = self.db.execute(
            base + f" AND d.source_issue_id NOT IN ({placeholders})" + """
                ORDER BY v.checked_at, d.seen_at, d.source_issue_id LIMIT ?""",
            (refresh, retry, *ids, limit - len(recent)),
        ).fetchall()
        with self.db:
            self.db.execute("""INSERT INTO scan_state(key,value) VALUES ('queue_round',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(round_number+1),))
        self.queue_recent_count = len(recent)
        return [BountyCandidate.model_validate_json(row[1]) for row in recent + older]

    def search_progress(self, source: str = "github") -> SearchProgress | None:
        row = self.db.execute(
            "SELECT value FROM scan_state WHERE key=?", (f"{source}_search_progress",)
        ).fetchone()
        return SearchProgress.model_validate_json(row[0]) if row else None

    def github_cooldown(self) -> float:
        row = self.db.execute(
            "SELECT value FROM scan_state WHERE key='github_cooldown'"
        ).fetchone()
        return float(row[0]) if row else 0.0

    def defer_github(self, until: float) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO scan_state(key,value) VALUES ('github_cooldown',?)
                ON CONFLICT(key) DO UPDATE SET value=CAST(
                    max(CAST(value AS REAL), CAST(excluded.value AS REAL)) AS TEXT)""",
                (str(until),),
            )

    def save(
        self,
        candidates: list[BountyCandidate],
        results: list[VerificationResult],
        *,
        completed_at: datetime | None,
        source: str = "github",
        progress: SearchProgress | None = None,
    ) -> None:
        """Save findings atomically; advance the checkpoint only for a complete scan."""
        with self.db:
            self.db.executemany(
                """INSERT INTO discovered_issues(source_issue_id,payload_json,seen_at)
                VALUES (?,?,?) ON CONFLICT(source_issue_id) DO UPDATE SET
                payload_json=excluded.payload_json
                WHERE julianday(json_extract(excluded.payload_json, '$.updated_at')) >=
                      julianday(json_extract(discovered_issues.payload_json, '$.updated_at'))""",
                [
                    (
                        f"{item.source}:{item.source_issue_id}",
                        item.model_dump_json(),
                        item.discovered_at.isoformat(),
                    )
                    for item in candidates
                ],
            )
            self.db.executemany(
                """INSERT INTO verification_results(source_issue_id,payload_json,checked_at)
                VALUES (?,?,?) ON CONFLICT(source_issue_id) DO UPDATE SET
                payload_json=excluded.payload_json,checked_at=excluded.checked_at""",
                [
                    (
                        f"{item.candidate.source}:{item.candidate.source_issue_id}",
                        item.model_dump_json(),
                        item.checked_at.isoformat(),
                    )
                    for item in results
                ],
            )
            if progress is not None:
                self.db.execute(
                    """INSERT INTO scan_state(key,value) VALUES (?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (f"{source}_search_progress", progress.model_dump_json()),
                )
            if completed_at is not None:
                self.db.execute(
                    """INSERT INTO scan_state(key,value) VALUES (?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (f"last_complete_at:{source}", completed_at.astimezone(UTC).isoformat()),
                )
                self.db.execute(
                    "DELETE FROM scan_state WHERE key=?", (f"{source}_search_progress",)
                )
                if source == "github":
                    self.db.execute(
                        """INSERT INTO scan_state(key,value) VALUES ('last_complete_at',?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                        (completed_at.astimezone(UTC).isoformat(),),
                    )


class SearchLane:
    """Isolated durable cursor; issue records and API cooldown remain shared."""

    def __init__(self, state: RunState, source: str):
        self.state, self.source = state, source

    def search_progress(self):
        return self.state.search_progress(self.source)

    def save(self, candidates, results, *, completed_at, progress=None):
        self.state.save(candidates, results, completed_at=completed_at,
                        progress=progress, source=self.source)

    def github_cooldown(self):
        return self.state.github_cooldown()

    def defer_github(self, until):
        self.state.defer_github(until)

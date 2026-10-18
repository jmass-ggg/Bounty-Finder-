"""SQLite-backed qualification transitions and conservative delivery outbox."""

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bountyradar.notification import eligible_for_delivery
from bountyradar.settings import Settings
from bountyradar.verification_models import Availability, VerificationResult


@dataclass(frozen=True)
class OutboxEntry:
    event_key: str
    reason: str
    result: VerificationResult
    state: str
    attempts: int
    next_attempt_at: datetime


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class NotificationOutbox:
    """One transaction per transition or delivery state change."""

    def __init__(
        self,
        path: Path,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.settings = settings
        self.clock = clock
        self._schema()

    def close(self) -> None:
        self.db.close()

    def _schema(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS notification_issue_state (
                source_issue_id TEXT PRIMARY KEY,
                eligible INTEGER NOT NULL,
                amount TEXT,
                currency TEXT,
                category TEXT NOT NULL,
                availability TEXT NOT NULL,
                generation INTEGER NOT NULL,
                alert_amount TEXT,
                alert_currency TEXT,
                checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_outbox (
                event_key TEXT PRIMARY KEY,
                source_issue_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN
                    ('pending','sending','sent','retryable_failure','permanent_failure','unknown_delivery','cancelled')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                lease_until TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS outbox_due ON notification_outbox(state,next_attempt_at);
            CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                outcome TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS delivery_attempt_day
                ON notification_delivery_attempts(attempted_at);
            CREATE TABLE IF NOT EXISTS notification_policy (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        if self.settings.notification_mode == "include_unverified":
            # A fixed activation boundary, never a rolling age cutoff. Downtime
            # after activation remains eligible however long the laptop was off.
            boundary = self.clock() - timedelta(
                hours=self.settings.notification_initial_lookback_hours,
            )
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO notification_policy(key,value) VALUES (?,?)",
                    ("unverified_since", _iso(boundary)),
                )

    def _new_reward_in_monitoring_period(self, result: VerificationResult) -> bool:
        row = self.db.execute(
            "SELECT value FROM notification_policy WHERE key='unverified_since'"
        ).fetchone()
        if row is None or result.notification_classification == "CONFIRMED_FUNDED":
            return True
        boundary = _date(row[0])
        return result.candidate.created_at >= boundary or any(
            item.kind == "trusted_reward_announcement" and item.observed_at >= boundary
            for item in result.evidence
        )

    def enqueue(self, result: VerificationResult) -> str | None:
        """Queue only meaningful transitions, without using issue update timestamps as keys."""
        availability_missing = [source for source in result.missing_sources
                                if source != "authoritative funding provider not configured"]
        if availability_missing and result.availability not in {
            Availability.CLOSED,
            Availability.UNAVAILABLE,
            Availability.ASSIGNED,
            Availability.CONTESTED,
        }:
            return None
        issue_id = f"{result.candidate.source}:{result.candidate.source_issue_id}"
        now = self.clock().astimezone(UTC)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT * FROM notification_issue_state WHERE source_issue_id=?", (issue_id,)
            ).fetchone()
            if previous and _date(previous["checked_at"]) > result.checked_at:
                return None
            generation = previous["generation"] if previous else 0
            baseline = previous["alert_amount"] if previous else None
            baseline_currency = previous["alert_currency"] if previous else None
            eligible = eligible_for_delivery(result, self.settings, now)
            # Already queued work survives activation-policy changes. An old
            # issue update alone is not evidence that a new reward was offered.
            if eligible and (not previous or previous["alert_amount"] is None):
                eligible = self._new_reward_in_monitoring_period(result)
            reason = None
            if eligible:
                if previous is None:
                    reason = "new_qualified_bounty"
                elif (previous["alert_amount"] is None
                      and result.category.value != "FUNDED_EVIDENCE"):
                    reason = "new_qualified_bounty"
                elif not previous["eligible"]:
                    if previous["availability"] != "AVAILABLE":
                        reason = "available_again"
                    elif (previous["category"] != "FUNDED_EVIDENCE"
                          and result.category.value == "FUNDED_EVIDENCE"):
                        reason = "funding_verified"
                    elif previous["amount"] is None:
                        reason = "new_reward"
                    elif result.amount is not None:
                        threshold = (
                            self.settings.min_reward_usd
                            if result.currency == "USD"
                            else self.settings.currency_minimums.get(result.currency or "")
                        )
                        if (
                            threshold is not None
                            and previous["amount"] is not None
                            and Decimal(previous["amount"]) < threshold <= result.amount
                        ):
                            reason = "reward_became_eligible"
                if reason is None and previous is not None and baseline is None:
                    reason = "new_qualified_bounty"
                if reason is None and previous is not None:
                    if (previous["category"] != "FUNDED_EVIDENCE"
                            and result.category.value == "FUNDED_EVIDENCE"):
                        reason = "funding_verified"
                    elif baseline_currency is not None and baseline_currency != result.currency:
                        reason = "reward_currency_changed"
                    elif baseline is not None and result.amount is not None:
                        original = Decimal(baseline)
                        increase = (
                            (result.amount - original) / original * 100
                            if original > 0
                            else Decimal(0)
                        )
                        if increase > 0 and increase >= self.settings.reward_increase_min_percent:
                            reason = "reward_increased"
            if reason:
                generation += 1
                baseline = str(result.amount) if result.amount is not None else None
                baseline_currency = result.currency
                event_key = f"{issue_id}:{generation}"
                self.db.execute(
                    """INSERT OR IGNORE INTO notification_outbox
                    (event_key,source_issue_id,reason,payload_json,state,next_attempt_at,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        event_key,
                        issue_id,
                        reason,
                        result.model_dump_json(),
                        "pending",
                        _iso(now),
                        _iso(now),
                        _iso(now),
                    ),
                )
                self.db.execute(
                    """UPDATE notification_outbox
                    SET state='cancelled',last_error='superseded by newer qualification',
                        updated_at=?
                    WHERE source_issue_id=? AND event_key<>?
                      AND state IN ('pending','retryable_failure')""",
                    (_iso(now), issue_id, event_key),
                )
            else:
                event_key = None
            if eligible:
                # Refresh unsent observations without changing their event identity or retries.
                self.db.execute(
                    """UPDATE notification_outbox SET payload_json=?,updated_at=?
                    WHERE source_issue_id=? AND state IN ('pending','retryable_failure')""",
                    (result.model_dump_json(), _iso(now), issue_id),
                )
            else:
                self.db.execute(
                    """UPDATE notification_outbox SET state='cancelled',
                    last_error='latest verification no longer qualifies',updated_at=?
                    WHERE source_issue_id=? AND state IN ('pending','retryable_failure')""",
                    (_iso(now), issue_id),
                )
            self.db.execute(
                """INSERT INTO notification_issue_state
                (source_issue_id,eligible,amount,currency,category,availability,generation,
                 alert_amount,alert_currency,checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_issue_id) DO UPDATE SET
                 eligible=excluded.eligible,amount=excluded.amount,currency=excluded.currency,
                 category=excluded.category,availability=excluded.availability,
                 generation=excluded.generation,alert_amount=excluded.alert_amount,
                 alert_currency=excluded.alert_currency,checked_at=excluded.checked_at""",
                (
                    issue_id,
                    int(eligible),
                    str(result.amount) if result.amount is not None else None,
                    result.currency,
                    result.category.value,
                    result.availability.value,
                    generation,
                    baseline,
                    baseline_currency,
                    _iso(result.checked_at),
                ),
            )
            return event_key

    def recover_expired(self) -> int:
        """Do not automatically retry a send interrupted after claiming the event."""
        now = _iso(self.clock())
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT event_key FROM notification_outbox "
                "WHERE state='sending' AND lease_until<=?",
                (now,),
            ).fetchall()
            for row in rows:
                self.db.execute(
                    "UPDATE notification_outbox SET state='unknown_delivery',updated_at=?,"
                    "last_error='process stopped during send' WHERE event_key=?",
                    (now, row["event_key"]),
                )
                self.db.execute(
                    "UPDATE notification_delivery_attempts SET outcome='unknown' WHERE id=("
                    "SELECT id FROM notification_delivery_attempts WHERE event_key=? "
                    "ORDER BY id DESC LIMIT 1)",
                    (row["event_key"],),
                )
            return len(rows)

    def list_entries(
        self, *, due_only: bool = False, limit: int | None = None,
    ) -> list[OutboxEntry]:
        now = _iso(self.clock())
        where = (
            "WHERE state IN ('pending','retryable_failure') AND next_attempt_at<=?"
            if due_only
            else "WHERE state!='sent'"
        )
        params = (now,) if due_only else ()
        query = (f"SELECT * FROM notification_outbox {where} "
                 "ORDER BY next_attempt_at,created_at,event_key")
        if limit is not None:
            query += " LIMIT ?"
            params = (*params, limit)
        rows = self.db.execute(query, params).fetchall()
        return [
            OutboxEntry(
                row["event_key"],
                row["reason"],
                VerificationResult.model_validate_json(row["payload_json"]),
                row["state"],
                row["attempts"],
                _date(row["next_attempt_at"]),
            )
            for row in rows
        ]

    def _attempts_today(self, now: datetime) -> int:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return self.db.execute(
            "SELECT count(*) FROM notification_delivery_attempts "
            "WHERE attempted_at>=? AND attempted_at<?",
            (_iso(start), _iso(end)),
        ).fetchone()[0]

    def daily_cap_reached(self) -> bool:
        return self._attempts_today(self.clock().astimezone(UTC)) >= self.settings.email_daily_cap

    def defer(self, event_key: str) -> None:
        """Rotate unavailable evidence without spending an SMTP attempt."""
        with self.db:
            self.db.execute(
                "UPDATE notification_outbox SET next_attempt_at=? WHERE event_key=? "
                "AND state IN ('pending','retryable_failure')",
                (_iso(self.clock() + timedelta(seconds=self.settings.scan_interval_seconds)),
                 event_key),
            )

    def claim(self, event_key: str) -> bool:
        now = self.clock().astimezone(UTC)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self._attempts_today(now) >= self.settings.email_daily_cap:
                return False
            row = self.db.execute(
                "SELECT * FROM notification_outbox WHERE event_key=?", (event_key,)
            ).fetchone()
            if (
                not row
                or row["state"] not in ("pending", "retryable_failure")
                or _date(row["next_attempt_at"]) > now
            ):
                return False
            if row["attempts"] >= self.settings.email_max_attempts:
                self.db.execute(
                    "UPDATE notification_outbox SET state='permanent_failure' WHERE event_key=?",
                    (event_key,),
                )
                return False
            lease = now + timedelta(seconds=self.settings.email_sending_lease_seconds)
            self.db.execute(
                "UPDATE notification_outbox SET state='sending',attempts=attempts+1,"
                "lease_until=?,updated_at=? WHERE event_key=?",
                (_iso(lease), _iso(now), event_key),
            )
            self.db.execute(
                "INSERT INTO notification_delivery_attempts"
                "(event_key,attempted_at,outcome) VALUES (?,?,?)",
                (event_key, _iso(now), "sending"),
            )
            return True

    def finish(self, event_key: str, outcome: str) -> None:
        now = self.clock().astimezone(UTC)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT attempts,state FROM notification_outbox WHERE event_key=?", (event_key,)
            ).fetchone()
            if not row or row["state"] != "sending":
                raise ValueError("outbox event is not in sending state")
            if outcome == "sent":
                state, delay = "sent", 0
            elif outcome == "unknown":
                state, delay = "unknown_delivery", 0
            elif outcome == "permanent":
                state, delay = "permanent_failure", 0
            elif outcome == "retryable":
                state = (
                    "retryable_failure"
                    if row["attempts"] < self.settings.email_max_attempts
                    else "permanent_failure"
                )
                delay = self.settings.email_retry_base_seconds * 2 ** (row["attempts"] - 1)
            else:
                raise ValueError("unknown delivery outcome")
            self.db.execute(
                "UPDATE notification_outbox SET state=?,next_attempt_at=?,updated_at=?,sent_at=?,"
                "lease_until=NULL,last_error=? WHERE event_key=?",
                (
                    state,
                    _iso(now + timedelta(seconds=delay)),
                    _iso(now),
                    _iso(now) if state == "sent" else None,
                    None if state == "sent" else outcome,
                    event_key,
                ),
            )
            self.db.execute(
                "UPDATE notification_delivery_attempts SET outcome=? WHERE id=("
                "SELECT id FROM notification_delivery_attempts "
                "WHERE event_key=? ORDER BY id DESC LIMIT 1)",
                (outcome, event_key),
            )

    def cancel(self, event_key: str, reason: str) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE notification_outbox SET state='cancelled',last_error=? WHERE event_key=? "
                "AND state IN ('pending','retryable_failure')",
                (reason, event_key),
            )
            self.db.execute(
                "UPDATE notification_issue_state SET eligible=0 WHERE source_issue_id=("
                "SELECT source_issue_id FROM notification_outbox WHERE event_key=?)",
                (event_key,),
            )

    def claim_test(self) -> int | None:
        """Reserve one daily email attempt for an explicitly invoked test message."""
        now = self.clock().astimezone(UTC)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self._attempts_today(now) >= self.settings.email_daily_cap:
                return None
            cursor = self.db.execute(
                "INSERT INTO notification_delivery_attempts(event_key,attempted_at,outcome) "
                "VALUES (?,?,?)",
                ("manual-test", _iso(now), "sending"),
            )
            return cursor.lastrowid

    def finish_test(self, attempt_id: int, outcome: str) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE notification_delivery_attempts SET outcome=? WHERE id=?",
                (outcome, attempt_id),
            )

    def resolve_unknown(self, event_key: str, *, delivered: bool) -> bool:
        """Manual resolution after checking the recipient inbox or SMTP records."""
        now = _iso(self.clock())
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = "sent" if delivered else "retryable_failure"
            changed = self.db.execute(
                "UPDATE notification_outbox SET state=?,next_attempt_at=?,updated_at=?,"
                "sent_at=?,lease_until=NULL,last_error=? "
                "WHERE event_key=? AND state='unknown_delivery'",
                (
                    state,
                    now,
                    now,
                    now if delivered else None,
                    None if delivered else "manually confirmed not delivered",
                    event_key,
                ),
            ).rowcount
            return bool(changed)

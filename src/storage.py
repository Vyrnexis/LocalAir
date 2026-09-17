"""SQLite persistence for LocalAIR web/history data."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import threading

from .device import DeviceReading


class LocalAirStore:
    """Small thread-safe SQLite wrapper used by the web service."""

    def __init__(self, path: Path, retention_days: int) -> None:
        self.path = path
        self.retention_days = retention_days
        self.lock = threading.Lock()
        self.last_cleanup: datetime | None = None
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._connect().close()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _migrate(self) -> None:
        with self.lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sensor_readings (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT NOT NULL,
                  temperature_c REAL,
                  humidity_percent INTEGER,
                  status TEXT NOT NULL,
                  error_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sensor_readings_timestamp
                  ON sensor_readings(timestamp);

                CREATE TABLE IF NOT EXISTS ir_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT NOT NULL,
                  source TEXT NOT NULL,
                  command TEXT,
                  nec_hex TEXT,
                  known INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ir_events_timestamp
                  ON ir_events(timestamp);

                CREATE TABLE IF NOT EXISTS ac_state_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT NOT NULL,
                  source TEXT NOT NULL,
                  field TEXT NOT NULL,
                  old_value TEXT,
                  new_value TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_ac_state_events_timestamp
                  ON ac_state_events(timestamp);
                """
            )

    def add_reading(self, reading: DeviceReading) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO sensor_readings (
                  timestamp, temperature_c, humidity_percent, status,
                  error_message
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    reading.timestamp.isoformat(timespec="seconds"),
                    reading.temperature_c,
                    reading.humidity_percent,
                    reading.connection_status.value,
                    reading.error_message,
                ),
            )
        self.cleanup_if_due()

    def add_ir_event(
        self,
        source: str,
        command: str | None,
        nec_hex: str | None = None,
        known: bool = True,
    ) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO ir_events (
                  timestamp, source, command, nec_hex, known
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    source,
                    command,
                    nec_hex,
                    1 if known else 0,
                ),
            )

    def add_state_events(
        self,
        source: str,
        old_state: dict[str, str],
        new_state: dict[str, str],
    ) -> None:
        rows = []
        for key in sorted(set(old_state) | set(new_state)):
            old = old_state.get(key)
            new = new_state.get(key)
            if old != new:
                rows.append((key, old, new))
        if not rows:
            return

        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock, self._connect() as db:
            db.executemany(
                """
                INSERT INTO ac_state_events (
                  timestamp, source, field, old_value, new_value
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (timestamp, source, field, old, new)
                    for field, old, new in rows
                ],
            )

    def recent_ir_events(self, limit: int = 8) -> list[dict]:
        with self.lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT timestamp, source, command, nec_hex, known
                FROM ir_events
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_activity_events(self, limit: int = 10) -> list[dict]:
        """Return recent operator-facing activity for the dashboard."""
        with self.lock, self._connect() as db:
            ir_rows = db.execute(
                """
                SELECT timestamp, source, command, nec_hex, known
                FROM ir_events
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            error_rows = db.execute(
                """
                SELECT timestamp, status, error_message
                FROM sensor_readings
                WHERE status != 'Connected'
                   OR error_message IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        events = [
            {
                "kind": "ir",
                "timestamp": row["timestamp"],
                "source": row["source"],
                "command": row["command"],
                "nec_hex": row["nec_hex"],
                "known": row["known"],
            }
            for row in ir_rows
        ]
        events.extend(
            {
                "kind": "device",
                "timestamp": row["timestamp"],
                "source": "device",
                "status": row["status"],
                "error_message": row["error_message"],
            }
            for row in error_rows
        )
        return sorted(
            events,
            key=lambda event: datetime.fromisoformat(event["timestamp"]),
            reverse=True,
        )[:limit]

    def history(self, range_name: str) -> list[dict]:
        hours, bucket_minutes = {
            "1h": (1, 1),
            "24h": (24, 5),
            "7d": (24 * 7, 30),
            "30d": (24 * 30, 120),
            "31d": (24 * 31, 120),
        }.get(range_name, (24, 5))
        start = datetime.now().astimezone() - timedelta(hours=hours)
        start_str = start.isoformat(timespec="seconds")

        with self.lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT timestamp, temperature_c, humidity_percent
                FROM sensor_readings
                WHERE temperature_c IS NOT NULL
                  AND humidity_percent IS NOT NULL
                  AND datetime(timestamp) >= datetime(?)
                ORDER BY timestamp
                """,
                (start_str,),
            ).fetchall()

        buckets: dict[datetime, list[tuple[float, int]]] = {}
        for row in rows:
            timestamp = datetime.fromisoformat(row["timestamp"])
            timestamp = timestamp.astimezone()
            minute = timestamp.minute - (timestamp.minute % bucket_minutes)
            bucket = timestamp.replace(
                minute=minute, second=0, microsecond=0
            )
            buckets.setdefault(bucket, []).append(
                (float(row["temperature_c"]), int(row["humidity_percent"]))
            )

        return [
            {
                "timestamp": bucket.isoformat(timespec="seconds"),
                "temperature_c": round(
                    sum(temperature for temperature, _humidity in values)
                    / len(values),
                    1,
                ),
                "humidity_percent": round(
                    sum(humidity for _temperature, humidity in values)
                    / len(values)
                ),
            }
            for bucket, values in sorted(buckets.items())
        ]

    # Return successful AC commands within the selected chart window.
    def history_events(self, range_name: str) -> list[dict]:
        hours = {
            "1h": 1,
            "24h": 24,
            "7d": 24 * 7,
            "30d": 24 * 30,
            "31d": 24 * 31,
        }.get(range_name, 24)
        start = datetime.now().astimezone() - timedelta(hours=hours)
        with self.lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT timestamp, source, command
                FROM ir_events
                WHERE command IS NOT NULL
                  AND known = 1
                  AND datetime(timestamp) >= datetime(?)
                ORDER BY timestamp
                """,
                (start.isoformat(timespec="seconds"),),
            ).fetchall()
        return [dict(row) for row in rows]

    def cleanup_if_due(self) -> None:
        now = datetime.now().astimezone()
        if self.last_cleanup and now - self.last_cleanup < timedelta(hours=24):
            return
        cutoff = now - timedelta(days=self.retention_days)
        cutoff_str = cutoff.isoformat(timespec="seconds")
        with self.lock, self._connect() as db:
            db.execute(
                """
                DELETE FROM sensor_readings
                WHERE datetime(timestamp) < datetime(?)
                """,
                (cutoff_str,),
            )
            db.execute(
                """
                DELETE FROM ir_events
                WHERE datetime(timestamp) < datetime(?)
                """,
                (cutoff_str,),
            )
            db.execute(
                """
                DELETE FROM ac_state_events
                WHERE datetime(timestamp) < datetime(?)
                """,
                (cutoff_str,),
            )
        self.last_cleanup = now

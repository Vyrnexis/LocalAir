from __future__ import annotations

import stat
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.config import AppConfig, load_config, save_config
from src.device import ConnectionStatus, TuyaDevice, update_ac_state
from src.storage import LocalAirStore


class ConfigTests(unittest.TestCase):
    def test_round_trip_and_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            expected = AppConfig(
                local_key="not-a-real-key",
                ir_address=0x08E7,
                ir_commands={"power": 0x00},
                ac_state={"power": "on", "mode": "heat"},
            )

            save_config(expected, path)

            self.assertEqual(load_config(path), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_partial_config_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[device]\nlocal_key = "placeholder"\n',
                encoding="utf-8",
            )

            loaded = load_config(path)

            self.assertEqual(loaded.ip_address, "192.168.1.145")
            self.assertEqual(loaded.protocol_version, 3.5)
            self.assertEqual(loaded.poll_interval_seconds, 30)
            self.assertEqual(loaded.ir_address, 0x08E7)
            self.assertEqual(loaded.ir_commands, {})
            self.assertEqual(loaded.ac_state, {})


class DeviceTests(unittest.TestCase):
    def test_valid_sensor_response(self) -> None:
        reading = TuyaDevice._parse_status(
            {"dps": {"1": 202, "2": 49}},
            datetime.now().astimezone(),
        )

        self.assertEqual(reading.connection_status, ConnectionStatus.CONNECTED)
        self.assertEqual(reading.temperature_c, 20.2)
        self.assertEqual(reading.humidity_percent, 49)

    def test_missing_sensor_value_is_an_error(self) -> None:
        reading = TuyaDevice._parse_status(
            {"dps": {"1": 202}},
            datetime.now().astimezone(),
        )

        self.assertEqual(reading.connection_status, ConnectionStatus.ERROR)

    def test_tuya_error_message_is_reported(self) -> None:
        reading = TuyaDevice._parse_status(
            {"Error": "Network Error: Unable to Connect"},
            datetime.now().astimezone(),
        )

        self.assertEqual(reading.connection_status, ConnectionStatus.ERROR)
        self.assertIn("Unable to Connect", reading.error_message or "")

    def test_unknown_ir_command_is_rejected(self) -> None:
        result = TuyaDevice().send_ir(
            AppConfig(local_key="not-a-real-key"),
            "../../unexpected",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "Unknown IR command.")

    def test_missing_ir_code_has_clear_error(self) -> None:
        result = TuyaDevice().send_ir(
            AppConfig(local_key="not-a-real-key"),
            "power",
        )

        self.assertFalse(result.ok)
        self.assertIn("not been learned", result.message)

    def test_nec_frame_uses_wire_order_and_inverse_byte(self) -> None:
        self.assertEqual(TuyaDevice._nec_frame(0x08E7, 0x28), 0x08E728D7)

    def test_observed_commands_update_assumed_state(self) -> None:
        state = {
            "power": "on",
            "temperature": "22",
            "swing": "off",
        }

        state = update_ac_state(state, "power")
        state = update_ac_state(state, "mode_cool")
        state = update_ac_state(state, "fan_high")
        state = update_ac_state(state, "temp_up")
        state = update_ac_state(state, "swing")

        self.assertEqual(
            state,
            {
                "power": "off",
                "mode": "cool",
                "temperature": "23",
                "fan": "high",
                "swing": "on",
            },
        )

    def test_unknown_toggle_state_remains_unknown(self) -> None:
        self.assertEqual(update_ac_state({}, "power"), {})


class StorageTests(unittest.TestCase):
    def test_history_preserves_local_timezone_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "localair.db", 31)
            local_tz = timezone(timedelta(hours=8))
            timestamp = datetime.now(local_tz).replace(second=40, microsecond=0)
            store.add_reading(
                TuyaDevice._parse_status(
                    {"dps": {"1": 285, "2": 39}},
                    timestamp,
                )
            )

            points = store.history("1h")

            self.assertEqual(len(points), 1)
            expected = timestamp.astimezone().replace(second=0)
            self.assertTrue(
                points[0]["timestamp"].startswith(
                    expected.isoformat(timespec="seconds")
                )
            )
            self.assertIsNotNone(
                datetime.fromisoformat(points[0]["timestamp"]).tzinfo
            )

    def test_removed_six_hour_range_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "localair.db", 31)

            self.assertEqual(store.history("6h"), [])

    def test_history_handles_mixed_timezone_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "localair.db", 31)
            recent_utc = datetime.now(timezone.utc) - timedelta(minutes=30)
            with store._connect() as db:
                db.execute(
                    """
                    INSERT INTO sensor_readings (
                      timestamp, temperature_c, humidity_percent, status
                    ) VALUES (?, 21.5, 48, 'Connected')
                    """,
                    (recent_utc.isoformat(timespec="seconds"),),
                )

            points = store.history("1h")

            self.assertEqual(len(points), 1)

    def test_cleanup_prunes_all_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "localair.db", 1)
            two_days_ago = (
                datetime.now().astimezone() - timedelta(days=2)
            ).isoformat(timespec="seconds")
            with store._connect() as db:
                db.execute(
                    """
                    INSERT INTO sensor_readings (
                      timestamp, temperature_c, humidity_percent, status
                    ) VALUES (?, 20.0, 50, 'Connected')
                    """,
                    (two_days_ago,),
                )
                db.execute(
                    """
                    INSERT INTO ir_events (
                      timestamp, source, command, known
                    ) VALUES (?, 'localair', 'power', 1)
                    """,
                    (two_days_ago,),
                )
                db.execute(
                    """
                    INSERT INTO ac_state_events (
                      timestamp, source, field, old_value, new_value
                    ) VALUES (?, 'sync', 'power', 'off', 'on')
                    """,
                    (two_days_ago,),
                )

            store.cleanup_if_due()

            with store._connect() as db:
                tables = (
                    "sensor_readings",
                    "ir_events",
                    "ac_state_events",
                )
                for table in tables:
                    count = db.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import re
import stat
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from src.config import AppConfig, load_config, save_config
from src.device import (
    CommandResult,
    ConnectionStatus,
    DeviceMonitor,
    DeviceReading,
    TuyaDevice,
    decide_climate,
    load_local_ir_code,
    local_ir_instruction,
    update_ac_state,
)
from src.storage import LocalAirStore
from src.web import LocalAirService, SettingsError


class ConfigTests(unittest.TestCase):
    def test_round_trip_and_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            expected = AppConfig(
                local_key="not-a-real-key",
                ir_address=0x08E7,
                ir_commands={"power": 0x00},
                ac_state={"power": "on", "mode": "heat"},
                climate_enabled=True,
                climate_start="09:30",
                climate_end="15:45",
                turn_on_at=25.0,
                turn_off_at=21.5,
                aircon_temperature=17,
                sensor_timeout_seconds=180,
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
            self.assertEqual(
                loaded.ac_state,
                {"power": "off", "temperature": "24"},
            )


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

    def test_tuya_network_error_message_is_shortened(self) -> None:
        reading = TuyaDevice._parse_status(
            {"Error": "Network Error: Unable to Connect"},
            datetime.now().astimezone(),
        )

        self.assertEqual(reading.connection_status, ConnectionStatus.ERROR)
        self.assertEqual(reading.error_message, "Device unreachable.")

    def test_tuya_payload_error_message_is_shortened(self) -> None:
        reading = TuyaDevice._parse_status(
            {"Error": "Unexpected Payload from Device"},
            datetime.now().astimezone(),
        )

        self.assertEqual(reading.connection_status, ConnectionStatus.ERROR)
        self.assertEqual(
            reading.error_message,
            "Unexpected device response.",
        )

    def test_unknown_ir_command_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown IR command"):
            local_ir_instruction("../../unexpected", {"temperature": "22"})

    def test_missing_temperature_has_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Sync the current temperature"):
            local_ir_instruction("power", {"power": "off"})

    # Map manual and climate actions to complete-state learned frames.
    def test_piu14_local_instructions(self) -> None:
        power, power_message = local_ir_instruction(
            "power", {"power": "off", "temperature": "24"}
        )
        temperature, temperature_message = local_ir_instruction(
            "temp_up", {"temperature": "29"}
        )
        climate, climate_message = local_ir_instruction(
            "climate_on", {}, 16
        )
        power_off, _message = local_ir_instruction(
            "power", {"power": "on"}
        )

        self.assertEqual(power, "cold 24°C auto")
        self.assertEqual(power_message, "Power turned on at 24°C.")
        self.assertEqual(temperature, "cold 30°C auto")
        self.assertEqual(temperature_message, "Temperature set to 30°C.")
        self.assertEqual(climate, "cold 16°C auto")
        self.assertEqual(climate_message, "Climate control cooling at 16°C.")
        self.assertEqual(power_off, "Power Off")

    # Ensure every temperature and power-off frame is bundled and valid.
    def test_piu14_profile_is_complete(self) -> None:
        for temperature in range(16, 31):
            self.assertGreater(
                len(load_local_ir_code(f"cold {temperature}°C auto")),
                100,
            )
        self.assertGreater(len(load_local_ir_code("Power Off")), 100)

    # Convert and send a learned frame locally without exposing its value.
    def test_local_ir_transmission(self) -> None:
        device = Mock()
        with patch("src.device.time.sleep") as sleep:
            DeviceMonitor._send_local_ir(device, "cold 16°C auto")

        device.study_end.assert_called_once_with()
        device.send_button.assert_called_once()
        self.assertTrue(device.send_button.call_args.args[0])
        self.assertEqual(sleep.call_args_list[0].args, (0.2,))
        self.assertEqual(sleep.call_args_list[1].args, (0.5,))

    # Keep repeated temperature presses inside the PIU14's supported range.
    def test_temperature_state_is_clamped(self) -> None:
        self.assertEqual(
            update_ac_state({"temperature": "30"}, "temp_up"),
            {"temperature": "30", "power": "on"},
        )
        self.assertEqual(
            update_ac_state({"temperature": "16"}, "temp_down"),
            {"temperature": "16", "power": "on"},
        )

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
                "power": "on",
                "mode": "cool",
                "temperature": "23",
                "fan": "high",
                "swing": "on",
            },
        )

    def test_unknown_toggle_state_remains_unknown(self) -> None:
        self.assertEqual(update_ac_state({}, "power"), {})

    # Enforce schedule, hysteresis, and stale-sensor fail-safe decisions.
    def test_climate_control_decisions(self) -> None:
        config = AppConfig(climate_enabled=True)
        midday = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        hot = DeviceReading(24.0, 40, midday, ConnectionStatus.CONNECTED)
        cool = DeviceReading(22.0, 40, midday, ConnectionStatus.CONNECTED)
        stale = DeviceReading(
            25.0,
            40,
            midday - timedelta(seconds=121),
            ConnectionStatus.CONNECTED,
        )

        self.assertEqual(
            decide_climate(config, hot, "off", midday).desired_power,
            "on",
        )
        self.assertEqual(
            decide_climate(config, cool, "on", midday).desired_power,
            "off",
        )
        self.assertEqual(
            decide_climate(config, stale, "on", midday).status,
            "Sensor unavailable",
        )
        self.assertIsNone(
            decide_climate(
                config,
                hot,
                "on",
                midday.replace(hour=16),
            ).desired_power,
        )

class WebUiTests(unittest.TestCase):
    STATIC = Path(__file__).parents[1] / "src" / "static"

    # Every element a script looks up by id must exist in its page.
    def test_scripts_only_reference_existing_elements(self) -> None:
        for page, script in (
            ("index.html", "dashboard.js"),
            ("settings.html", "settings.js"),
        ):
            html = (self.STATIC / page).read_text(encoding="utf-8")
            source = (self.STATIC / script).read_text(encoding="utf-8")
            ids = set(re.findall(r'\$\("([A-Za-z]+)"\)', source))
            self.assertTrue(ids, script)
            for element_id in ids:
                self.assertIn(f'id="{element_id}"', html, f"{script}: {element_id}")

    # The settings form posts every field the service reads.
    def test_settings_form_covers_every_setting(self) -> None:
        html = (self.STATIC / "settings.html").read_text(encoding="utf-8")
        names = set(re.findall(r'<input[^>]*\sname="([a-z_]+)"', html))
        self.assertEqual(
            names,
            {
                "climate_enabled", "climate_start", "climate_end",
                "turn_on_at", "turn_off_at", "aircon_temperature",
                "sensor_timeout_seconds", "ip_address", "protocol_version",
                "device_id", "local_key", "poll_interval_seconds",
                "history_retention_days", "server_host", "server_port",
            },
        )

    # Pages load only bundled assets, so the UI works without internet access.
    def test_pages_use_only_local_assets(self) -> None:
        for page in ("index.html", "settings.html", "app.css"):
            source = (self.STATIC / page).read_text(encoding="utf-8")
            self.assertNotRegex(source, r"https?://")


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

            points = store.history("24h")

            self.assertEqual(len(points), 1)
            expected = timestamp.astimezone().replace(
                minute=timestamp.minute - (timestamp.minute % 5),
                second=0,
            )
            self.assertTrue(
                points[0]["timestamp"].startswith(
                    expected.isoformat(timespec="seconds")
                )
            )
            self.assertIsNotNone(
                datetime.fromisoformat(points[0]["timestamp"]).tzinfo
            )

    def test_hourly_history_excludes_older_readings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "localair.db", 31)
            recent = datetime.now().astimezone() - timedelta(minutes=30)
            old = recent - timedelta(hours=1)
            with store._connect() as db:
                db.executemany(
                    """
                    INSERT INTO sensor_readings (
                      timestamp, temperature_c, humidity_percent, status
                    ) VALUES (?, 21.5, 48, 'Connected')
                    """,
                    [
                        (old.isoformat(timespec="seconds"),),
                        (recent.isoformat(timespec="seconds"),),
                    ],
                )

            points = store.history("1h")

            self.assertEqual(len(points), 1)
            self.assertEqual(
                datetime.fromisoformat(points[0]["timestamp"]).minute,
                recent.minute,
            )

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

            points = store.history("24h")

            self.assertEqual(len(points), 1)

    def test_recent_activity_includes_ir_and_device_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "localair.db", 31)
            store.add_ir_event("localair", "power")
            store.add_reading(
                TuyaDevice._failed_reading(
                    datetime.now().astimezone(),
                    ConnectionStatus.ERROR,
                    "Device returned an error.",
                )
            )

            events = store.recent_activity_events()
            kinds = {event["kind"] for event in events}

            self.assertIn("ir", kinds)
            self.assertIn("device", kinds)

    def test_history_events_excludes_unknown_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "localair.db", 31)
            store.add_ir_event("climate", "climate_on")
            store.add_ir_event("observed", None, "0x1234", known=False)

            events = store.history_events("24h")

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["command"], "climate_on")

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


class HistoryTests(unittest.TestCase):
    # Two-hour buckets group readings into even hours, not just hours.
    def test_thirty_day_history_uses_two_hour_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "test.db", 31)
            base = datetime.now().astimezone().replace(
                hour=2, minute=0, second=0, microsecond=0
            ) - timedelta(days=1)
            for minutes, temperature in ((0, 20.0), (70, 22.0), (130, 30.0)):
                store.add_reading(
                    DeviceReading(
                        temperature,
                        50,
                        base + timedelta(minutes=minutes),
                        ConnectionStatus.CONNECTED,
                    )
                )

            points = store.history("30d")

            self.assertEqual(
                [point["temperature_c"] for point in points], [21.0, 30.0]
            )

    # Power changes become on-periods, including one still running.
    def test_power_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "test.db", 31)
            now = datetime.now().astimezone().replace(microsecond=0)
            start = now - timedelta(hours=3)
            with store._connect() as db:
                for minutes_ago, old, new in (
                    (240, "off", "on"),
                    (150, "on", "off"),
                    (60, "off", "on"),
                ):
                    db.execute(
                        "INSERT INTO ac_state_events (timestamp, source, field, "
                        "old_value, new_value) VALUES (?, 'test', 'power', ?, ?)",
                        (
                            (now - timedelta(minutes=minutes_ago)).isoformat(),
                            old,
                            new,
                        ),
                    )

            runs = store.power_runs(start, now, "on")

            self.assertEqual(
                runs,
                [
                    {
                        "start": start.isoformat(timespec="seconds"),
                        "end": (now - timedelta(minutes=150)).isoformat(
                            timespec="seconds"
                        ),
                    },
                    {
                        "start": (now - timedelta(minutes=60)).isoformat(
                            timespec="seconds"
                        ),
                        "end": now.isoformat(timespec="seconds"),
                    },
                ],
            )
            self.assertEqual(
                store.power_runs(now - timedelta(minutes=30), now, "off"),
                [
                    {
                        "start": (now - timedelta(minutes=30)).isoformat(
                            timespec="seconds"
                        ),
                        "end": now.isoformat(timespec="seconds"),
                    }
                ],
            )


class SettingsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.environ = patch.dict(
            "os.environ", {"LOCALAIR_DATA": self.directory.name}
        )
        self.environ.start()
        save_config(AppConfig(local_key="not-a-real-key"))
        self.service = LocalAirService()
        self.service.monitor = Mock()

    def tearDown(self) -> None:
        self.environ.stop()
        self.directory.cleanup()

    # The Settings API reports whether a key is saved, never the key itself.
    def test_settings_never_include_the_local_key(self) -> None:
        settings = self.service.settings()

        self.assertNotIn("local_key", settings)
        self.assertTrue(settings["local_key_saved"])
        self.assertNotIn("not-a-real-key", str(settings))

    # JSON values save, and a blank key keeps the saved one.
    def test_update_settings_accepts_json_values(self) -> None:
        self.service.update_settings(
            {
                "climate_enabled": True,
                "turn_on_at": 25.5,
                "turn_off_at": "23",
                "local_key": "",
            }
        )

        config = load_config()
        self.assertTrue(config.climate_enabled)
        self.assertEqual((config.turn_on_at, config.turn_off_at), (25.5, 23.0))
        self.assertEqual(config.local_key, "not-a-real-key")

    # Errors name the field the Settings page should highlight.
    def test_settings_errors_name_the_field(self) -> None:
        cases = (
            ({"poll_interval_seconds": "soon"}, "poll_interval_seconds"),
            ({"turn_on_at": 21, "turn_off_at": 22}, "turn_off_at"),
            ({"aircon_temperature": 40}, "aircon_temperature"),
            ({"ip_address": " "}, "ip_address"),
        )
        for data, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(SettingsError) as raised:
                    self.service.update_settings(data)
                self.assertEqual(raised.exception.field, field)

    # Testing unchanged details reuses the live reading instead of reconnecting.
    def test_connection_test_reuses_live_reading(self) -> None:
        self.service.latest_reading = DeviceReading(
            24.5, 60, datetime.now().astimezone(), ConnectionStatus.CONNECTED
        )
        with patch("src.web.TuyaDevice") as device:
            result = self.service.test_connection({})

        device.assert_not_called()
        self.assertEqual(
            result,
            {
                "ok": True,
                "message": "Connected.",
                "temperature_c": 24.5,
                "humidity_percent": 60,
            },
        )


class ClimateServiceTests(unittest.TestCase):
    # Hold the band during the window, send one Off at the end, then allow manual use.
    def test_schedule_end_turns_off_once_then_allows_manual_control(self) -> None:
        clock = [datetime(2026, 1, 1, 15, 59).astimezone()]

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"LOCALAIR_DATA": directory}
        ), patch("src.web.datetime", FakeDatetime):
            save_config(
                AppConfig(
                    local_key="not-a-real-key",
                    climate_enabled=True,
                    ac_state={"power": "off", "temperature": "24"},
                )
            )
            service = LocalAirService()
            sent: list[str] = []
            service.monitor = Mock()
            service.monitor.send_ir.side_effect = lambda key: (
                sent.append(key) or CommandResult(True, "sent")
            )
            service.last_valid_reading = DeviceReading(
                25.0, 40, clock[0], ConnectionStatus.CONNECTED
            )

            service._evaluate_climate()
            self.assertEqual(sent, ["cold 16°C auto"])
            self.assertEqual(service.config.ac_state["power"], "on")
            self.assertEqual(service.config.ac_state["temperature"], "16")

            clock[0] = clock[0].replace(hour=16, minute=0)
            service._evaluate_climate()
            self.assertEqual(sent[-1], "Power Off")
            self.assertEqual(service.config.ac_state["power"], "off")
            self.assertEqual(
                service.store.recent_ir_events(1)[0]["command"], "schedule_end"
            )

            sent.clear()
            service.send_ir("power")
            self.assertEqual(sent, ["cold 16°C auto"])
            clock[0] = clock[0].replace(hour=17)
            service._evaluate_climate()
            self.assertEqual(sent, ["cold 16°C auto"])
            self.assertEqual(service.config.ac_state["power"], "on")
            self.assertEqual(service.climate.status, "Outside schedule")


if __name__ == "__main__":
    unittest.main()

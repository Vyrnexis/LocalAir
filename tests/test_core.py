from __future__ import annotations

import queue
import re
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from src.config import (
    AppConfig,
    ClimatePeriod,
    ConfigError,
    load_config,
    save_config,
)
from src.device import (
    CommandResult,
    ConnectionStatus,
    DeviceMonitor,
    DeviceReading,
    NOT_CONFIGURED,
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
                ip_address="192.0.2.10",
                device_id="test-device",
                local_key="not-a-real-key",
                ac_state={"power": "on", "temperature": "21"},
                climate_enabled=True,
                climate_periods=(
                    ClimatePeriod(
                        name="Day",
                        start="09:30",
                        end="15:45",
                        turn_on_at=25.0,
                        turn_off_at=21.5,
                        aircon_temperature=17,
                    ),
                    ClimatePeriod(
                        name='Night "late"',
                        enabled=False,
                        start="16:00",
                        end="09:00",
                        turn_on_at=27.0,
                        turn_off_at=25.0,
                        aircon_temperature=22,
                    ),
                ),
                sensor_timeout_seconds=180,
            )

            save_config(expected, path)

            self.assertEqual(load_config(path), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    # A config saved before periods existed loads as a single period.
    def test_single_schedule_config_loads_as_one_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[climate]\n"
                "enabled = true\n"
                'start_time = "10:00"\n'
                'end_time = "15:30"\n'
                "turn_on_at = 24.5\n"
                "turn_off_at = 22.0\n"
                "aircon_temperature = 18\n",
                encoding="utf-8",
            )

            config = load_config(path)

        self.assertEqual(
            config.climate_periods,
            (
                ClimatePeriod(
                    start="10:00",
                    end="15:30",
                    turn_on_at=24.5,
                    turn_off_at=22.0,
                    aircon_temperature=18,
                ),
            ),
        )

    # The old master switch being off must not switch the period itself off.
    def test_single_schedule_with_cooling_off_keeps_period_on(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[climate]\nenabled = false\nstart_time = "10:00"\n',
                encoding="utf-8",
            )

            config = load_config(path)

        self.assertFalse(config.climate_enabled)
        self.assertTrue(config.climate_periods[0].enabled)

    # Periods may cross midnight and meet end to end, but not overlap.
    def test_period_validation(self) -> None:
        day = ClimatePeriod(start="10:00", end="15:30")
        night = ClimatePeriod(start="16:00", end="09:00")
        AppConfig(climate_periods=(day, night)).validated()
        AppConfig(
            climate_periods=(day, replace(night, start="15:30", end="10:00"))
        ).validated()
        # A disabled period may overlap, since it never runs.
        AppConfig(
            climate_periods=(day, replace(night, end="11:00", enabled=False))
        ).validated()

        cases = (
            ((day, replace(night, end="10:30")), "climate_periods.1.start"),
            ((day, replace(night, start="15:00")), "climate_periods.1.start"),
            ((replace(day, end="10:00"),), "climate_periods.0.end"),
            (
                (day, replace(night, turn_off_at=28.0)),
                "climate_periods.1.turn_off_at",
            ),
            (
                (replace(day, aircon_temperature=31),),
                "climate_periods.0.aircon_temperature",
            ),
            ((replace(day, start="25:00"),), "climate_periods.0.start"),
            ((), "climate_periods"),
            ((day,) * 5, "climate_periods"),
        )
        for periods, field in cases:
            with self.subTest(field=field, periods=len(periods)):
                with self.assertRaises(ConfigError) as raised:
                    AppConfig(climate_periods=periods).validated()
                self.assertEqual(raised.exception.field, field)

    def test_partial_config_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[device]\nlocal_key = "placeholder"\n',
                encoding="utf-8",
            )

            loaded = load_config(path)

            self.assertEqual(loaded.ip_address, "")
            self.assertFalse(loaded.sensor_configured)
            self.assertEqual(loaded.protocol_version, 3.5)
            self.assertEqual(loaded.poll_interval_seconds, 30)
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

        device.send_button.assert_called_once()
        self.assertTrue(device.send_button.call_args.args[0])
        sleep.assert_called_once_with(0.5)

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

    def test_sent_commands_update_assumed_state(self) -> None:
        state = {"power": "on", "temperature": "22"}

        state = update_ac_state(state, "power")
        self.assertEqual(state, {"power": "off", "temperature": "22"})
        state = update_ac_state(state, "temp_up")
        self.assertEqual(state, {"power": "on", "temperature": "23"})
        state = update_ac_state(state, "climate_off")
        self.assertEqual(state, {"power": "off", "temperature": "23"})

    # Without sensor details the monitor never connects, and sends fail fast.
    def test_monitor_waits_for_sensor_details(self) -> None:
        readings = []
        monitor = DeviceMonitor(AppConfig(), readings.append)
        replies = queue.Queue()
        monitor.actions.put(("send", "Power Off", replies))
        monitor.actions.put(("reconnect", None, None))

        with patch(
            "tinytuya.Contrib.IRRemoteControlDevice.IRRemoteControlDevice"
        ) as device:
            monitor._connected_loop()

        device.assert_not_called()
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0].error_message, NOT_CONFIGURED)
        self.assertFalse(replies.get_nowait().ok)

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

    # Each period applies its own thresholds, including across midnight.
    def test_climate_uses_the_active_period(self) -> None:
        config = AppConfig(
            climate_enabled=True,
            climate_periods=(
                ClimatePeriod(name="Day", start="10:00", end="15:30"),
                ClimatePeriod(
                    name="Night",
                    start="16:00",
                    end="09:00",
                    turn_on_at=27.0,
                    turn_off_at=25.0,
                ),
            ),
        )

        def decide(hour: int, minute: int, temperature: float):
            now = datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc)
            reading = DeviceReading(
                temperature, 40, now, ConnectionStatus.CONNECTED
            )
            return decide_climate(config, reading, "off", now)

        self.assertEqual(decide(12, 0, 25.0).desired_power, "on")
        self.assertEqual(decide(12, 0, 25.0).period, 0)
        self.assertEqual(decide(20, 0, 25.0).desired_power, "off")
        self.assertEqual(decide(2, 0, 27.5).desired_power, "on")
        self.assertEqual(decide(2, 0, 27.5).period, 1)
        self.assertEqual(decide(8, 59, 27.5).period, 1)
        for hour, minute in ((9, 0), (15, 30), (15, 59)):
            with self.subTest(time=f"{hour}:{minute}"):
                decision = decide(hour, minute, 30.0)
                self.assertEqual(decision.status, "Outside schedule")
                self.assertIsNone(decision.period)
        self.assertIn("Night starts at 16:00", decide(15, 45, 30.0).detail)
        self.assertIn("Day starts at 10:00", decide(9, 30, 30.0).detail)

        # A switched-off period never runs.
        config = replace(
            config,
            climate_periods=(
                config.climate_periods[0],
                replace(config.climate_periods[1], enabled=False),
            ),
        )
        self.assertIsNone(decide(20, 0, 30.0).period)

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
                "climate_enabled",
                "sensor_timeout_seconds", "ip_address", "protocol_version",
                "device_id", "local_key", "poll_interval_seconds",
                "history_retention_days", "server_host", "server_port",
            },
        )

    # Period cards post every field the service reads for a period.
    def test_period_cards_cover_every_period_field(self) -> None:
        source = (self.STATIC / "settings.js").read_text(encoding="utf-8")
        names = set(re.findall(r'data-period="([a-z_]+)"', source))
        self.assertEqual(
            names,
            {
                "name", "enabled", "start", "end", "turn_on_at",
                "turn_off_at", "aircon_temperature",
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

    # Rows from the removed remote listener have no command and are skipped.
    def test_activity_skips_unrecognised_remote_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAirStore(Path(directory) / "localair.db", 31)
            store.add_ir_event("climate", "climate_on")
            with store._db() as db:
                db.execute(
                    "INSERT INTO ir_events (timestamp, source, command, "
                    "nec_hex, known) VALUES (?, 'observed', NULL, '0x1234', 0)",
                    (datetime.now().astimezone().isoformat(),),
                )

            events = store.recent_activity_events()

            self.assertEqual(
                [event["command"] for event in events], ["climate_on"]
            )

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
        save_config(
            AppConfig(
                ip_address="192.0.2.10",
                device_id="test-device",
                local_key="not-a-real-key",
            )
        )
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

    # Container-set host and port are reported so Settings can show them.
    def test_settings_report_container_overrides(self) -> None:
        with patch.dict("os.environ", {"LOCALAIR_PORT": "9000"}):
            overrides = self.service.settings()["env_overrides"]

        self.assertEqual(overrides["server_port"], "9000")
        self.assertIsNone(overrides["server_host"])

    # JSON values save, and a blank key keeps the saved one.
    def test_update_settings_accepts_json_values(self) -> None:
        self.service.update_settings(
            {
                "climate_enabled": True,
                "climate_periods": [
                    {
                        "name": " Day ",
                        "start": "10:00",
                        "end": "15:30",
                        "turn_on_at": 25.5,
                        "turn_off_at": "23",
                        "aircon_temperature": 17,
                    },
                    {
                        "name": "Night",
                        "enabled": False,
                        "start": "16:00",
                        "end": "09:00",
                        "turn_on_at": 27,
                        "turn_off_at": 25,
                        "aircon_temperature": "20",
                    },
                ],
                "local_key": "",
            }
        )

        config = load_config()
        self.assertTrue(config.climate_enabled)
        day, night = config.climate_periods
        self.assertEqual(day.name, "Day")
        self.assertEqual((day.turn_on_at, day.turn_off_at), (25.5, 23.0))
        self.assertEqual((night.start, night.end), ("16:00", "09:00"))
        self.assertEqual(night.aircon_temperature, 20)
        self.assertFalse(night.enabled)
        self.assertEqual(config.local_key, "not-a-real-key")
        self.assertEqual(
            [p["label"] for p in self.service.settings()["climate_periods"]],
            ["Day", "Night"],
        )

    # A submitted period never borrows values from the saved period at the
    # same position, and a null name counts as blank.
    def test_submitted_periods_do_not_inherit_by_position(self) -> None:
        self.service.update_settings(
            {
                "climate_periods": [
                    {"name": "Day", "start": "10:00", "end": "15:30",
                     "turn_on_at": 29, "turn_off_at": 28,
                     "aircon_temperature": 25},
                    {"name": "Night", "start": "16:00", "end": "09:00"},
                ]
            }
        )
        self.service.update_settings(
            {"climate_periods": [{"name": None, "start": "16:00", "end": "09:00"}]}
        )

        (period,) = load_config().climate_periods
        self.assertEqual(period.name, "")
        self.assertEqual(
            (period.turn_on_at, period.turn_off_at, period.aircon_temperature),
            (ClimatePeriod().turn_on_at, ClimatePeriod().turn_off_at,
             ClimatePeriod().aircon_temperature),
        )

    # Errors name the field the Settings page should highlight.
    def test_settings_errors_name_the_field(self) -> None:
        cases = (
            ({"poll_interval_seconds": "soon"}, "poll_interval_seconds"),
            (
                {"climate_periods": [{"turn_on_at": 21, "turn_off_at": 22}]},
                "climate_periods.0.turn_off_at",
            ),
            (
                {"climate_periods": [{}, {"aircon_temperature": "cold"}]},
                "climate_periods.1.aircon_temperature",
            ),
            ({"climate_periods": [{}, {}]}, "climate_periods.1.start"),
            ({"climate_periods": []}, "climate_periods"),
            ({"climate_periods": "day"}, "climate_periods"),
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
                    ip_address="192.0.2.10",
                    device_id="test-device",
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
                service.store.recent_activity_events(1)[0]["command"],
                "schedule_end",
            )

            sent.clear()
            service.send_ir("power")
            self.assertEqual(sent, ["cold 16°C auto"])
            clock[0] = clock[0].replace(hour=17)
            service._evaluate_climate()
            self.assertEqual(sent, ["cold 16°C auto"])
            self.assertEqual(service.config.ac_state["power"], "on")
            self.assertEqual(service.climate.status, "Outside schedule")


    def _service(self, directory: str, periods: tuple[ClimatePeriod, ...]):
        save_config(
            AppConfig(
                ip_address="192.0.2.10",
                device_id="test-device",
                local_key="not-a-real-key",
                climate_enabled=True,
                climate_periods=periods,
                ac_state={"power": "off", "temperature": "24"},
            )
        )
        service = LocalAirService()
        sent: list[str] = []
        service.monitor = Mock()
        service.monitor.send_ir.side_effect = lambda key: (
            sent.append(key) or CommandResult(True, "sent")
        )
        return service, sent

    # Back-to-back periods hand over without an Off, and the AC is sent the
    # new period's temperature.
    def test_back_to_back_periods_hand_over_without_turning_off(self) -> None:
        clock = [datetime(2026, 1, 1, 15, 59).astimezone()]

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"LOCALAIR_DATA": directory}
        ), patch("src.web.datetime", FakeDatetime):
            service, sent = self._service(
                directory,
                (
                    ClimatePeriod(start="10:00", end="16:00"),
                    ClimatePeriod(
                        start="16:00",
                        end="22:00",
                        turn_on_at=27.0,
                        turn_off_at=25.0,
                        aircon_temperature=20,
                    ),
                ),
            )
            service.last_valid_reading = DeviceReading(
                26.0, 40, clock[0], ConnectionStatus.CONNECTED
            )

            service._evaluate_climate()
            self.assertEqual(sent, ["cold 16°C auto"])

            # 26 °C is inside the evening band, so the AC stays on at 20 °C.
            clock[0] = clock[0].replace(hour=16, minute=0)
            service.last_valid_reading = DeviceReading(
                26.0, 40, clock[0], ConnectionStatus.CONNECTED
            )
            service._evaluate_climate()
            self.assertEqual(sent, ["cold 16°C auto", "cold 20°C auto"])
            self.assertEqual(service.config.ac_state["temperature"], "20")
            self.assertEqual(service.climate.period, 1)

            service._evaluate_climate()
            self.assertEqual(len(sent), 2)

            clock[0] = clock[0].replace(hour=22, minute=0)
            service._evaluate_climate()
            self.assertEqual(sent[-1], "Power Off")
            self.assertEqual(
                service.store.recent_activity_events(1)[0]["command"],
                "schedule_end",
            )

    # A handover sends nothing when the AC is off, or already at the new
    # period's temperature.
    def test_handover_sends_nothing_when_nothing_changes(self) -> None:
        clock = [datetime(2026, 1, 1, 15, 59).astimezone()]

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        for power, room in (("off", 23.0), ("on", 26.0)):
            with self.subTest(power=power), tempfile.TemporaryDirectory() as directory, patch.dict(
                "os.environ", {"LOCALAIR_DATA": directory}
            ), patch("src.web.datetime", FakeDatetime):
                clock[0] = clock[0].replace(hour=15, minute=59)
                service, sent = self._service(
                    directory,
                    (
                        ClimatePeriod(start="10:00", end="16:00"),
                        ClimatePeriod(
                            start="16:00", end="22:00",
                            turn_on_at=27.0, turn_off_at=25.0,
                        ),
                    ),
                )
                service.last_valid_reading = DeviceReading(
                    room, 40, clock[0], ConnectionStatus.CONNECTED
                )
                service._evaluate_climate()
                before = list(sent)

                clock[0] = clock[0].replace(hour=16, minute=0)
                service.last_valid_reading = DeviceReading(
                    room, 40, clock[0], ConnectionStatus.CONNECTED
                )
                service._evaluate_climate()
                self.assertEqual(sent, before)
                self.assertEqual(service.climate.period, 1)
                self.assertEqual(service.config.ac_state["power"], power)

    # Entering a period after a gap sends nothing while the AC is already off.
    def test_period_after_a_gap_does_not_resend_off(self) -> None:
        clock = [datetime(2026, 1, 1, 15, 29).astimezone()]

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"LOCALAIR_DATA": directory}
        ), patch("src.web.datetime", FakeDatetime):
            service, sent = self._service(
                directory,
                (
                    ClimatePeriod(start="10:00", end="15:30"),
                    ClimatePeriod(
                        start="16:00",
                        end="09:00",
                        turn_on_at=27.0,
                        turn_off_at=25.0,
                    ),
                ),
            )
            service.last_valid_reading = DeviceReading(
                23.0, 40, clock[0], ConnectionStatus.CONNECTED
            )
            service._evaluate_climate()
            self.assertEqual(sent, ["Power Off"])

            clock[0] = clock[0].replace(minute=30)
            service._evaluate_climate()
            self.assertEqual(sent, ["Power Off", "Power Off"])

            clock[0] = clock[0].replace(hour=16, minute=0)
            service.last_valid_reading = DeviceReading(
                24.0, 40, clock[0], ConnectionStatus.CONNECTED
            )
            service._evaluate_climate()
            self.assertEqual(len(sent), 2)
            self.assertEqual(service.climate.period, 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.config import AppConfig, load_config, save_config
from src.device import ConnectionStatus, TuyaDevice, update_ac_state


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


if __name__ == "__main__":
    unittest.main()

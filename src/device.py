"""Read the Tuya sensor and send learned IR commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import queue
import threading
import time
from typing import Any

from .config import AppConfig, state_path


IR_COMMANDS = {
    "power",
    "temp_up",
    "temp_down",
    "mode_heat",
    "mode_cool",
    "mode_dry",
    "mode_fan",
    "fan_high",
    "fan_mid",
    "fan_low",
    "swing",
    "timer",
    "unit_cf",
}


class ConnectionStatus(StrEnum):
    CONNECTED = "Connected"
    DISCONNECTED = "Disconnected"
    ERROR = "Error"


@dataclass(frozen=True, slots=True)
class DeviceReading:
    temperature_c: float | None
    humidity_percent: int | None
    timestamp: datetime
    connection_status: ConnectionStatus
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.connection_status is ConnectionStatus.CONNECTED


@dataclass(frozen=True, slots=True)
class CommandResult:
    ok: bool
    message: str


def update_ac_state(state: dict[str, str], command: str) -> dict[str, str]:
    """Apply an observed or transmitted remote command to assumed state."""
    updated = dict(state)
    if command == "power":
        current = updated.get("power")
        if current in {"on", "off"}:
            updated["power"] = "off" if current == "on" else "on"
    elif command.startswith("mode_"):
        updated["mode"] = command.removeprefix("mode_")
    elif command.startswith("fan_"):
        updated["fan"] = command.removeprefix("fan_")
    elif command in {"temp_up", "temp_down"}:
        try:
            temperature = int(updated["temperature"])
        except (KeyError, ValueError):
            pass
        else:
            updated["temperature"] = str(
                temperature + (1 if command == "temp_up" else -1)
            )
    elif command in {"swing", "timer"}:
        current = updated.get(command)
        if current in {"on", "off"}:
            updated[command] = "off" if current == "on" else "on"
    return updated


class TuyaDevice:
    """TinyTuya wrapper for this temperature/humidity IR device."""

    def get_status(self, config: AppConfig) -> DeviceReading:
        now = datetime.now().astimezone()
        if not config.local_key:
            return self._failed_reading(
                now,
                ConnectionStatus.DISCONNECTED,
                "Open Settings and enter the device local key.",
            )

        try:
            import tinytuya

            device = tinytuya.Device(
                config.device_id, config.ip_address, config.local_key
            )
            device.set_version(config.protocol_version)
            device.set_socketTimeout(8)
            return self._parse_status(device.status(), now)
        except ImportError:
            return self._failed_reading(
                now,
                ConnectionStatus.ERROR,
                "TinyTuya is not installed.",
            )
        except (OSError, TimeoutError):
            return self._failed_reading(
                now,
                ConnectionStatus.DISCONNECTED,
                "Device did not respond. Check its address and network.",
            )
        except Exception as exc:
            return self._failed_reading(
                now,
                ConnectionStatus.ERROR,
                f"Could not read device ({type(exc).__name__}).",
            )

    def send_ir(self, config: AppConfig, command: str) -> CommandResult:
        if command not in IR_COMMANDS:
            return CommandResult(False, "Unknown IR command.")
        if not config.local_key:
            return CommandResult(
                False, "Open Settings and enter the device local key."
            )

        command_byte = config.ir_commands.get(command)
        if command_byte is None:
            return CommandResult(False, "This IR command has not been learned.")

        try:
            from tinytuya.Contrib.IRRemoteControlDevice import (
                IRRemoteControlDevice,
            )

            pulses = IRRemoteControlDevice.nec_to_pulses(
                self._nec_frame(config.ir_address, command_byte)
            )
            code = IRRemoteControlDevice.pulses_to_base64(pulses)

            device = IRRemoteControlDevice(
                config.device_id,
                config.ip_address,
                config.local_key,
                version=config.protocol_version,
                control_type=1,
            )
            device.set_socketTimeout(8)
            device.send_button(code)
            return CommandResult(True, "IR command sent.")
        except ImportError:
            return CommandResult(False, "TinyTuya is not installed.")
        except Exception as exc:
            return CommandResult(
                False, f"Could not send IR command ({type(exc).__name__})."
            )

    @staticmethod
    def _nec_frame(address: int, command: int) -> int:
        return (address << 16) | (command << 8) | (command ^ 0xFF)

    @staticmethod
    def _parse_status(
        data: Any, timestamp: datetime
    ) -> DeviceReading:
        if not isinstance(data, dict):
            return TuyaDevice._failed_reading(
                timestamp,
                ConnectionStatus.ERROR,
                "Device returned an error. Check its settings.",
            )
        if "Error" in data:
            return TuyaDevice._failed_reading(
                timestamp,
                ConnectionStatus.ERROR,
                TuyaDevice._tuya_error_message(data),
            )

        dps = data.get("dps")
        if not isinstance(dps, dict):
            return TuyaDevice._failed_reading(
                timestamp,
                ConnectionStatus.ERROR,
                "Device response did not contain sensor data.",
            )

        temperature_raw = dps.get("1", dps.get(1))
        humidity_raw = dps.get("2", dps.get(2))
        try:
            temperature = float(temperature_raw) / 10.0
            humidity = int(humidity_raw)
        except (TypeError, ValueError):
            return TuyaDevice._failed_reading(
                timestamp,
                ConnectionStatus.ERROR,
                "Device returned invalid sensor values.",
            )

        if not -50 <= temperature <= 100 or not 0 <= humidity <= 100:
            return TuyaDevice._failed_reading(
                timestamp,
                ConnectionStatus.ERROR,
                "Sensor values were outside the expected range.",
            )

        return DeviceReading(
            temperature_c=temperature,
            humidity_percent=humidity,
            timestamp=timestamp,
            connection_status=ConnectionStatus.CONNECTED,
        )

    @staticmethod
    def _failed_reading(
        timestamp: datetime,
        status: ConnectionStatus,
        message: str,
    ) -> DeviceReading:
        return DeviceReading(
            temperature_c=None,
            humidity_percent=None,
            timestamp=timestamp,
            connection_status=status,
            error_message=message,
        )

    @staticmethod
    def _tuya_error_message(data: dict[str, Any]) -> str:
        detail = data.get("Error") or data.get("Err") or data.get("error")
        if detail is None:
            return "Device returned an error. Check its settings."
        text = str(detail).strip()
        if not text:
            return "Device returned an error. Check its settings."
        if len(text) > 160:
            text = text[:157] + "..."
        return f"Device returned an error: {text}"


class DeviceMonitor:
    """Own the Tuya connection and serialize listening, polling, and sending."""

    def __init__(
        self,
        config: AppConfig,
        on_reading,
        on_ir,
        on_result,
    ) -> None:
        self.config = config
        self.on_reading = on_reading
        self.on_ir = on_ir
        self.on_result = on_result
        self.actions: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.stop_event = threading.Event()
        self.last_unknown: tuple[str, float] | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="localair-device",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.actions.put(("stop", None))
        self.thread.join(timeout=3)

    def update_config(self, config: AppConfig) -> None:
        self.config = config
        self.actions.put(("reconnect", None))

    def refresh(self) -> None:
        self.actions.put(("refresh", None))

    def send(self, command: str) -> None:
        self.actions.put(("send", command))

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._connected_loop()
            except Exception as exc:
                self.on_reading(
                    TuyaDevice._failed_reading(
                        datetime.now().astimezone(),
                        ConnectionStatus.DISCONNECTED,
                        f"Device connection failed ({type(exc).__name__}).",
                    )
                )
                self.stop_event.wait(3)

    def _connected_loop(self) -> None:
        from tinytuya.Contrib.IRRemoteControlDevice import (
            IRRemoteControlDevice,
        )

        config = self.config
        device = IRRemoteControlDevice(
            config.device_id,
            config.ip_address,
            config.local_key,
            version=config.protocol_version,
            control_type=1,
        )
        device.set_socketPersistent(True)
        device.set_socketTimeout(1)
        if not self._poll_sensor(device, config):
            self.stop_event.wait(3)
            return
        self._start_learning(device)
        next_poll = time.monotonic() + config.poll_interval_seconds

        try:
            while not self.stop_event.is_set():
                try:
                    action, command = self.actions.get_nowait()
                except queue.Empty:
                    action, command = "", None

                if action in {"stop", "reconnect"}:
                    return
                if action == "refresh":
                    next_poll = min(next_poll, time.monotonic() + 0.2)
                elif action == "send" and command:
                    device.study_end()
                    result = self._send_with_device(device, config, command)
                    self.on_result(result)
                    if result.ok:
                        self.on_ir(command, "localair")
                    self._start_learning(device)

                if time.monotonic() >= next_poll:
                    if not self._poll_sensor(device, config):
                        return
                    next_poll = (
                        time.monotonic() + config.poll_interval_seconds
                    )
                    self._start_learning(device)
                else:
                    response = device._send_receive(None)

                if self._handle_ir_response(response, config):
                    self._start_learning(device)
        finally:
            try:
                device.study_end()
            except Exception:
                pass

    def _poll_sensor(self, device, config: AppConfig) -> bool:
        response = device.status()
        reading = TuyaDevice._parse_status(
            response, datetime.now().astimezone()
        )
        self.on_reading(reading)
        return reading.ok

    @staticmethod
    def _start_learning(device) -> None:
        """Arm IR learning and give the device a moment to enter that mode."""
        device.study_end()
        time.sleep(0.2)
        device.study_start()
        time.sleep(0.2)

    @staticmethod
    def _send_with_device(device, config, command: str) -> CommandResult:
        if command not in IR_COMMANDS:
            return CommandResult(False, "Unknown IR command.")
        command_byte = config.ir_commands.get(command)
        if command_byte is None:
            return CommandResult(False, "This IR command has not been learned.")
        try:
            pulses = device.nec_to_pulses(
                TuyaDevice._nec_frame(config.ir_address, command_byte)
            )
            device.send_button(device.pulses_to_base64(pulses))
            return CommandResult(True, "IR command sent.")
        except Exception as exc:
            return CommandResult(
                False, f"Could not send IR command ({type(exc).__name__})."
            )

    def _handle_ir_response(self, response: Any, config: AppConfig) -> bool:
        if not isinstance(response, dict) or not isinstance(
            response.get("dps"), dict
        ):
            return False
        dps = response["dps"]
        code = dps.get("202") or dps.get(202)
        if not isinstance(code, str) or not code.strip():
            return False

        from tinytuya.Contrib.IRRemoteControlDevice import (
            IRRemoteControlDevice,
        )

        pulses = IRRemoteControlDevice.base64_to_pulses(code.strip())
        decoded = IRRemoteControlDevice.pulses_to_nec(pulses)
        expected = {
            TuyaDevice._nec_frame(config.ir_address, value): name
            for name, value in config.ir_commands.items()
        }
        if decoded:
            frame = int(decoded[0]["hex"], 16)
            command = expected.get(frame)
            if command:
                self.on_ir(command, "observed")
                return True
            self._log_unknown(
                f"unknown NEC hex={decoded[0]['hex']} pulses={len(pulses)}"
            )
        else:
            self._log_unknown(f"unknown protocol pulses={len(pulses)}")
        return True

    def _log_unknown(self, message: str) -> None:
        now = time.monotonic()
        if self.last_unknown and self.last_unknown[0] == message:
            if now - self.last_unknown[1] < 5:
                return
        self.last_unknown = (message, now)
        path = state_path() / "unknown-ir.log"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as log:
            log.write(f"{timestamp} {message}\n")
        path.chmod(0o600)

"""Read the Tuya sensor and control the PIU14 over the local network."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
from pathlib import Path
import queue
import threading
import time
from typing import Any

from .config import AppConfig


IR_COMMANDS = {
    "power",
    "temp_up",
    "temp_down",
}
PIU14_CODES_PATH = Path(__file__).with_name("piu14_ir_codes.json")
NOT_CONFIGURED = "Open Settings and enter the sensor's details."


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


@dataclass(frozen=True, slots=True)
class ClimateDecision:
    status: str
    detail: str
    desired_power: str | None


# Decide the required AC state from schedule, sensor freshness, and hysteresis.
def decide_climate(
    config: AppConfig,
    reading: DeviceReading | None,
    power: str,
    now: datetime,
) -> ClimateDecision:
    if not config.climate_enabled:
        return ClimateDecision("Disabled", "Automation is off.", None)

    current_time = now.strftime("%H:%M")
    schedule = f"{config.climate_start}–{config.climate_end}"
    # Outside the window the AC is left to manual control; the service sends
    # one Off when the window ends.
    if not config.climate_start <= current_time < config.climate_end:
        return ClimateDecision(
            "Outside schedule",
            f"Active daily {schedule}. Manual control until then.",
            None,
        )

    if reading is None:
        return ClimateDecision(
            "Sensor unavailable", "Waiting for a valid reading.", "off"
        )
    age = max(0.0, (now - reading.timestamp).total_seconds())
    if age > config.sensor_timeout_seconds:
        return ClimateDecision(
            "Sensor unavailable",
            f"Last valid reading was {round(age)} seconds ago.",
            "off",
        )

    temperature = reading.temperature_c
    if temperature is None:
        return ClimateDecision(
            "Sensor unavailable", "Waiting for a valid reading.", "off"
        )
    if temperature >= config.turn_on_at:
        return ClimateDecision(
            "Cooling",
            f"Room is at or above {config.turn_on_at:g}°C.",
            "on",
        )
    if temperature <= config.turn_off_at:
        return ClimateDecision(
            "Holding",
            f"Room reached {config.turn_off_at:g}°C or below.",
            "off",
        )
    return ClimateDecision(
        "Cooling" if power == "on" else "Holding",
        f"Inside the {config.turn_off_at:g}–{config.turn_on_at:g}°C comfort band.",
        "on" if power == "on" else "off",
    )


def update_ac_state(state: dict[str, str], command: str) -> dict[str, str]:
    """Apply a transmitted command to the assumed AC state."""
    updated = dict(state)
    if command == "power":
        current = updated.get("power")
        if current in {"on", "off"}:
            updated["power"] = "off" if current == "on" else "on"
    elif command == "climate_on":
        updated["power"] = "on"
    elif command == "climate_off":
        updated["power"] = "off"
    elif command in {"temp_up", "temp_down"}:
        try:
            temperature = int(updated["temperature"])
        except (KeyError, ValueError):
            pass
        else:
            change = 1 if command == "temp_up" else -1
            updated["temperature"] = str(
                min(30, max(16, temperature + change))
            )
            updated["power"] = "on"
    return updated


# Select the complete-state PIU14 frame and success message for one action.
def local_ir_instruction(
    command: str,
    state: dict[str, str],
    climate_temperature: int = 16,
) -> tuple[str, str]:
    if command == "climate_off" or (
        command == "power" and state.get("power") == "on"
    ):
        message = (
            "Climate control turned the AC off."
            if command == "climate_off"
            else "Power turned off."
        )
        return "Power Off", message

    if command == "climate_on":
        temperature = climate_temperature
        message = f"Climate control cooling at {temperature}°C."
    else:
        try:
            temperature = int(state["temperature"])
        except (KeyError, ValueError):
            raise ValueError(
                "Sync the current temperature before controlling the AC."
            ) from None
        if command in {"temp_up", "temp_down"}:
            temperature += 1 if command == "temp_up" else -1
            temperature = min(30, max(16, temperature))
            message = f"Temperature set to {temperature}°C."
        elif command == "power":
            message = f"Power turned on at {temperature}°C."
        else:
            raise ValueError("Unknown IR command.")

    if not 16 <= temperature <= 30:
        raise ValueError("PIU14 temperature must be between 16°C and 30°C.")
    return f"cold {temperature}°C auto", message


# Load one bundled PIU14 code without exposing the raw signal in errors.
def load_local_ir_code(key_name: str) -> str:
    try:
        codes = json.loads(PIU14_CODES_PATH.read_text(encoding="utf-8"))
        code = codes[key_name]
        if not isinstance(code, str) or not code or len(code) % 2:
            raise ValueError
        bytes.fromhex(code)
        return code
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(f"Local IR code is unavailable for {key_name}.") from None


class TuyaDevice:
    """TinyTuya wrapper for reading the temperature/humidity device."""

    def get_status(self, config: AppConfig) -> DeviceReading:
        now = datetime.now().astimezone()
        if not config.sensor_configured:
            return self._failed_reading(
                now, ConnectionStatus.DISCONNECTED, NOT_CONFIGURED
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
            return "Device error. Check settings."
        text = str(detail).strip()
        if not text:
            return "Device error. Check settings."

        lowered = text.lower()
        if "unexpected payload" in lowered:
            return "Unexpected device response."
        if "check device key or version" in lowered:
            return "Check device key or protocol version."
        if "device unreachable" in lowered or "unable to connect" in lowered:
            return "Device unreachable."

        if len(text) > 120:
            text = text[:117] + "..."
        return f"Device error: {text}"


class DeviceMonitor:
    """Own the Tuya connection and serialize sensor polling and IR sends."""

    def __init__(self, config: AppConfig, on_reading) -> None:
        self.config = config
        self.on_reading = on_reading
        self.actions: queue.Queue[
            tuple[str, str | None, queue.Queue[CommandResult] | None]
        ] = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="localair-device",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.actions.put(("stop", None, None))
        self.thread.join(timeout=3)

    def update_config(self, config: AppConfig) -> None:
        self.config = config
        self.actions.put(("reconnect", None, None))

    def refresh(self) -> None:
        self.actions.put(("refresh", None, None))

    # Queue a learned IR frame on the monitor-owned device connection.
    def send_ir(self, key_name: str) -> CommandResult:
        result_queue: queue.Queue[CommandResult] = queue.Queue(maxsize=1)
        self.actions.put(("send", key_name, result_queue))
        try:
            return result_queue.get(timeout=10)
        except queue.Empty:
            return CommandResult(False, "Local IR device did not respond.")

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

    # Without sensor details there is nothing to connect to; wait for Settings.
    def _wait_for_settings(self) -> None:
        self.on_reading(
            TuyaDevice._failed_reading(
                datetime.now().astimezone(),
                ConnectionStatus.DISCONNECTED,
                NOT_CONFIGURED,
            )
        )
        while not self.stop_event.is_set():
            action, _command, result_queue = self.actions.get()
            if action in {"stop", "reconnect"}:
                return
            if result_queue:
                result_queue.put(CommandResult(False, NOT_CONFIGURED))

    def _connected_loop(self) -> None:
        from tinytuya.Contrib.IRRemoteControlDevice import (
            IRRemoteControlDevice,
        )

        config = self.config
        if not config.sensor_configured:
            self._wait_for_settings()
            return
        device = IRRemoteControlDevice(
            config.device_id,
            config.ip_address,
            config.local_key,
            version=config.protocol_version,
            control_type=1,
        )
        device.set_socketPersistent(True)
        device.set_socketTimeout(0.25)
        if not self._poll_sensor(device, config):
            self.stop_event.wait(3)
            return
        next_poll = time.monotonic() + config.poll_interval_seconds

        while not self.stop_event.is_set():
            try:
                action, command, result_queue = self.actions.get_nowait()
            except queue.Empty:
                action, command, result_queue = "", None, None

            if action in {"stop", "reconnect"}:
                return
            if action == "refresh":
                next_poll = min(next_poll, time.monotonic() + 0.2)
            if action == "send" and command and result_queue:
                try:
                    self._send_local_ir(device, command)
                    result = CommandResult(True, "Local IR command sent.")
                except Exception as exc:
                    result = CommandResult(
                        False,
                        f"Could not send local IR command "
                        f"({type(exc).__name__}).",
                    )
                result_queue.put(result)
                continue
            if time.monotonic() >= next_poll:
                if not self._poll_sensor(device, config):
                    return
                next_poll = time.monotonic() + config.poll_interval_seconds
            else:
                # Drain anything the device pushes so replies stay in order;
                # the short socket timeout paces this loop.
                device._send_receive(None)

    def _poll_sensor(self, device, config: AppConfig) -> bool:
        response = device.status()
        reading = TuyaDevice._parse_status(
            response, datetime.now().astimezone()
        )
        self.on_reading(reading)
        return reading.ok

    # Convert and transmit one bundled complete-state PIU14 frame.
    @staticmethod
    def _send_local_ir(device, key_name: str) -> None:
        from tinytuya.Contrib.IRRemoteControlDevice import (
            IRRemoteControlDevice,
        )

        code = load_local_ir_code(key_name)
        signal = IRRemoteControlDevice.pulses_to_base64(
            IRRemoteControlDevice.hex_to_pulses(code)
        )
        device.send_button(signal)
        time.sleep(0.5)

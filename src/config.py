"""Load and save LocalAIR settings without exposing credentials."""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path


APP_DIR_NAME = "localair"
DEFAULT_IP_ADDRESS = "192.168.1.145"
DEFAULT_DEVICE_ID = "bfc626075b28194ecfhor5"
DEFAULT_PROTOCOL_VERSION = 3.5
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_IR_ADDRESS = 0x08E7
DEFAULT_CLOUD_REGION = "eu"
DEFAULT_SERVER_HOST = "0.0.0.0"
DEFAULT_SERVER_PORT = 8787
DEFAULT_HISTORY_RETENTION_DAYS = 31
AC_STATE_KEYS = {"power", "mode", "temperature", "fan", "swing", "timer"}
DEFAULT_AC_STATE = {"power": "off", "temperature": "24"}
DEFAULT_CLIMATE_START = "10:00"
DEFAULT_CLIMATE_END = "16:00"
DEFAULT_TURN_ON_AT = 24.0
DEFAULT_TURN_OFF_AT = 22.0
DEFAULT_AIRCON_TEMPERATURE = 16
DEFAULT_SENSOR_TIMEOUT_SECONDS = 120


def data_path() -> Path | None:
    configured = os.environ.get("LOCALAIR_DATA")
    return Path(configured).expanduser() if configured else None


def config_path() -> Path:
    configured = os.environ.get("LOCALAIR_CONFIG")
    if configured:
        return Path(configured).expanduser()
    if data := data_path():
        return data / "config.toml"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_DIR_NAME / "config.toml"


def state_path() -> Path:
    if data := data_path():
        return data
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / APP_DIR_NAME


def database_path() -> Path:
    configured = os.environ.get("LOCALAIR_DB")
    if configured:
        return Path(configured).expanduser()
    if data := data_path():
        return data / "localair.db"
    return state_path() / "localair.db"


# Normalize a schedule time and reject values outside the HH:MM format.
def _normalise_time(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError("Schedule times must use HH:MM format.") from None


@dataclass(frozen=True, slots=True)
class AppConfig:
    ip_address: str = DEFAULT_IP_ADDRESS
    device_id: str = DEFAULT_DEVICE_ID
    local_key: str = ""
    protocol_version: float = DEFAULT_PROTOCOL_VERSION
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    ir_address: int = DEFAULT_IR_ADDRESS
    ir_commands: dict[str, int] = field(default_factory=dict)
    ac_state: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_AC_STATE)
    )
    cloud_region: str = DEFAULT_CLOUD_REGION
    cloud_api_key: str = ""
    cloud_api_secret: str = ""
    aircon_device_id: str = ""
    climate_enabled: bool = False
    climate_start: str = DEFAULT_CLIMATE_START
    climate_end: str = DEFAULT_CLIMATE_END
    turn_on_at: float = DEFAULT_TURN_ON_AT
    turn_off_at: float = DEFAULT_TURN_OFF_AT
    aircon_temperature: int = DEFAULT_AIRCON_TEMPERATURE
    sensor_timeout_seconds: int = DEFAULT_SENSOR_TIMEOUT_SECONDS
    server_host: str = DEFAULT_SERVER_HOST
    server_port: int = DEFAULT_SERVER_PORT
    history_retention_days: int = DEFAULT_HISTORY_RETENTION_DAYS

    def validated(self) -> "AppConfig":
        ip = self.ip_address.strip()
        device_id = self.device_id.strip()
        server_host = self.server_host.strip() or DEFAULT_SERVER_HOST
        cloud_region = self.cloud_region.strip().lower()
        climate_start = _normalise_time(self.climate_start)
        climate_end = _normalise_time(self.climate_end)
        if not ip:
            raise ValueError("IP address is required.")
        if not device_id:
            raise ValueError("Device ID is required.")
        if self.protocol_version not in (3.1, 3.2, 3.3, 3.4, 3.5):
            raise ValueError("Protocol version must be between 3.1 and 3.5.")
        if not 5 <= self.poll_interval_seconds <= 3600:
            raise ValueError("Poll interval must be between 5 and 3600 seconds.")
        if not 0 <= self.ir_address <= 0xFFFF:
            raise ValueError("IR address must fit in 16 bits.")
        if any(not 0 <= command <= 0xFF for command in self.ir_commands.values()):
            raise ValueError("IR commands must fit in one byte.")
        if any(key not in AC_STATE_KEYS for key in self.ac_state):
            raise ValueError("Unknown AC state field.")
        if cloud_region not in {
            "cn",
            "us",
            "us-e",
            "eu",
            "eu-w",
            "in",
            "sg",
        }:
            raise ValueError("Unknown Tuya cloud region.")
        if climate_start >= climate_end:
            raise ValueError("Schedule start must be before schedule end.")
        if not -20 <= self.turn_off_at < self.turn_on_at <= 60:
            raise ValueError(
                "Cooling-off temperature must be below cooling-on temperature."
            )
        if not 16 <= self.aircon_temperature <= 30:
            raise ValueError("Air-conditioner temperature must be 16–30°C.")
        if not self.poll_interval_seconds <= self.sensor_timeout_seconds <= 86400:
            raise ValueError(
                "Sensor timeout must be at least the polling interval."
            )
        if not 1 <= self.server_port <= 65535:
            raise ValueError("Server port must be between 1 and 65535.")
        if not 1 <= self.history_retention_days <= 365:
            raise ValueError("History retention must be between 1 and 365 days.")
        return replace(
            self,
            ip_address=ip,
            device_id=device_id,
            server_host=server_host,
            cloud_region=cloud_region,
            cloud_api_key=self.cloud_api_key.strip(),
            cloud_api_secret=self.cloud_api_secret.strip(),
            aircon_device_id=self.aircon_device_id.strip(),
            climate_start=climate_start,
            climate_end=climate_end,
        )


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.exists():
        return AppConfig()

    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
        device = raw.get("device", {})
        ir = raw.get("ir", {})
        server = raw.get("server", {})
        history = raw.get("history", {})
        cloud = raw.get("tuya_cloud", {})
        climate = raw.get("climate", {})
        if not isinstance(device, dict) or not isinstance(ir, dict):
            raise ValueError("Expected [device] and [ir] tables.")
        commands = ir.get("commands", {})
        ac_state = raw.get("ac_state", DEFAULT_AC_STATE)
        if not isinstance(commands, dict):
            raise ValueError("Expected an [ir.commands] table.")
        if not isinstance(ac_state, dict):
            raise ValueError("Expected an [ac_state] table.")
        if not isinstance(server, dict):
            raise ValueError("Expected a [server] table.")
        if not isinstance(history, dict):
            raise ValueError("Expected a [history] table.")
        if not isinstance(cloud, dict):
            raise ValueError("Expected a [tuya_cloud] table.")
        if not isinstance(climate, dict):
            raise ValueError("Expected a [climate] table.")
        poll_interval = int(
            device.get(
                "poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS
            )
        )
        return AppConfig(
            ip_address=str(device.get("ip_address", DEFAULT_IP_ADDRESS)),
            device_id=str(device.get("device_id", DEFAULT_DEVICE_ID)),
            local_key=str(device.get("local_key", "")),
            protocol_version=float(
                device.get("protocol_version", DEFAULT_PROTOCOL_VERSION)
            ),
            poll_interval_seconds=poll_interval,
            ir_address=int(ir.get("address", DEFAULT_IR_ADDRESS)),
            ir_commands={
                str(name): int(command)
                for name, command in commands.items()
                if isinstance(command, int)
            },
            ac_state={
                str(name): str(value)
                for name, value in ac_state.items()
                if str(name) in AC_STATE_KEYS
            },
            cloud_region=str(cloud.get("region", DEFAULT_CLOUD_REGION)),
            cloud_api_key=str(cloud.get("api_key", "")),
            cloud_api_secret=str(cloud.get("api_secret", "")),
            aircon_device_id=str(cloud.get("aircon_device_id", "")),
            climate_enabled=bool(climate.get("enabled", False)),
            climate_start=str(
                climate.get("start_time", DEFAULT_CLIMATE_START)
            ),
            climate_end=str(climate.get("end_time", DEFAULT_CLIMATE_END)),
            turn_on_at=float(climate.get("turn_on_at", DEFAULT_TURN_ON_AT)),
            turn_off_at=float(
                climate.get("turn_off_at", DEFAULT_TURN_OFF_AT)
            ),
            aircon_temperature=int(
                climate.get(
                    "aircon_temperature", DEFAULT_AIRCON_TEMPERATURE
                )
            ),
            sensor_timeout_seconds=int(
                climate.get(
                    "sensor_timeout_seconds",
                    max(DEFAULT_SENSOR_TIMEOUT_SECONDS, poll_interval),
                )
            ),
            server_host=str(server.get("host", DEFAULT_SERVER_HOST)),
            server_port=int(server.get("port", DEFAULT_SERVER_PORT)),
            history_retention_days=int(
                history.get(
                    "retention_days", DEFAULT_HISTORY_RETENTION_DAYS
                )
            ),
        ).validated()
    except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Could not read configuration: {exc}") from None


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    config = config.validated()
    path = path or config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    content = (
        "[device]\n"
        f"ip_address = {_toml_string(config.ip_address)}\n"
        f"device_id = {_toml_string(config.device_id)}\n"
        f"local_key = {_toml_string(config.local_key)}\n"
        f"protocol_version = {config.protocol_version:.1f}\n"
        f"poll_interval_seconds = {config.poll_interval_seconds}\n\n"
        "[tuya_cloud]\n"
        f"region = {_toml_string(config.cloud_region)}\n"
        f"api_key = {_toml_string(config.cloud_api_key)}\n"
        f"api_secret = {_toml_string(config.cloud_api_secret)}\n"
        f"aircon_device_id = {_toml_string(config.aircon_device_id)}\n\n"
        "[climate]\n"
        f"enabled = {'true' if config.climate_enabled else 'false'}\n"
        f"start_time = {_toml_string(config.climate_start)}\n"
        f"end_time = {_toml_string(config.climate_end)}\n"
        f"turn_on_at = {config.turn_on_at}\n"
        f"turn_off_at = {config.turn_off_at}\n"
        f"aircon_temperature = {config.aircon_temperature}\n"
        f"sensor_timeout_seconds = {config.sensor_timeout_seconds}\n\n"
        "[server]\n"
        f"host = {_toml_string(config.server_host)}\n"
        f"port = {config.server_port}\n\n"
        "[history]\n"
        f"retention_days = {config.history_retention_days}\n\n"
        "[ir]\n"
        f"address = 0x{config.ir_address:04X}\n\n"
        "[ir.commands]\n"
        + "".join(
            f"{name} = 0x{command:02X}\n"
            for name, command in sorted(config.ir_commands.items())
        )
        + "\n[ac_state]\n"
        + "".join(
            f"{name} = {_toml_string(value)}\n"
            for name, value in sorted(config.ac_state.items())
        )
    )

    fd, temporary_name = tempfile.mkstemp(
        prefix=".config.", dir=path.parent, text=True
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as config_file:
            config_file.write(content)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temporary_name, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return path

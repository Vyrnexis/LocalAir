"""Load and save LocalAIR settings without exposing credentials."""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path


APP_DIR_NAME = "localair"
DEFAULT_PROTOCOL_VERSION = 3.5
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_SERVER_HOST = "0.0.0.0"
DEFAULT_SERVER_PORT = 8787
DEFAULT_HISTORY_RETENTION_DAYS = 31
AC_STATE_KEYS = {"power", "temperature"}
DEFAULT_AC_STATE = {"power": "off", "temperature": "24"}
DEFAULT_CLIMATE_START = "10:00"
DEFAULT_CLIMATE_END = "16:00"
DEFAULT_TURN_ON_AT = 24.0
DEFAULT_TURN_OFF_AT = 22.0
DEFAULT_AIRCON_TEMPERATURE = 16
DEFAULT_SENSOR_TIMEOUT_SECONDS = 120
MAX_CLIMATE_PERIODS = 4
MAX_PERIOD_NAME_LENGTH = 24
MINUTES_PER_DAY = 24 * 60


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


class ConfigError(ValueError):
    """A validation error tied to the setting it is about."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


# Normalize a schedule time and reject values outside the HH:MM format.
def _normalise_time(value: str, field: str | None = None) -> str:
    try:
        return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ConfigError(
            "Schedule times must use HH:MM format.", field
        ) from None


def _minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


@dataclass(frozen=True, slots=True)
class ClimatePeriod:
    """One daily cooling window with its own thresholds and AC setting.

    An end time earlier than the start time runs past midnight.
    """

    name: str = ""
    enabled: bool = True
    start: str = DEFAULT_CLIMATE_START
    end: str = DEFAULT_CLIMATE_END
    turn_on_at: float = DEFAULT_TURN_ON_AT
    turn_off_at: float = DEFAULT_TURN_OFF_AT
    aircon_temperature: int = DEFAULT_AIRCON_TEMPERATURE

    @property
    def overnight(self) -> bool:
        return self.start > self.end

    # Whether an HH:MM time falls inside the window (end is exclusive).
    def contains(self, clock: str) -> bool:
        if self.overnight:
            return clock >= self.start or clock < self.end
        return self.start <= clock < self.end

    # The window as minute ranges within one day, split at midnight.
    def ranges(self) -> list[tuple[int, int]]:
        start, end = _minutes(self.start), _minutes(self.end)
        if start < end:
            return [(start, end)]
        return [(start, MINUTES_PER_DAY), (0, end)]

    def label(self, index: int) -> str:
        return self.name or f"Period {index + 1}"

    def validated(self, index: int) -> "ClimatePeriod":
        prefix = f"climate_periods.{index}"
        name = " ".join(self.name.split())
        if len(name) > MAX_PERIOD_NAME_LENGTH:
            raise ConfigError(
                f"Period names can be up to {MAX_PERIOD_NAME_LENGTH} "
                "characters.",
                f"{prefix}.name",
            )
        start = _normalise_time(self.start, f"{prefix}.start")
        end = _normalise_time(self.end, f"{prefix}.end")
        label = replace(self, name=name).label(index)
        if start == end:
            raise ConfigError(
                f"{label}: the start and end times must differ.",
                f"{prefix}.end",
            )
        if not -20 <= self.turn_off_at < self.turn_on_at <= 60:
            raise ConfigError(
                f"{label}: the turn-off temperature must be lower than the "
                "turn-on temperature.",
                f"{prefix}.turn_off_at",
            )
        if not 16 <= self.aircon_temperature <= 30:
            raise ConfigError(
                f"{label}: the AC temperature must be 16–30°C.",
                f"{prefix}.aircon_temperature",
            )
        return replace(self, name=name, start=start, end=end)


def _default_periods() -> tuple[ClimatePeriod, ...]:
    return (ClimatePeriod(),)


# Reject enabled periods that share any minute of the day.
def _check_overlaps(periods: tuple[ClimatePeriod, ...]) -> None:
    enabled = [
        (index, period)
        for index, period in enumerate(periods)
        if period.enabled
    ]
    for position, (index, period) in enumerate(enabled):
        for other_index, other in enabled[:position]:
            if any(
                start < other_end and other_start < end
                for start, end in period.ranges()
                for other_start, other_end in other.ranges()
            ):
                raise ConfigError(
                    f"{period.label(index)} overlaps "
                    f"{other.label(other_index)}. Periods can meet but "
                    "not overlap.",
                    f"climate_periods.{index}.start",
                )


@dataclass(frozen=True, slots=True)
class AppConfig:
    ip_address: str = ""
    device_id: str = ""
    local_key: str = ""
    protocol_version: float = DEFAULT_PROTOCOL_VERSION
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    ac_state: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_AC_STATE)
    )
    climate_enabled: bool = False
    climate_periods: tuple[ClimatePeriod, ...] = field(
        default_factory=_default_periods
    )
    sensor_timeout_seconds: int = DEFAULT_SENSOR_TIMEOUT_SECONDS
    server_host: str = DEFAULT_SERVER_HOST
    server_port: int = DEFAULT_SERVER_PORT
    history_retention_days: int = DEFAULT_HISTORY_RETENTION_DAYS

    @property
    def sensor_configured(self) -> bool:
        return bool(self.ip_address and self.device_id and self.local_key)

    # The enabled period whose window contains now, with its index.
    def active_period(
        self, now: datetime
    ) -> tuple[int, ClimatePeriod] | None:
        clock = now.strftime("%H:%M")
        for index, period in enumerate(self.climate_periods):
            if period.enabled and period.contains(clock):
                return index, period
        return None

    # The enabled period that starts soonest after now, with its index.
    def next_period(self, now: datetime) -> tuple[int, ClimatePeriod] | None:
        current = now.hour * 60 + now.minute
        upcoming = [
            ((_minutes(period.start) - current) % MINUTES_PER_DAY, index)
            for index, period in enumerate(self.climate_periods)
            if period.enabled
        ]
        if not upcoming:
            return None
        index = min(upcoming)[1]
        return index, self.climate_periods[index]

    def validated(self) -> "AppConfig":
        ip = self.ip_address.strip()
        device_id = self.device_id.strip()
        server_host = self.server_host.strip() or DEFAULT_SERVER_HOST
        if self.protocol_version not in (3.1, 3.2, 3.3, 3.4, 3.5):
            raise ConfigError(
                "Protocol version must be between 3.1 and 3.5.",
                "protocol_version",
            )
        if not 5 <= self.poll_interval_seconds <= 3600:
            raise ConfigError(
                "Poll interval must be between 5 and 3600 seconds.",
                "poll_interval_seconds",
            )
        if any(key not in AC_STATE_KEYS for key in self.ac_state):
            raise ConfigError("Unknown AC state field.")
        if not 1 <= len(self.climate_periods) <= MAX_CLIMATE_PERIODS:
            raise ConfigError(
                f"Automatic cooling needs 1 to {MAX_CLIMATE_PERIODS} periods.",
                "climate_periods",
            )
        periods = tuple(
            period.validated(index)
            for index, period in enumerate(self.climate_periods)
        )
        _check_overlaps(periods)
        if not self.poll_interval_seconds <= self.sensor_timeout_seconds <= 86400:
            raise ConfigError(
                "Sensor timeout must be at least the polling interval.",
                "sensor_timeout_seconds",
            )
        if not 1 <= self.server_port <= 65535:
            raise ConfigError(
                "Server port must be between 1 and 65535.", "server_port"
            )
        if not 1 <= self.history_retention_days <= 365:
            raise ConfigError(
                "History retention must be between 1 and 365 days.",
                "history_retention_days",
            )
        return replace(
            self,
            ip_address=ip,
            device_id=device_id,
            server_host=server_host,
            climate_periods=periods,
        )


def _load_period(raw: dict, legacy: bool = False) -> ClimatePeriod:
    if not isinstance(raw, dict):
        raise ValueError("Expected [[climate.periods]] tables.")
    return ClimatePeriod(
        name=str(raw.get("name", "")),
        # In the old single-schedule format, "enabled" is the master switch,
        # not the period's own.
        enabled=True if legacy else bool(raw.get("enabled", True)),
        start=str(raw.get("start_time", DEFAULT_CLIMATE_START)),
        end=str(raw.get("end_time", DEFAULT_CLIMATE_END)),
        turn_on_at=float(raw.get("turn_on_at", DEFAULT_TURN_ON_AT)),
        turn_off_at=float(raw.get("turn_off_at", DEFAULT_TURN_OFF_AT)),
        aircon_temperature=int(
            raw.get("aircon_temperature", DEFAULT_AIRCON_TEMPERATURE)
        ),
    )


# Read the period list, or the single schedule older versions saved.
def _load_periods(climate: dict) -> tuple[ClimatePeriod, ...]:
    periods = climate.get("periods")
    if periods is None:
        return (_load_period(climate, legacy=True),)
    if not isinstance(periods, list):
        raise ValueError("Expected [[climate.periods]] tables.")
    return tuple(_load_period(period) for period in periods)


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.exists():
        return AppConfig()

    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
        device = raw.get("device", {})
        server = raw.get("server", {})
        history = raw.get("history", {})
        climate = raw.get("climate", {})
        if not isinstance(device, dict):
            raise ValueError("Expected a [device] table.")
        ac_state = raw.get("ac_state", DEFAULT_AC_STATE)
        if not isinstance(ac_state, dict):
            raise ValueError("Expected an [ac_state] table.")
        if not isinstance(server, dict):
            raise ValueError("Expected a [server] table.")
        if not isinstance(history, dict):
            raise ValueError("Expected a [history] table.")
        if not isinstance(climate, dict):
            raise ValueError("Expected a [climate] table.")
        poll_interval = int(
            device.get(
                "poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS
            )
        )
        return AppConfig(
            ip_address=str(device.get("ip_address", "")),
            device_id=str(device.get("device_id", "")),
            local_key=str(device.get("local_key", "")),
            protocol_version=float(
                device.get("protocol_version", DEFAULT_PROTOCOL_VERSION)
            ),
            poll_interval_seconds=poll_interval,
            ac_state={
                str(name): str(value)
                for name, value in ac_state.items()
                if str(name) in AC_STATE_KEYS
            },
            climate_enabled=bool(climate.get("enabled", False)),
            climate_periods=_load_periods(climate),
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


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


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
        "[climate]\n"
        f"enabled = {_toml_bool(config.climate_enabled)}\n"
        f"sensor_timeout_seconds = {config.sensor_timeout_seconds}\n\n"
        + "".join(
            "[[climate.periods]]\n"
            f"name = {_toml_string(period.name)}\n"
            f"enabled = {_toml_bool(period.enabled)}\n"
            f"start_time = {_toml_string(period.start)}\n"
            f"end_time = {_toml_string(period.end)}\n"
            f"turn_on_at = {period.turn_on_at}\n"
            f"turn_off_at = {period.turn_off_at}\n"
            f"aircon_temperature = {period.aircon_temperature}\n\n"
            for period in config.climate_periods
        )
        + "[server]\n"
        f"host = {_toml_string(config.server_host)}\n"
        f"port = {config.server_port}\n\n"
        "[history]\n"
        f"retention_days = {config.history_retention_days}\n\n"
        "[ac_state]\n"
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

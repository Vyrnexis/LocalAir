"""GTK4 user interface for LocalAir."""

from __future__ import annotations

import math
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from .config import AppConfig, save_config
from .device import (
    CommandResult,
    ConnectionStatus,
    DeviceMonitor,
    DeviceReading,
    IR_COMMANDS,
    TuyaDevice,
    update_ac_state,
)


APP_CSS = b"""
window {
  background: #f4f7fb;
  color: #172033;
}
.content {
  padding: 14px;
}
.card {
  background: #ffffff;
  border: 1px solid #e0e6ef;
  border-radius: 14px;
  padding: 14px;
  box-shadow: 0 4px 14px rgba(28, 39, 60, 0.06);
}
.metric-label {
  color: #667085;
  font-size: 13px;
  font-weight: 600;
}
.temperature-value, .humidity-value {
  font-size: 38px;
  font-weight: 800;
}
.temperature-value { color: #ef6c42; }
.humidity-value { color: #2878d0; }
.metric-hint {
  color: #98a2b3;
  font-size: 12px;
}
.section-title {
  font-size: 16px;
  font-weight: 750;
}
.status-row {
  background: #ffffff;
  border: 1px solid #e0e6ef;
  border-radius: 10px;
  padding: 10px 14px;
}
.status-dot {
  border-radius: 999px;
  min-width: 10px;
  min-height: 10px;
}
.status-connected { background: #23a36d; }
.status-disconnected { background: #98a2b3; }
.status-error { background: #d92d20; }
.status-text { font-weight: 650; }
.error-text { color: #b42318; }
.muted { color: #667085; }
.primary {
  background: #2167d5;
  color: #ffffff;
  border-radius: 9px;
  padding: 8px 16px;
  font-weight: 700;
}
.secondary {
  background: #ffffff;
  color: #344054;
  border: 1px solid #d0d5dd;
  border-radius: 9px;
  padding: 8px 16px;
}
.action-button {
  min-width: 82px;
  min-height: 32px;
}
.legend-line {
  min-width: 28px;
  min-height: 4px;
  border-radius: 3px;
}
.legend-temperature { background: #ef5930; }
.legend-humidity { background: #2170d1; }
.control-group {
  background: #f8fafc;
  border: 1px solid #e4e7ec;
  border-radius: 8px;
  padding: 8px;
}
.control-button {
  min-height: 30px;
}
.ir-success { color: #18794e; }
.history-row {
  padding: 5px 3px;
  border-bottom: 1px solid #eef1f5;
}
.dialog-content {
  padding: 24px;
}
entry, spinbutton {
  min-height: 36px;
}
"""


def install_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(APP_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )


def _label(text: str, *classes: str, xalign: float = 0.0) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign)
    for css_class in classes:
        label.add_css_class(css_class)
    return label


class HistoryGraph(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.history: list[DeviceReading] = []
        self.set_content_height(155)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_history(self, history: list[DeviceReading]) -> None:
        self.history = history
        self.queue_draw()

    def _draw(self, _area, context, width: int, height: int) -> None:
        context.set_source_rgb(1.0, 1.0, 1.0)
        context.paint()

        left, right, top, bottom = 46.0, 18.0, 18.0, 30.0
        graph_width = max(1.0, width - left - right)
        graph_height = max(1.0, height - top - bottom)

        context.set_line_width(1.0)
        context.set_source_rgb(0.90, 0.92, 0.95)
        for step in range(5):
            y = top + graph_height * step / 4
            context.move_to(left, y)
            context.line_to(left + graph_width, y)
            context.stroke()

        values = [
            reading
            for reading in self.history[-30:]
            if reading.temperature_c is not None
            and reading.humidity_percent is not None
        ]
        if len(values) < 2:
            context.set_source_rgb(0.40, 0.44, 0.51)
            context.select_font_face("Sans", 0, 0)
            context.set_font_size(13)
            message = "History graph will appear after two readings"
            extents = context.text_extents(message)
            context.move_to(
                left + (graph_width - extents.width) / 2,
                top + graph_height / 2,
            )
            context.show_text(message)
            return

        temperatures = [reading.temperature_c for reading in values]
        minimum = math.floor(min(temperatures) - 1)
        maximum = math.ceil(max(temperatures) + 1)
        if maximum - minimum < 4:
            midpoint = (maximum + minimum) / 2
            minimum, maximum = midpoint - 2, midpoint + 2

        self._draw_series(
            context,
            [float(reading.temperature_c) for reading in values],
            minimum,
            maximum,
            left,
            top,
            graph_width,
            graph_height,
            (0.94, 0.35, 0.18),
        )
        self._draw_series(
            context,
            [float(reading.humidity_percent) for reading in values],
            0,
            100,
            left,
            top,
            graph_width,
            graph_height,
            (0.13, 0.44, 0.82),
            dashed=True,
        )

        context.set_source_rgb(0.40, 0.44, 0.51)
        context.set_font_size(11)
        context.move_to(6, top + 5)
        context.show_text(f"{maximum:g}°")
        context.move_to(6, top + graph_height)
        context.show_text(f"{minimum:g}°")
        context.move_to(left, height - 8)
        context.show_text(values[0].timestamp.strftime("%H:%M"))
        end_text = values[-1].timestamp.strftime("%H:%M")
        extents = context.text_extents(end_text)
        context.move_to(left + graph_width - extents.width, height - 8)
        context.show_text(end_text)

    @staticmethod
    def _draw_series(
        context,
        values,
        minimum,
        maximum,
        left,
        top,
        width,
        height,
        color,
        dashed=False,
    ) -> None:
        context.set_source_rgb(*color)
        context.set_line_width(3.6)
        context.set_dash([6.0, 4.0] if dashed else [])
        for index, value in enumerate(values):
            x = left + width * index / (len(values) - 1)
            normalized = (value - minimum) / (maximum - minimum)
            y = top + height * (1.0 - normalized)
            if index == 0:
                context.move_to(x, y)
            else:
                context.line_to(x, y)
        context.stroke()
        context.set_dash([])


class SettingsWindow(Gtk.Window):
    def __init__(self, parent: "LocalAirWindow", config: AppConfig) -> None:
        super().__init__(
            title="LocalAir Settings",
            transient_for=parent,
            modal=True,
            default_width=520,
            resizable=False,
        )
        self.parent_window = parent

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        root.add_css_class("dialog-content")
        self.set_child(root)

        root.append(_label("Device settings", "section-title"))
        root.append(
            _label(
                "Credentials stay on this computer and are never logged.",
                "muted",
            )
        )

        grid = Gtk.Grid(column_spacing=16, row_spacing=12)
        grid.set_column_homogeneous(False)
        root.append(grid)

        self.ip_entry = self._add_entry(
            grid, 0, "IP address", config.ip_address
        )
        self.device_id_entry = self._add_entry(
            grid, 1, "Device ID", config.device_id
        )
        self.key_entry = self._add_entry(
            grid, 2, "Local key", config.local_key, password=True
        )
        self.version_entry = self._add_entry(
            grid, 3, "Protocol version", f"{config.protocol_version:.1f}"
        )

        interval_label = _label("Poll interval (seconds)")
        grid.attach(interval_label, 0, 4, 1, 1)
        adjustment = Gtk.Adjustment(
            value=config.poll_interval_seconds,
            lower=5,
            upper=3600,
            step_increment=5,
            page_increment=30,
        )
        self.interval_spin = Gtk.SpinButton(adjustment=adjustment, numeric=True)
        self.interval_spin.set_hexpand(True)
        grid.attach(self.interval_spin, 1, 4, 1, 1)

        self.error_label = _label("", "error-text")
        self.error_label.set_wrap(True)
        self.error_label.set_visible(False)
        root.append(self.error_label)

        buttons = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            halign=Gtk.Align.END,
        )
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("secondary")
        cancel.connect("clicked", lambda _button: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("primary")
        save.connect("clicked", self._on_save)
        buttons.append(cancel)
        buttons.append(save)
        root.append(buttons)

    @staticmethod
    def _add_entry(
        grid: Gtk.Grid,
        row: int,
        label_text: str,
        value: str,
        password: bool = False,
    ) -> Gtk.Entry:
        grid.attach(_label(label_text), 0, row, 1, 1)
        entry = Gtk.Entry()
        entry.set_text(value)
        entry.set_hexpand(True)
        entry.set_visibility(not password)
        if password:
            entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
            entry.set_placeholder_text("Required for local device access")
        grid.attach(entry, 1, row, 1, 1)
        return entry

    def _on_save(self, _button: Gtk.Button) -> None:
        try:
            config = AppConfig(
                ip_address=self.ip_entry.get_text(),
                device_id=self.device_id_entry.get_text(),
                local_key=self.key_entry.get_text(),
                protocol_version=float(self.version_entry.get_text()),
                poll_interval_seconds=self.interval_spin.get_value_as_int(),
                ir_address=self.parent_window.config.ir_address,
                ir_commands=self.parent_window.config.ir_commands,
                ac_state=self.parent_window.config.ac_state,
                server_host=self.parent_window.config.server_host,
                server_port=self.parent_window.config.server_port,
                history_retention_days=(
                    self.parent_window.config.history_retention_days
                ),
            ).validated()
            save_config(config)
        except (ValueError, OSError) as exc:
            self.error_label.set_text(str(exc))
            self.error_label.set_visible(True)
            return

        self.parent_window.apply_config(config)
        self.close()


class LocalAirWindow(Gtk.ApplicationWindow):
    MAX_HISTORY = 200

    def __init__(
        self,
        application: Gtk.Application,
        config: AppConfig,
        device: TuyaDevice,
        startup_error: str | None = None,
    ) -> None:
        super().__init__(
            application=application,
            title="LocalAir",
            default_width=980,
            default_height=720,
        )
        self.config = config
        self.device = device
        self.history: list[DeviceReading] = []
        self.refresh_in_progress = False
        self.ir_in_progress = False
        self.ir_buttons: dict[str, Gtk.Button] = {}

        self.set_size_request(680, 560)
        self.connect("close-request", self._on_close_request)
        self.set_child(self._build_layout())
        self.monitor = DeviceMonitor(
            config,
            lambda reading: GLib.idle_add(self._apply_reading, reading),
            lambda command: GLib.idle_add(self._apply_observed_ir, command),
            lambda result: GLib.idle_add(self._apply_ir_result, result),
        )
        self.monitor.start()

        if startup_error:
            self._show_startup_error(startup_error)
        else:
            self.refresh()

    def _build_layout(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        content.add_css_class("content")
        scroller.set_child(content)
        root.append(scroller)

        # Connection state is intentionally first so a device/network problem
        # is visible before any readings or controls.
        content.append(self._build_status_row())

        dashboard = Gtk.Grid(
            column_spacing=12,
            row_spacing=12,
            column_homogeneous=True,
        )
        temperature_card, self.temperature_value = self._metric_card(
            "Temperature", "--.-°C", "DP 1 · local sensor", "temperature-value"
        )
        humidity_card, self.humidity_value = self._metric_card(
            "Humidity", "--%", "DP 2 · relative humidity", "humidity-value"
        )
        dashboard.attach(temperature_card, 0, 0, 1, 1)
        dashboard.attach(humidity_card, 1, 0, 1, 1)

        # Controls stay beneath temperature on the left. Recent timestamps
        # stay beneath humidity on the right.
        controls = self._build_ac_controls()
        controls.set_vexpand(True)

        right_column = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            vexpand=True,
        )
        readings = self._build_history_list()
        readings.set_vexpand(True)
        right_column.append(readings)
        right_column.append(self._build_actions_card())

        dashboard.attach(controls, 0, 1, 1, 1)
        dashboard.attach(right_column, 1, 1, 1, 1)
        content.append(dashboard)

        graph_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        graph_card.add_css_class("card")
        graph_heading = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        graph_title = _label("Session history", "section-title")
        graph_title.set_hexpand(True)
        graph_heading.append(graph_title)
        graph_heading.append(
            self._legend_item(
                "Temperature", "legend-temperature"
            )
        )
        graph_heading.append(
            self._legend_item("Humidity", "legend-humidity")
        )
        graph_card.append(graph_heading)
        self.history_graph = HistoryGraph()
        graph_card.append(self.history_graph)
        content.append(graph_card)

        return root

    @staticmethod
    def _metric_card(
        title: str, value: str, hint: str, value_class: str
    ) -> tuple[Gtk.Box, Gtk.Label]:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        card.add_css_class("card")
        card.append(_label(title, "metric-label"))
        value_label = _label(value, value_class)
        card.append(value_label)
        card.append(_label(hint, "metric-hint"))
        return card, value_label

    def _build_status_row(self) -> Gtk.Widget:
        row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        row.add_css_class("status-row")
        self.status_dot = Gtk.Box()
        self.status_dot.add_css_class("status-dot")
        self.status_dot.add_css_class("status-disconnected")
        row.append(self.status_dot)
        self.status_label = _label("Disconnected", "status-text")
        row.append(self.status_label)
        self.connection_detail = _label(
            f"Waiting to contact {self.config.ip_address}", "muted"
        )
        self.connection_detail.set_ellipsize(Pango.EllipsizeMode.END)
        self.connection_detail.set_hexpand(True)
        row.append(self.connection_detail)
        self.last_updated_label = _label("Last updated —", "muted", xalign=1.0)
        row.append(self.last_updated_label)
        return row

    def _build_history_list(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.append(_label("Recent readings", "section-title"))
        self.history_list = Gtk.ListBox()
        self.history_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.empty_history_label = _label(
            "No readings yet", "muted", xalign=0.5
        )
        self.empty_history_label.set_margin_top(18)
        self.empty_history_label.set_margin_bottom(18)
        self.history_list.append(self.empty_history_label)
        card.append(self.history_list)
        return card

    def _build_actions_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        card.add_css_class("card")
        card.append(_label("Application", "section-title"))
        card.append(_label("Device configuration and polling", "muted"))

        buttons = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            homogeneous=True,
        )
        settings_button = Gtk.Button(label="Settings")
        settings_button.add_css_class("action-button")
        settings_button.connect("clicked", self._open_settings)

        self.refresh_button = Gtk.Button(label="Refresh")
        self.refresh_button.add_css_class("action-button")
        self.refresh_button.connect("clicked", lambda _button: self.refresh())

        buttons.append(settings_button)
        buttons.append(self.refresh_button)
        card.append(buttons)
        return card

    @staticmethod
    def _legend_item(text: str, color_class: str) -> Gtk.Widget:
        item = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=7,
            valign=Gtk.Align.CENTER,
        )
        swatch = Gtk.Box(valign=Gtk.Align.CENTER)
        swatch.add_css_class("legend-line")
        swatch.add_css_class(color_class)
        item.append(swatch)
        item.append(_label(text, "metric-hint"))
        return item

    def _build_ac_controls(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        card.add_css_class("card")
        card.append(_label("Air conditioner", "section-title"))
        card.append(_label("Listening for LocalAir, Sensibo and remote", "muted"))
        self.ac_state_label = _label("", "metric-hint")
        self.ac_state_label.set_wrap(True)
        card.append(self.ac_state_label)
        self._update_ac_state_label()

        power = self._ir_button("Power", "power")
        power.add_css_class("primary")
        card.append(power)

        card.append(
            self._control_group(
                "Temperature",
                (("−", "temp_down"), ("+", "temp_up")),
            )
        )
        card.append(
            self._control_group(
                "Mode",
                (
                    ("Heat", "mode_heat"),
                    ("Cool", "mode_cool"),
                    ("Dry", "mode_dry"),
                    ("Fan", "mode_fan"),
                ),
            )
        )
        card.append(
            self._control_group(
                "Fan speed",
                (
                    ("Low", "fan_low"),
                    ("Mid", "fan_mid"),
                    ("High", "fan_high"),
                ),
            )
        )
        card.append(
            self._control_group(
                "Other",
                (
                    ("Swing", "swing"),
                    ("Timer", "timer"),
                    ("C/F", "unit_cf"),
                ),
            )
        )
        card.append(self._sync_group())

        self.ir_status_label = _label(
            "Ready · controls transmit the learned remote signal.",
            "muted",
        )
        self.ir_status_label.set_wrap(True)
        card.append(self.ir_status_label)
        self._update_ir_button_availability()
        return card

    def _control_group(
        self,
        title: str,
        controls: tuple[tuple[str, str], ...],
    ) -> Gtk.Widget:
        group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        group.add_css_class("control-group")
        group.append(_label(title, "metric-label"))
        row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            homogeneous=True,
        )
        for label, command in controls:
            row.append(self._ir_button(label, command))
        group.append(row)
        return group

    def _sync_group(self) -> Gtk.Widget:
        group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        group.add_css_class("control-group")
        group.append(_label("Sync", "metric-label"))
        group.append(
            _label(
                "Corrects LocalAir power state without sending IR.",
                "metric-hint",
            )
        )
        button = Gtk.Button(label="Sync Power")
        button.add_css_class("control-button")
        button.set_tooltip_text("Toggle LocalAir's saved power state only")
        button.connect("clicked", lambda _button: self._sync_power())
        group.append(button)
        return group

    def _ir_button(self, label: str, command: str) -> Gtk.Button:
        button = Gtk.Button(label=label, hexpand=True)
        button.add_css_class("control-button")
        button.connect(
            "clicked",
            lambda _button, selected=command: self._send_ir(selected),
        )
        self.ir_buttons[command] = button
        return button

    def _update_ir_button_availability(self) -> None:
        available = IR_COMMANDS.intersection(self.config.ir_commands)
        for command, button in self.ir_buttons.items():
            learned = command in available
            button.set_sensitive(learned and not self.ir_in_progress)
            button.set_tooltip_text(
                "Send learned IR command"
                if learned
                else "This command has not been learned yet."
            )

    def _open_settings(self, _button: Gtk.Button) -> None:
        SettingsWindow(self, self.config).present()

    def apply_config(self, config: AppConfig) -> None:
        self.config = config
        self.connection_detail.set_text(f"Configured for {config.ip_address}")
        self._update_ir_button_availability()
        self.monitor.update_config(config)
        self.refresh()

    def _send_ir(self, command: str) -> None:
        if self.ir_in_progress:
            return
        self.ir_in_progress = True
        self._update_ir_button_availability()
        self.ir_status_label.remove_css_class("error-text")
        self.ir_status_label.remove_css_class("ir-success")
        self.ir_status_label.set_text("Sending IR command…")
        self.monitor.send(command)

    def _sync_power(self) -> None:
        current = self.config.ac_state.get("power")
        next_power = "off" if current == "on" else "on"
        self._set_ac_state("Synced power state", power=next_power)

    def _set_ac_state(self, status: str, **changes: str) -> None:
        state = dict(self.config.ac_state)
        state.update(changes)
        self._save_ac_state(state, status)

    def _save_ac_state(self, state: dict[str, str], status: str) -> None:
        self.config = AppConfig(
            ip_address=self.config.ip_address,
            device_id=self.config.device_id,
            local_key=self.config.local_key,
            protocol_version=self.config.protocol_version,
            poll_interval_seconds=self.config.poll_interval_seconds,
            ir_address=self.config.ir_address,
            ir_commands=self.config.ir_commands,
            ac_state=state,
            server_host=self.config.server_host,
            server_port=self.config.server_port,
            history_retention_days=self.config.history_retention_days,
        )
        try:
            save_config(self.config)
        except OSError:
            self.ir_status_label.set_text("State changed but could not be saved.")
        else:
            self.ir_status_label.remove_css_class("error-text")
            self.ir_status_label.add_css_class("ir-success")
            self.ir_status_label.set_text(
                f"{status} · {datetime.now().strftime('%H:%M:%S')}"
            )
        self._update_ac_state_label()
        self._force_redraw()

    def _apply_ir_result(self, result: CommandResult) -> bool:
        self.ir_in_progress = False
        self._update_ir_button_availability()
        self.ir_status_label.remove_css_class("error-text")
        self.ir_status_label.remove_css_class("ir-success")
        if result.ok:
            self.ir_status_label.add_css_class("ir-success")
            self.ir_status_label.set_text(
                f"{result.message} · {datetime.now().strftime('%H:%M:%S')}"
            )
        else:
            self.ir_status_label.add_css_class("error-text")
            self.ir_status_label.set_text(result.message)
        self._force_redraw()
        return GLib.SOURCE_REMOVE

    def refresh(self) -> None:
        if self.refresh_in_progress:
            return
        self.refresh_in_progress = True
        self.connection_detail.set_text(
            f"Contacting {self.config.ip_address}…"
        )
        self._force_redraw()
        self.monitor.refresh()

    def _apply_reading(self, reading: DeviceReading) -> bool:
        self.refresh_in_progress = False

        for css_class in (
            "status-connected",
            "status-disconnected",
            "status-error",
        ):
            self.status_dot.remove_css_class(css_class)

        self.status_label.set_text(reading.connection_status.value)
        status_class = {
            ConnectionStatus.CONNECTED: "status-connected",
            ConnectionStatus.DISCONNECTED: "status-disconnected",
            ConnectionStatus.ERROR: "status-error",
        }[reading.connection_status]
        self.status_dot.add_css_class(status_class)

        if reading.ok:
            self.temperature_value.set_text(f"{reading.temperature_c:.1f}°C")
            self.humidity_value.set_text(f"{reading.humidity_percent}%")
            self.connection_detail.set_text(
                f"Connected to {self.config.ip_address}"
            )
            self.last_updated_label.set_text(
                f"Last updated {reading.timestamp.strftime('%H:%M:%S')}"
            )
            self.history.append(reading)
            if len(self.history) > self.MAX_HISTORY:
                self.history = self.history[-self.MAX_HISTORY :]
            self._update_history()
        else:
            self.connection_detail.set_text(
                reading.error_message or f"Could not reach {self.config.ip_address}"
            )
        self._force_redraw()
        return GLib.SOURCE_REMOVE

    def _apply_observed_ir(self, command: str) -> bool:
        self._save_ac_state(
            update_ac_state(self.config.ac_state, command),
            f"Observed {command.replace('_', ' ')}",
        )
        return GLib.SOURCE_REMOVE

    def _update_ac_state_label(self) -> None:
        state = self.config.ac_state
        fields = (
            ("Power", state.get("power", "unknown")),
            ("Mode", state.get("mode", "unknown")),
            ("Set", state.get("temperature", "unknown")),
            ("Fan", state.get("fan", "unknown")),
            ("Swing", state.get("swing", "unknown")),
        )
        text = " · ".join(f"{name}: {value.title()}" for name, value in fields)
        self.ac_state_label.set_text(f"Assumed state · {text}")

    def _force_redraw(self) -> None:
        """Invalidate the full window after background-driven label changes."""
        self.queue_draw()

    def _update_history(self) -> None:
        child = self.history_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.history_list.remove(child)
            child = next_child

        for reading in reversed(self.history[-3:]):
            row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=12,
            )
            row.add_css_class("history-row")
            timestamp = _label(reading.timestamp.strftime("%H:%M:%S"), "muted")
            timestamp.set_hexpand(True)
            row.append(timestamp)
            row.append(_label(f"{reading.temperature_c:.1f}°C"))
            row.append(_label(f"{reading.humidity_percent}%"))
            self.history_list.append(row)
        self.history_graph.set_history(self.history)

    def _show_startup_error(self, message: str) -> None:
        reading = DeviceReading(
            temperature_c=None,
            humidity_percent=None,
            timestamp=datetime.now().astimezone(),
            connection_status=ConnectionStatus.ERROR,
            error_message=message,
        )
        self._apply_reading(reading)

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self.monitor.stop()
        return False

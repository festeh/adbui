"""ADB TUI - A terminal UI for managing Android devices."""

from collections import deque
from dataclasses import dataclass
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, LoadingIndicator, RichLog, Static
from textual.screen import ModalScreen

import adb
from adb import DeviceState, set_log_callback
from mdns import ADBDiscovery, DiscoveredDevice, set_mdns_log_callback


@dataclass
class UnifiedDevice:
    """Merged device info from ADB and mDNS."""
    name: str  # From mDNS device_name or ADB model
    address: str  # IP:port for connect
    serial: str  # ADB serial (for disconnect)
    api_level: int
    paired: bool  # Seen via _adb-tls-connect._tcp
    pairing: bool  # Currently in pairing mode
    connected: bool  # ADB state is "device"
    adb_state: str  # Raw ADB state


def merge_devices(
    adb_devices: list[adb.Device],
    mdns_devices: list[DiscoveredDevice],
) -> list[UnifiedDevice]:
    """Merge ADB and mDNS device lists into unified view."""
    unified: dict[str, UnifiedDevice] = {}

    # First, process mDNS devices (they have richer info)
    for md in mdns_devices:
        # Extract device ID from instance name (e.g., "adb-RFAW70DK06L-IqBWav" -> "RFAW70DK06L")
        parts = md.instance_name.split("-")
        device_id = parts[1] if len(parts) > 1 else md.instance_name

        key = device_id

        if key in unified:
            # Update existing entry
            dev = unified[key]
            if md.device_name:
                dev.name = md.device_name
            if md.api_level:
                dev.api_level = md.api_level
            if md.is_pairing:
                dev.pairing = True
                dev.address = md.address  # Use pairing address when pairing
            else:
                if not dev.address:
                    dev.address = md.address
        else:
            unified[key] = UnifiedDevice(
                name=md.device_name or md.instance_name,
                address=md.address,
                serial="",
                api_level=md.api_level,
                paired=False,  # mDNS discovery alone doesn't mean paired
                pairing=md.is_pairing,
                connected=False,
                adb_state="",
            )

    # Then, merge ADB devices
    for ad in adb_devices:
        # Try to find matching mDNS device by ID in serial
        matched = False
        for key in unified:
            if key in ad.serial:
                dev = unified[key]
                dev.serial = ad.serial
                dev.connected = ad.state == DeviceState.DEVICE
                dev.adb_state = ad.state.value
                # Only mark as paired if ADB can actually connect
                dev.paired = ad.state == DeviceState.DEVICE
                if ad.model:
                    dev.name = ad.model
                matched = True
                break

        if not matched:
            # ADB device without mDNS match (USB or already connected by IP)
            key = ad.serial
            unified[key] = UnifiedDevice(
                name=ad.model or ad.serial,
                address=ad.serial if ":" in ad.serial else "",
                serial=ad.serial,
                api_level=0,
                paired=ad.state == DeviceState.DEVICE,
                pairing=False,
                connected=ad.state == DeviceState.DEVICE,
                adb_state=ad.state.value,
            )

    return list(unified.values())


def get_status(dev: UnifiedDevice) -> tuple[str, str]:
    """Get status strings based on device state.

    Returns (paired_status, connection_status).
    States are mutually exclusive - no guessing or fallbacks.
    """
    # Pairing mode: device is broadcasting pairing service
    if dev.pairing:
        return ("Pairing", "Enter code")

    # Connected: ADB reports device is ready
    if dev.connected:
        return ("Paired", "Connected")

    # ADB states (device known to ADB but not fully connected)
    if dev.adb_state == "offline":
        return ("Paired", "Offline")
    if dev.adb_state == "unauthorized":
        return ("Paired", "Unauthorized")

    # Discovered via mDNS connect service (previously paired)
    if dev.address:
        return ("Paired", "Disconnected")

    # Fallback (shouldn't happen with current merge logic)
    return ("Unknown", "Unknown")


class PairScreen(ModalScreen[bool]):
    """Modal screen for entering pairing code."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, address: str, device_name: str, **kwargs):
        super().__init__(**kwargs)
        self._address = address
        self._device_name = device_name

    def compose(self) -> ComposeResult:
        with Container(id="pair-dialog"):
            title = f"Pair {self._device_name}" if self._device_name else "Pair Device"
            yield Label(title, id="pair-title")
            yield Label("Pairing Code:")
            yield Input(placeholder="123456", id="pair-code")
            with Horizontal(id="pair-buttons"):
                yield Button("Pair", variant="primary", id="btn-pair")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#pair-code", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def _do_pair(self) -> None:
        code = self.query_one("#pair-code", Input).value
        if code:
            success, msg = await adb.pair(self._address, code)
            self.app.notify(msg, severity="information" if success else "error", markup=False)
            self.dismiss(success)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._do_pair()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(False)
        elif event.button.id == "btn-pair":
            await self._do_pair()


class ConnectScreen(ModalScreen[bool]):
    """Modal screen for connecting to a device."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, address: str = "", **kwargs):
        super().__init__(**kwargs)
        self._address = address

    def compose(self) -> ComposeResult:
        with Container(id="connect-dialog"):
            yield Label("Connect to Device", id="connect-title")
            yield Label("Address (e.g., 192.168.1.100:5555):")
            yield Input(placeholder="192.168.1.100:5555", value=self._address, id="connect-address")
            with Horizontal(id="connect-buttons"):
                yield Button("Connect", variant="primary", id="btn-connect")
                yield Button("Cancel", id="btn-cancel")

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def _do_connect(self) -> None:
        address = self.query_one("#connect-address", Input).value
        if address:
            success, msg = await adb.connect(address)
            self.app.notify(msg, severity="information" if success else "error", markup=False)
            self.dismiss(success)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._do_connect()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(False)
        elif event.button.id == "btn-connect":
            await self._do_connect()


class AdbUI(App):
    """Main ADB TUI application."""

    CSS = """
    Screen {
        background: $surface;
    }

    .section-label {
        margin: 1 2 0 2;
        text-style: bold;
        color: $text-muted;
    }

    #device-table {
        height: 1fr;
        margin: 0 2;
        width: 100%;
    }

    #log-panel {
        height: 12;
        margin: 0 2;
        border: solid $primary;
        display: none;
    }

    #log-panel.visible {
        display: block;
    }

    #loading {
        height: 1;
        width: 100%;
        margin: 0 2;
        display: none;
    }

    #loading.visible {
        display: block;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }

    #pair-dialog, #connect-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: solid $primary;
    }

    #pair-title, #connect-title {
        text-style: bold;
        width: 100%;
        content-align: center middle;
        margin-bottom: 1;
    }

    #pair-dialog Input, #connect-dialog Input {
        margin-bottom: 1;
    }

    #pair-buttons, #connect-buttons {
        width: 100%;
        height: auto;
        align: center middle;
        margin-top: 1;
    }

    #pair-buttons Button, #connect-buttons Button {
        margin: 0 1;
    }

    DataTable:focus {
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("c", "connect", "Connect"),
        Binding("p", "pair", "Pair"),
        Binding("d", "disconnect", "Disconnect"),
        Binding("K", "restart_server", "Restart Server"),
        Binding("l", "toggle_logs", "Logs"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "select_device", "Select", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._discovery = ADBDiscovery(on_update=self._on_discovery_update)
        self._devices: list[UnifiedDevice] = []
        self._log_visible = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Devices", classes="section-label")
        yield DataTable(id="device-table")
        yield RichLog(id="log-panel", highlight=True, markup=True, auto_scroll=True)
        yield LoadingIndicator(id="loading")
        yield Static("Loading...", id="status-bar")
        yield Footer()

    def _log(self, msg: str) -> None:
        """Add message to log panel."""
        try:
            log = self.query_one("#log-panel", RichLog)
            log.write(msg)
        except Exception:
            pass

    def on_mount(self) -> None:
        # Setup logging
        set_log_callback(self._log)
        set_mdns_log_callback(self._log)

        table = self.query_one("#device-table", DataTable)
        table.add_column("Name")  # Auto-expand
        table.add_column("Address", width=22)
        table.add_column("API", width=5)
        table.add_column("Paired", width=12)
        table.add_column("Status", width=14)
        table.cursor_type = "row"

        self._discovery.start()
        self.refresh_devices()

    def on_unmount(self) -> None:
        set_log_callback(None)
        set_mdns_log_callback(None)
        self._discovery.stop()

    def _on_discovery_update(self) -> None:
        """Called when mDNS discovers/loses devices."""
        self.call_from_thread(self.refresh_devices)

    def _get_selected_device(self) -> UnifiedDevice | None:
        """Get currently selected device."""
        table = self.query_one("#device-table", DataTable)
        if table.row_count == 0:
            return None
        row_idx = table.cursor_coordinate.row
        if 0 <= row_idx < len(self._devices):
            return self._devices[row_idx]
        return None

    @work(exclusive=True)
    async def refresh_devices(self) -> None:
        """Refresh the device list."""
        table = self.query_one("#device-table", DataTable)
        status = self.query_one("#status-bar", Static)
        loading = self.query_one("#loading", LoadingIndicator)

        loading.add_class("visible")
        status.update("Scanning...")

        adb_devices = await adb.get_devices()
        mdns_devices = self._discovery.devices

        self._devices = merge_devices(adb_devices, mdns_devices)

        table.clear()
        for dev in self._devices:
            paired, connected = get_status(dev)
            table.add_row(
                dev.name,
                dev.address or "-",
                str(dev.api_level) if dev.api_level else "-",
                paired,
                connected,
            )

        loading.remove_class("visible")
        connected = sum(1 for d in self._devices if d.connected)
        status.update(f"{len(self._devices)} device(s), {connected} connected")

    def action_refresh(self) -> None:
        self._discovery.clear()  # Clear stale mDNS cache
        self.refresh_devices()

    def action_toggle_logs(self) -> None:
        """Toggle log panel visibility."""
        log_panel = self.query_one("#log-panel", RichLog)
        self._log_visible = not self._log_visible
        if self._log_visible:
            log_panel.add_class("visible")
        else:
            log_panel.remove_class("visible")

    async def action_connect(self) -> None:
        dev = self._get_selected_device()

        if dev and dev.address:
            # Connect directly if device selected
            success, msg = await adb.connect(dev.address)
            self.notify(msg, severity="information" if success else "error", markup=False)
            if success:
                self.refresh_devices()
        else:
            # Show dialog for manual entry
            def on_dismiss(success: bool) -> None:
                if success:
                    self.refresh_devices()
            self.push_screen(ConnectScreen(), on_dismiss)

    def action_pair(self) -> None:
        dev = self._get_selected_device()
        if not dev:
            self.notify("No device selected", severity="warning")
            return
        if not dev.pairing or not dev.address:
            self.notify("Device not in pairing mode", severity="warning")
            return

        def on_dismiss(success: bool) -> None:
            if success:
                self.refresh_devices()

        self.push_screen(PairScreen(address=dev.address, device_name=dev.name), on_dismiss)

    async def action_disconnect(self) -> None:
        dev = self._get_selected_device()
        if not dev:
            self.notify("No device selected", severity="warning")
            return

        if not dev.serial:
            self.notify("Device not connected via ADB", severity="warning")
            return

        success, msg = await adb.disconnect(dev.serial)
        self.notify(msg, severity="information" if success else "error", markup=False)
        self.refresh_devices()

    async def action_restart_server(self) -> None:
        await adb.kill_server()
        success, msg = await adb.start_server()
        self.notify("Server restarted" if success else msg, severity="information" if success else "error", markup=False)
        self.refresh_devices()

    def action_cursor_down(self) -> None:
        table = self.query_one("#device-table", DataTable)
        table.action_cursor_down()

    def action_cursor_up(self) -> None:
        table = self.query_one("#device-table", DataTable)
        table.action_cursor_up()

    async def action_select_device(self) -> None:
        """Handle Enter key on selected device."""
        dev = self._get_selected_device()
        if not dev:
            return

        if dev.pairing:
            self.action_pair()
        elif dev.paired and not dev.connected and dev.address:
            success, msg = await adb.connect(dev.address)
            self.notify(msg, severity="information" if success else "error", markup=False)
            if success:
                self.refresh_devices()
        elif not dev.paired:
            self.notify("Device needs pairing first", severity="warning")


def main():
    app = AdbUI()
    app.run()


if __name__ == "__main__":
    main()

"""ADB TUI - A terminal UI for managing Android devices."""

from dataclasses import dataclass, field
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, LoadingIndicator, RichLog, Static
from textual.screen import ModalScreen

import adb
from adb import set_log_callback
from log import setup_logging
from mdns import ADBDiscovery, DiscoveredDevice, set_mdns_log_callback


@dataclass
class Device:
    """Merged device info from ADB and mDNS. Raw observations only."""
    name: str
    address: str  # IP:port display address (highest port heuristic)
    pairing_address: str  # IP:port from mDNS pairing service
    serial: str  # ADB serial
    adb_state: str  # "device", "offline", "unauthorized", ""
    api_level: int
    connect_addresses: list[str] = field(default_factory=list)  # all known connect addresses


def _pick_address(addresses: list[str]) -> str:
    """Pick display address from candidates: prefer highest port."""
    if not addresses:
        return ""

    def port_of(addr: str) -> int:
        try:
            return int(addr.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            return 0

    return max(addresses, key=port_of)


def merge_devices(
    adb_devices: list[adb.Device],
    mdns_devices: list[DiscoveredDevice],
) -> list[Device]:
    """Merge ADB and mDNS device lists into unified view."""
    unified: dict[str, Device] = {}
    address_to_key: dict[str, str] = {}

    # First, process mDNS devices (they have richer info)
    for md in mdns_devices:
        parts = md.instance_name.split("-")
        key = parts[1] if len(parts) > 1 else md.instance_name

        if key in unified:
            dev = unified[key]
            if md.device_name:
                dev.name = md.device_name
            if md.api_level:
                dev.api_level = md.api_level
            if md.is_pairing:
                dev.pairing_address = md.address
            elif md.address and md.address not in dev.connect_addresses:
                dev.connect_addresses.append(md.address)
        else:
            connect_addrs = [md.address] if (not md.is_pairing and md.address) else []
            unified[key] = Device(
                name=md.device_name or md.instance_name,
                address="",
                pairing_address=md.address if md.is_pairing else "",
                serial="",
                adb_state="",
                api_level=md.api_level,
                connect_addresses=connect_addrs,
            )

        if md.address:
            address_to_key[md.address] = key

    # Then, merge ADB devices
    for ad in adb_devices:
        matched_key = None
        for key in unified:
            if key in ad.serial:
                matched_key = key
                break
        if not matched_key and ad.serial in address_to_key:
            matched_key = address_to_key[ad.serial]

        if matched_key:
            dev = unified[matched_key]
            dev.serial = ad.serial
            dev.adb_state = ad.state.value
            if ad.model:
                dev.name = ad.model
            if ":" in ad.serial and "._adb" not in ad.serial:
                if ad.serial not in dev.connect_addresses:
                    dev.connect_addresses.append(ad.serial)
        else:
            addr = ad.serial if ":" in ad.serial else ""
            unified[ad.serial] = Device(
                name=ad.model or ad.serial,
                address="",
                pairing_address="",
                serial=ad.serial,
                adb_state=ad.state.value,
                api_level=0,
                connect_addresses=[addr] if addr else [],
            )

    # Set display address: highest port heuristic
    result = list(unified.values())
    for dev in result:
        dev.address = _pick_address(dev.connect_addresses)
    return result


def get_status(dev: Device) -> tuple[str, str]:
    """Derive display status from raw device data.

    Returns (paired_status, connection_status).
    """
    if dev.pairing_address:
        return ("Pairing", "Enter code")
    if dev.adb_state == "device":
        return ("Paired", "Connected")
    if dev.adb_state == "unauthorized":
        return ("Paired", "Unauthorized")
    if dev.adb_state == "offline":
        return ("—", "Offline")
    if dev.address:
        return ("—", "Disconnected")
    return ("—", "—")


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

    def on_mount(self) -> None:
        self.query_one("#connect-address", Input).focus()

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
        self._devices: list[Device] = []

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
        # Delay initial refresh to let mDNS discover devices
        self.set_timer(0.5, self.refresh_devices)

    def on_unmount(self) -> None:
        set_log_callback(None)
        set_mdns_log_callback(None)
        self._discovery.stop()

    def _on_discovery_update(self) -> None:
        """Called from zeroconf thread when mDNS discovers/loses devices."""
        self.call_from_thread(self.refresh_devices)

    def _get_selected_device(self) -> Device | None:
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
        connected = sum(1 for d in self._devices if d.adb_state == "device")
        status.update(f"{len(self._devices)} device(s), {connected} connected")

    def action_refresh(self) -> None:
        self.refresh_devices()

    def action_toggle_logs(self) -> None:
        """Toggle log panel visibility."""
        self.query_one("#log-panel", RichLog).toggle_class("visible")

    async def _try_connect(self, dev: Device) -> bool:
        """Try connecting using all known addresses. Updates display on success."""
        # Try preferred address first, then alternatives
        addresses = []
        if dev.address:
            addresses.append(dev.address)
        for addr in dev.connect_addresses:
            if addr not in addresses:
                addresses.append(addr)

        for addr in addresses:
            success, msg = await adb.connect(addr)
            if success:
                dev.address = addr
                self.notify(msg, severity="information", markup=False)
                self.refresh_devices()
                return True

        self.notify("Failed to connect", severity="error")
        return False

    async def action_connect(self) -> None:
        dev = self._get_selected_device()

        if dev and dev.connect_addresses:
            await self._try_connect(dev)
        else:
            # Show dialog for manual entry
            def on_dismiss(success: bool) -> None:
                if success:
                    self.refresh_devices()
            self.push_screen(ConnectScreen(address=dev.address if dev else ""), on_dismiss)

    def action_pair(self) -> None:
        dev = self._get_selected_device()
        if not dev:
            self.notify("No device selected", severity="warning")
            return
        if not dev.pairing_address:
            self.notify("Device not in pairing mode", severity="warning")
            return

        def on_dismiss(success: bool) -> None:
            if success:
                self.refresh_devices()

        self.push_screen(PairScreen(address=dev.pairing_address, device_name=dev.name), on_dismiss)

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

        if dev.pairing_address:
            self.action_pair()
        elif dev.connect_addresses and dev.adb_state != "device":
            await self._try_connect(dev)


def main():
    setup_logging()
    app = AdbUI()
    app.run()


if __name__ == "__main__":
    main()

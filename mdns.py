"""mDNS discovery for ADB devices with proper threading."""

import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from zeroconf import ServiceStateChange, Zeroconf, ServiceBrowser


# Log callback (set by main app)
_log_callback: Callable[[str], None] | None = None


def set_mdns_log_callback(callback: Callable[[str], None] | None) -> None:
    global _log_callback
    _log_callback = callback


def _log(msg: str) -> None:
    if _log_callback:
        timestamp = datetime.now().strftime("%H:%M:%S")
        _log_callback(f"[{timestamp}] [mDNS] {msg}")


class DiscoveryType(Enum):
    CONNECT = "_adb-tls-connect._tcp.local."
    PAIRING = "_adb-tls-pairing._tcp.local."


@dataclass
class DiscoveredDevice:
    instance_name: str
    discovery_type: DiscoveryType
    host: str = ""
    ip: str = ""
    port: int = 0
    device_name: str = ""
    api_level: int = 0

    @property
    def display_name(self) -> str:
        return self.device_name or self.instance_name

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}" if self.ip and self.port else ""

    @property
    def is_pairing(self) -> bool:
        return self.discovery_type == DiscoveryType.PAIRING


class ADBDiscovery:
    """Thread-safe mDNS discovery for ADB devices."""

    def __init__(self, on_update: Callable[[], None] | None = None):
        self._zeroconf: Zeroconf | None = None
        self._browsers: list[ServiceBrowser] = []
        self._devices: dict[str, DiscoveredDevice] = {}
        self._on_update = on_update
        self._lock = threading.RLock()  # Thread-safe reentrant lock

    @property
    def devices(self) -> list[DiscoveredDevice]:
        """Get copy of devices list (thread-safe)."""
        with self._lock:
            return list(self._devices.values())

    def clear(self) -> None:
        """Clear cached devices (thread-safe)."""
        with self._lock:
            self._devices.clear()
        _log("Cache cleared")

    def start(self) -> None:
        """Start mDNS discovery."""
        if self._zeroconf:
            return

        self._zeroconf = Zeroconf()

        for svc_type in [DiscoveryType.CONNECT.value, DiscoveryType.PAIRING.value]:
            browser = ServiceBrowser(
                self._zeroconf,
                svc_type,
                handlers=[self._on_service_state_change],
            )
            self._browsers.append(browser)

        _log("Discovery started")

    def stop(self) -> None:
        """Stop mDNS discovery."""
        for browser in self._browsers:
            browser.cancel()
        self._browsers.clear()

        if self._zeroconf:
            self._zeroconf.close()
            self._zeroconf = None

        with self._lock:
            self._devices.clear()

        _log("Discovery stopped")

    def _on_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Handle service state changes (runs in zeroconf thread)."""
        svc_short = "pairing" if "pairing" in service_type else "connect"

        if state_change == ServiceStateChange.Removed:
            with self._lock:
                removed = self._devices.pop(name, None)
            if removed:
                _log(f"- {removed.display_name} ({svc_short})")
        else:
            # Added or Updated - get service info (outside lock to avoid blocking)
            info = zeroconf.get_service_info(service_type, name, timeout=4000)
            if info:
                device = self._parse_service_info(info, service_type, name)
                with self._lock:
                    self._devices[name] = device
                action = "+" if state_change == ServiceStateChange.Added else "~"
                _log(f"{action} {device.display_name} @ {device.address} ({svc_short})")

        # Notify UI thread (callback should use call_from_thread)
        if self._on_update:
            self._on_update()

    def _parse_service_info(self, info, service_type: str, name: str) -> DiscoveredDevice:
        """Parse zeroconf service info into DiscoveredDevice."""
        discovery_type = (
            DiscoveryType.PAIRING
            if "pairing" in service_type
            else DiscoveryType.CONNECT
        )

        # Parse instance name (e.g., "adb-RFAW70DK06L-IqBWav")
        instance_name = name.replace(f".{service_type}", "")

        # Get first valid address (prefer IPv4, fallback to IPv6)
        ip = ""
        ipv6_fallback = ""
        for addr in info.parsed_addresses():
            if "." in addr:  # IPv4
                if not addr.startswith("169.254"):  # Skip link-local
                    ip = addr
                    break
            elif ":" in addr and not ipv6_fallback:  # IPv6
                if not addr.startswith("fe80"):  # Skip link-local
                    ipv6_fallback = f"[{addr}]"  # Bracket for URL format
        if not ip:
            ip = ipv6_fallback

        # Parse TXT records
        device_name = ""
        api_level = 0
        if info.properties:
            for k, v in info.properties.items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else str(v) if v else ""
                if key == "name":
                    device_name = val
                elif key == "api":
                    try:
                        api_level = int(val)
                    except ValueError:
                        pass

        return DiscoveredDevice(
            instance_name=instance_name,
            discovery_type=discovery_type,
            host=info.server or "",
            ip=ip,
            port=info.port or 0,
            device_name=device_name,
            api_level=api_level,
        )

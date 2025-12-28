"""ADB wrapper module for device operations."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

# Global log callback
_log_callback: Callable[[str], None] | None = None


def set_log_callback(callback: Callable[[str], None] | None) -> None:
    """Set callback for log messages."""
    global _log_callback
    _log_callback = callback


def _log(msg: str) -> None:
    """Log a message."""
    if _log_callback:
        timestamp = datetime.now().strftime("%H:%M:%S")
        _log_callback(f"[{timestamp}] {msg}")


class DeviceState(Enum):
    DEVICE = "device"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    AUTHORIZING = "authorizing"
    NO_PERMISSIONS = "no permissions"


@dataclass
class Device:
    serial: str
    state: DeviceState
    model: str = ""
    product: str = ""
    transport_id: str = ""

    @property
    def display_name(self) -> str:
        if self.model:
            return f"{self.model} ({self.serial})"
        return self.serial

    @property
    def is_wireless(self) -> bool:
        # IP:port format or mDNS format (_adb-tls-connect._tcp)
        return ":" in self.serial or "._adb" in self.serial


async def run_adb(*args: str) -> tuple[int, str, str]:
    """Run an adb command and return (returncode, stdout, stderr)."""
    cmd = f"adb {' '.join(args)}"
    _log(f"$ {cmd}")

    proc = await asyncio.create_subprocess_exec(
        "adb",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stdout_str, stderr_str = stdout.decode(), stderr.decode()

    if stdout_str.strip():
        for line in stdout_str.strip().split("\n"):
            _log(f"  → {line}")
    if stderr_str.strip():
        for line in stderr_str.strip().split("\n"):
            _log(f"  ✗ {line}")

    return proc.returncode, stdout_str, stderr_str


async def get_devices() -> list[Device]:
    """Get list of connected devices."""
    code, stdout, stderr = await run_adb("devices", "-l")
    if code != 0:
        return []

    devices = []
    for line in stdout.strip().split("\n")[1:]:  # Skip header
        if not line.strip():
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        state_str = parts[1]

        try:
            state = DeviceState(state_str)
        except ValueError:
            state = DeviceState.OFFLINE

        # Parse additional info
        model = ""
        product = ""
        transport_id = ""
        for part in parts[2:]:
            if part.startswith("model:"):
                model = part.split(":", 1)[1]
            elif part.startswith("product:"):
                product = part.split(":", 1)[1]
            elif part.startswith("transport_id:"):
                transport_id = part.split(":", 1)[1]

        devices.append(Device(serial, state, model, product, transport_id))

    return devices


async def connect(address: str) -> tuple[bool, str]:
    """Connect to a device. Returns (success, message)."""
    code, stdout, stderr = await run_adb("connect", address)
    output = stdout + stderr
    success = "connected" in output.lower() and "cannot" not in output.lower()
    return success, output.strip()


async def disconnect(address: str) -> tuple[bool, str]:
    """Disconnect from a device. Returns (success, message)."""
    code, stdout, stderr = await run_adb("disconnect", address)
    output = stdout + stderr
    return code == 0, output.strip()


async def disconnect_all() -> tuple[bool, str]:
    """Disconnect all devices."""
    code, stdout, stderr = await run_adb("disconnect")
    return code == 0, (stdout + stderr).strip()


async def pair(address: str, code: str) -> tuple[bool, str]:
    """Pair with a device using pairing code. Returns (success, message)."""
    returncode, stdout, stderr = await run_adb("pair", address, code)
    output = stdout + stderr
    success = "successfully" in output.lower()
    return success, output.strip()


async def kill_server() -> tuple[bool, str]:
    """Kill the ADB server."""
    code, stdout, stderr = await run_adb("kill-server")
    return code == 0, (stdout + stderr).strip() or "Server killed"


async def start_server() -> tuple[bool, str]:
    """Start the ADB server."""
    code, stdout, stderr = await run_adb("start-server")
    return code == 0, (stdout + stderr).strip() or "Server started"

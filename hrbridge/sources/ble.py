"""Bluetooth LE heart rate monitors (standard Heart Rate Service 0x180D).

Works with chest straps and arm bands (Polar, Garmin HRM, Wahoo, Coospo, Magene...)
and with watches that can broadcast heart rate.
"""

from __future__ import annotations

import asyncio
import re

from .base import Source, SourceConfigError, SourceError

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

_ADDRESS = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$|^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)


def parse_measurement(data: bytes) -> tuple[int, bool | None, list[float]]:
    """Decode a Heart Rate Measurement (0x2A37) notification.

    Returns (bpm, skin_contact, rr_intervals_ms). skin_contact is None when the
    sensor doesn't report it.
    """
    flags = data[0]
    if flags & 0x01:
        bpm, i = int.from_bytes(data[1:3], "little"), 3
    else:
        bpm, i = data[1], 2
    contact = bool(flags & 0x02) if flags & 0x04 else None
    if flags & 0x08:
        i += 2  # energy expended
    rr = []
    if flags & 0x10:
        while i + 1 < len(data):
            rr.append(int.from_bytes(data[i:i + 2], "little") * 1000 / 1024)
            i += 2
    return bpm, contact, rr


def _import_bleak():
    try:
        import bleak
    except ImportError as e:
        raise SourceConfigError("the 'ble' source needs bleak: pip install bleak") from e
    return bleak


async def find_device(target: str, timeout: float):
    bleak = _import_bleak()
    if target and _ADDRESS.match(target):
        return await bleak.BleakScanner.find_device_by_address(target, timeout=timeout)
    wanted = target.lower()

    def match(device, adv) -> bool:
        if wanted:
            return wanted in (device.name or adv.local_name or "").lower()
        return HR_SERVICE in [u.lower() for u in adv.service_uuids or []]

    return await bleak.BleakScanner.find_device_by_filter(match, timeout=timeout)


async def scan(timeout: float = 10.0, show_all: bool = False) -> list[dict]:
    bleak = _import_bleak()
    found = await bleak.BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for device, adv in found.values():
        is_hr = HR_SERVICE in [u.lower() for u in adv.service_uuids or []]
        if is_hr or (show_all and (device.name or adv.local_name)):
            out.append({
                "name": device.name or adv.local_name or "(no name)",
                "address": device.address,
                "rssi": adv.rssi,
                "heart_rate": is_hr,
            })
    return sorted(out, key=lambda d: (not d["heart_rate"], -(d["rssi"] or -999)))


class BleSource(Source):
    name = "ble"

    async def run_once(self) -> None:
        bleak = _import_bleak()
        target = self.cfg.ble.device.strip()
        self.set_status("searching", f"looking for {target or 'any heart rate monitor'}")
        device = await find_device(target, self.cfg.ble.scan_seconds)
        if device is None:
            raise SourceError(
                f"no {'device matching ' + repr(target) if target else 'heart rate monitor'} found. "
                "Is it on, worn, and not already connected to another app?"
            )
        loop = asyncio.get_running_loop()
        gone = asyncio.Event()
        label = device.name or device.address
        self.set_status("connecting", label)
        async with bleak.BleakClient(device, disconnected_callback=lambda _c: loop.call_soon_threadsafe(gone.set)) as client:
            try:
                self.info.battery = (await client.read_gatt_char(BATTERY_LEVEL))[0]
            except Exception:  # noqa: BLE001 - battery service is optional
                pass
            await client.start_notify(HR_MEASUREMENT, self._on_measurement)
            self.set_status("connected", label)
            await gone.wait()

    def _on_measurement(self, _char, data: bytearray) -> None:
        try:
            bpm, contact, _rr = parse_measurement(bytes(data))
        except IndexError:
            return
        if contact is False:
            return  # strap reports no skin contact; its number is meaningless
        self.push(bpm)

"""MicroBot Client."""
from __future__ import annotations
import logging
import asyncio
from typing import Optional, Any
import async_timeout
import bleak
from bleak import BleakScanner, discover, BleakError
from bleak.exc import BleakDBusError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    ble_device_has_changed,
    establish_connection,
)
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTCharacteristic, BleakGATTServiceCollection
from bleak.backends.scanner import AdvertisementData
from dataclasses import dataclass
import random, string
import binascii
from binascii import hexlify, unhexlify

_LOGGER: logging.Logger = logging.getLogger(__package__)
CONNECT_LOCK = asyncio.Lock()
DEFAULT_TIMEOUT = None
DEFAULT_RETRY_COUNT = 5
DEFAULT_SCAN_TIMEOUT = 30

SVCABCD = "0000abcd-0000-1000-8000-00805f9b34fb"
CHR2A89 = "00002a89-0000-1000-8000-00805f9b34fb"


@dataclass
class MicroBotAdvertisement:
    """MicroBot avertisement."""

    address: str
    data: dict[str, Any]
    device: BLEDevice


def parse_advertisement_data(
    device: BLEDevice, advertisement_data: AdvertisementData
) -> MicroBotAdvertisement | None:
    """Parse advertisement data."""
    services = advertisement_data.service_uuids
    if not services:
        return
    if SVCABCD not in services:
        return
    else:
        _LOGGER.debug("Updating MicroBot data")
        data = {
            "address": device.address,  # MacOS uses UUIDs
            "local_name": advertisement_data.local_name,
            "rssi": advertisement_data.rssi,
        }

    return MicroBotAdvertisement(device.address, data, device)


def randomid(bits):
    fmtstr = "{:" + "{:02d}".format(int(bits / 4)) + "x}"
    return fmtstr.format(random.randrange(2**bits))


class GetMicroBotDevices:
    """Scan for all MicroBot devices and return"""

    def __init__(self) -> None:
        """Get MicroBot devices class constructor."""
        self._adv_data: dict[str, MicroBotAdvertisement] = {}

    def detection_callback(
        self,
        device: BLEDevice,
        advertisement_data: AdvertisementData,
    ) -> None:
        discovery = parse_advertisement_data(device, advertisement_data)
        if discovery:
            self._adv_data[discovery.address] = discovery

    async def discover(
        self, retry: int = DEFAULT_RETRY_COUNT, scan_timeout: int = DEFAULT_SCAN_TIMEOUT
    ) -> dict:
        """Find microbot devices and their advertisement data."""

        _LOGGER.debug("Running discovery")
        devices = None
        devices = BleakScanner()
        devices.register_detection_callback(self.detection_callback)

        async with CONNECT_LOCK:
            await devices.start()
            await asyncio.sleep(scan_timeout)
            await devices.stop()

        _LOGGER.debug("Stopped discovery")

        if devices is None:
            if retry < 1:
                _LOGGER.error(
                    "Scanning for MicroBot devices failed. Stop trying", exc_info=True
                )
                return self._adv_data

            _LOGGER.warning(
                "Error scanning for MicroBot devices. Retrying (remaining: %d)",
                retry,
            )
            await asyncio.sleep(DEFAULT_RETRY_TIMEOUT)
            return await self.discover(retry - 1, scan_timeout)

        return self._adv_data

    async def _get_devices(self) -> dict:
        """Get microbot devices."""
        if not self._adv_data:
            await self.discover()

        return {address: adv for address, adv in self._adv_data.items()}

    async def get_bots(self) -> dict[str, MicroBotAdvertisement]:
        """Return all MicroBot devices with services data."""
        return await self._get_devices()

    async def get_device_data(
        self, address: str
    ) -> dict[str, MicroBotAdvertisement] | None:
        """Return data for specific device."""
        if not self._adv_data:
            await self.discover()

        _microbot_data = {
            device: data
            for device, data in self._adv_data.items()
            # MacOS uses UUIDs instead of MAC addresses
            if data.get("address") == address
        }

        return _microbot_data


class MicroBotApiClient:

    def __init__(
        self,
        device: BLEDevice,
        token: str,
    ) -> None:
        """MicroBot Client."""
        self._client: BleakClientWithServiceCache | None = None
        self._device = device
        self._mb_adv_data: MicroBotAdvertisement | None = None
        self._bdaddr = device.address
        self._default_timeout = DEFAULT_TIMEOUT
        self._retry: int = DEFAULT_RETRY_COUNT
        self._token = token
        self._token_callback = None
        self._depth = 50
        self._duration = 0
        self._mode = 0
        self._is_on = None
        self._cached_services: BleakGATTServiceCollection | None = None

    @property
    def is_on(self):
        return self._is_on

    @property
    def token(self):
        return self._token_callback

    @property
    def name(self) -> str:
        """Return device name."""
        return f"{self._device.name} ({self._device.address})"

    async def notification_handler(self, handle: int, data: bytes) -> None:
        tmp = binascii.b2a_hex(data)[4 : 4 + 36]
        if b"0f0101" == tmp[:6] or b"0f0102" == tmp[:6]:
            bdaddr = tmp[6 : 6 + 12]
            bdaddr_rcvd = bdaddr.decode()
            _LOGGER.debug("ack with bdaddr: %s", bdaddr_rcvd)
            await self.getToken()
        elif (
            b"1fff" == tmp[0:4]
            and b"0000000000000000000000" != tmp[6 : 6 + 22]
            and b"00000000" == tmp[28:36]
        ):
            token = tmp[4 : 4 + 32]
            self._token_callback = token.decode()
            _LOGGER.debug("ack with token")
            await self._client.stop_notify(CHR2A89)
        else:
            _LOGGER.debug(f'Received response at {handle=}: {hexlify(data, ":")!r}')

    async def notification_handler2(self, handle: int, data: bytes) -> None:
        _LOGGER.debug(f"Received response at {handle=}: {hexlify(data).decode()}")

    async def is_connected(self):
        if not self._client:
            return False
        try:
            return await asyncio.wait_for(
                self._client.is_connected(),
                self._default_timeout,
            )
        except asyncio.TimeoutError:
            return False
        except Exception as e:
            _LOGGER.error(e)
            return False

    async def _do_connect(self):
        x = await self.is_connected()
        if x == True:
            _LOGGER.debug("Already connected")
        else:
            async with CONNECT_LOCK:
                try:
                    self._client = await establish_connection(
                        BleakClientWithServiceCache,
                        self._device,
                        self.name,
                        self._disconnected,
                        max_attempts=self._retry,
                        cached_services=self._cached_services,
                        ble_device_callback=lambda: self._device,
                    )
                    _LOGGER.debug("Connected!")
                    await self._client.start_notify(
                        CHR2A89, self.notification_handler2
                    )
                except Exception as e:
                    _LOGGER.error(e)

    async def _do_disconnect(self):
        if self.is_connected():
            await self._client.disconnect()

    async def connect(self, init=False):
        retry = self._retry
        while True:
            _LOGGER.debug("Connecting to %s", self._bdaddr)
            try:
                await asyncio.wait_for(
                    self._do_connect(),
                    self._default_timeout,
                )
                await self.__setToken(init)
                break
            except Exception as e:
                if retry == 0:
                    _LOGGER.error("Failed to connect: %s", e)
                    break
                retry = retry - 1
                _LOGGER.debug("Retrying connect")
                await asyncio.sleep(0.5)

    async def disconnect(self):
        _LOGGER.debug("Disconnecting from %s", self._bdaddr)
        try:
            await asyncio.wait_for(
                self._do_disconnect(),
                self._default_timeout,
            )
        except Exception as e:
            _LOGGER.error("error: %s", e)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        _LOGGER.debug("Disconnection callback")

    async def __initToken(self):
        _LOGGER.debug("Generating token")
        try:
            id = self.__randomid(16)
            bar1 = list(binascii.a2b_hex(id + "00010040e20100fa01000700000000000000"))
            bar2 = list(
                binascii.a2b_hex(id + "0fffffffffffffffffffffffffff" + self._token)
            )
            _LOGGER.debug("Waiting for bdaddr notification")
            await self._client.start_notify(
                CHR2A89, self.notification_handler
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar1), response=True
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar2), response=True
            )
        except Exception as e:
            _LOGGER.error("failed to init token: %s", e)

    async def __setToken(self, init):
        if init:
            _LOGGER.debug("init set to True")
            await self.__initToken()
        else:
            _LOGGER.debug("Setting token")
            try:
                id = self.__randomid(16)
                bar1 = list(
                    binascii.a2b_hex(id + "00010000000000fa0000070000000000decd")
                )
                bar2 = list(binascii.a2b_hex(id + "0fff" + self._token))
                await self._client.write_gatt_char(
                    CHR2A89, bytearray(bar1), response=True
                )
                await self._client.write_gatt_char(
                    CHR2A89, bytearray(bar2), response=True
                )
                _LOGGER.debug("Token set")
            except Exception as e:
                _LOGGER.error("Failed to set token: %s", e)

    async def getToken(self):
        _LOGGER.debug("Getting token")
        try:
            id = self.__randomid(16)
            bar1 = list(binascii.a2b_hex(id + "00010040e20101fa01000000000000000000"))
            bar2 = list(binascii.a2b_hex(id + "0fffffffffffffffffff0000000000000000"))
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar1), response=True
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar2), response=True
            )
            _LOGGER.warning("touch the button to get a token")
        except Exception as e:
            _LOGGER.error("failed to request token: %s", e)

    async def push_on(self, init=False):
        x = await self.is_connected()
        if x == False:
            await self.connect(init=False)
            _LOGGER.debug("Attempting to push")
        try:
            id = self.__randomid(16)
            bar1 = list(binascii.a2b_hex(id + "000100000008020000000a0000000000decd"))
            bar2 = list(binascii.a2b_hex(id + "0fffffffffff000000000000000000000000"))
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar1), response=True
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar2), response=True
            )
            _LOGGER.debug("Pushed")
            self._is_on = True
        except Exception as e:
            _LOGGER.error("Failed to push: %s", e)
            self._is_on = False
        await self.disconnect()

    async def push_off(self):
        x = await self.is_connected()
        if x == False:
            await self.connect(init=False)
            _LOGGER.debug("Attempting to push")
        try:
            id = self.__randomid(16)
            bar1 = list(binascii.a2b_hex(id + "000100000008020000000a0000000000decd"))
            bar2 = list(binascii.a2b_hex(id + "0fffffffffff000000000000000000000000"))
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar1), response=True
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar2), response=True
            )
            _LOGGER.debug("Pushed")
            self._is_on = False
        except Exception as e:
            _LOGGER.error("Failed to push: %s", e)
            self._is_on = True
        await self.disconnect()

    def setDepth(self, depth):
        self._depth = depth
        _LOGGER.debug("Depth: %s", depth)

    def setDuration(self, duration):
        self._duration = duration
        _LOGGER.debug("Duration: %s", duration)

    def setMode(self, mode):
        if "normal" == mode:
            self._mode = 0
        elif "invert" == mode:
            self._mode = 1
        elif "toggle" == mode:
            self._mode = 2
        _LOGGER.debug("Mode: %s", mode)

    async def calibrate(self, depth, duration, mode):
        x = await self.is_connected()
        if x == False:
            await self.connect(init=False)
            _LOGGER.debug("Setting calibration")
        self.setMode(mode)
        self.setDepth(depth)
        self.setDuration(duration)
        try:
            id = self.__randomid(16)
            bar1 = list(binascii.a2b_hex(id + "000100000008030001000a0000000000decd"))
            bar2 = list(
                binascii.a2b_hex(
                    id
                    + "0fff"
                    + "{:02x}".format(self._mode)
                    + "000000"
                    + "000000000000000000000000"
                )
            )
            bar3 = list(binascii.a2b_hex(id + "000100000008040001000a0000000000decd"))
            bar4 = list(
                binascii.a2b_hex(
                    id
                    + "0fff"
                    + "{:02x}".format(self._depth)
                    + "000000"
                    + "000000000000000000000000"
                )
            )
            bar5 = list(binascii.a2b_hex(id + "000100000008050001000a0000000000decd"))
            bar6 = list(
                binascii.a2b_hex(
                    id
                    + "0fff"
                    + self._duration.to_bytes(4, "little").hex()
                    + "000000000000000000000000"
                )
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar1), response=True
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar2), response=True
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar3), response=True
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar4), response=True
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar5), response=True
            )
            await self._client.write_gatt_char(
                CHR2A89, bytearray(bar6), response=True
            )
            _LOGGER.debug("Calibration set")
        except Exception as e:
            _LOGGER.error("Failed to calibrate: %s", e)

    def update_from_advertisement(self, advertisement: MicroBotAdvertisement) -> None:
        """Update device data from advertisement."""
        _LOGGER.debug("Updating from advertisement")
        self._mb_adv_data = advertisement
        _LOGGER.debug("Advertisement: %s", self._mb_adv_data)
        self._device = advertisement.device
        _LOGGER.debug("Device: %s", self._device)

    def __randomstr(self, n):
        randstr = [random.choice(string.printable) for i in range(n)]
        return "".join(randstr)

    def __randomid(self, bits):
        fmtstr = "{:" + "{:02d}".format(int(bits / 4)) + "x}"
        return fmtstr.format(random.randrange(2**bits))

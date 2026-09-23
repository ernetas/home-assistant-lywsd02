from __future__ import annotations

import time
import struct
import logging

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType
from homeassistant.components import bluetooth
from homeassistant.util import dt as dt_util

DOMAIN = "lywsd02"

_LOGGER = logging.getLogger(__name__)


_UUID_TIME = 'EBE0CCB7-7A0A-4B0C-8A1A-6FF2997DA3A6'
_UUID_TEMO = 'EBE0CCBE-7A0A-4B0C-8A1A-6FF2997DA3A6'

def get_localized_timestamp(tz_offset=0):
    """Return the epoch that makes the device display local time.

    The device shows `timestamp + tz_offset hours` as wall-clock time, so the
    local UTC offset is baked into the timestamp minus whatever part of it
    `tz_offset` already contributes. Baking the full offset in regardless of
    `tz_offset` double-counts it (see #13): at UTC+3 with tz_offset=3 the
    clock ran 3 hours fast.

    Uses Home Assistant's configured time zone (dt_util), not the host OS
    one - containerized installs typically keep the OS on UTC.
    """
    now = int(time.time())
    offset = dt_util.now().utcoffset()
    return now + int(offset.total_seconds()) - tz_offset * 3600

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """
    Based off https://github.com/h4/lywsd02
    """

    async def set_time(call: ServiceCall) -> None:
        mac = call.data['mac'].upper()
        if not mac:
            _LOGGER.error(f"The 'mac' parameter is missing from service call: {call.data}.")
            return

        tz_offset = call.data.get('tz_offset', 0)

        ble_device = bluetooth.async_ble_device_from_address(
            hass,
            mac,
            connectable=True
        )

        if not ble_device:
            _LOGGER.error(f"Could not find '{mac}'.")
            return

        _LOGGER.info(f"Found '{ble_device}' - Attempting to update time.")

        temo_set = False
        ckmo_set = False
        temo = call.data.get('temp_mode', '') or "x"
        temo = temo.upper()
        _LOGGER.debug(f"temo var: {temo}")

        if temo in 'CF':
            data_temp_mode = struct.pack('B', (0x01 if temo == 'F' else 0xFF))
            _LOGGER.debug(f"Will set temp_mode")
            temo_set = True

        ckmo = call.data.get('clock_mode', 0)
        _LOGGER.debug(f"ckmo var: {ckmo}")
        if ckmo in [12, 24]:
            data_clock_mode = struct.pack('IHB', 0, 0, 0xaa if ckmo == 12 else 0x00)
            _LOGGER.debug(f"Will set clock_mode")
            ckmo_set = True

        tout = int(call.data.get('timeout', 60))

        # A plain BleakClient regularly fails on the first attempt when the
        # device is reached through an ESPHome/Shelly Bluetooth proxy rather
        # than a local adapter. establish_connection retries and handles the
        # proxy's connection slots; `timeout` is forwarded to the client.
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            mac,
            timeout=tout,
        )
        try:
            timestamp = call.data.get('timestamp')
            if timestamp is None:
                timestamp = get_localized_timestamp(tz_offset)
            timestamp = int(timestamp)

            data = struct.pack('Ib', timestamp, tz_offset)
            await client.write_gatt_char(_UUID_TIME, data)
            if temo_set:
                await client.write_gatt_char(_UUID_TEMO, data_temp_mode)
            if ckmo_set:
                # 12/24-hour switching writes a 7-byte clock-format value to the
                # time characteristic. This is validated against a Mi Home app
                # capture on the LYWSD02MMC (0xAA => 12h, 0x00 => 24h, see #10),
                # but on the plain LYWSD02 the same characteristic is a fixed
                # 5-byte time attribute and rejects it with "Invalid attribute
                # length". Treat a rejection as "unsupported on this model" and
                # warn rather than failing the call - the time is already set.
                try:
                    await client.write_gatt_char(_UUID_TIME, data_clock_mode)
                except BleakError as err:
                    _LOGGER.warning(
                        "clock_mode (12/24-hour) could not be set on '%s': it is "
                        "only supported on the LYWSD02MMC and this device "
                        "rejected the write (%s). The time was set successfully "
                        "- remove the 'clock_mode' parameter to silence this "
                        "warning.",
                        mac, err,
                    )
        finally:
            await client.disconnect()

        _LOGGER.info(f"Done - refreshed time on '{mac}' to '{timestamp}' with offset of '{tz_offset}' hours.")

    hass.services.async_register(DOMAIN, 'set_time', set_time)

    return True

"""LuxPower SNA5000 WPV Inverter Reader – READ ONLY!

Connects to the WiFi dongle and ONLY READS the data (ReadInput, function 0x04).
NEVER WRITE (functions 0x06/0x10 are NOT used).
Input registers are physically read-only in Modbus.

The packet layout follows publicly documented LuxPower protocol behavior."""

import asyncio
import logging
import struct
from dataclasses import dataclass
from enum import IntEnum

from inverterscout.settings.runtime import DONGLE_SERIAL as CONFIGURED_DONGLE_SERIAL
from inverterscout.settings.runtime import INVERTER_SERIAL as CONFIGURED_INVERTER_SERIAL

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Status codes reported while utility-grid power is unavailable.
# ──────────────────────────────────────────────
OFF_GRID_STATUSES = {0x40, 0x80, 0xC0, 0x88}
MIN_GRID_VOLTAGE = 50  # Below this voltage, grid power is considered unavailable.
LOW_GRID_VOLTAGE = (
    180  # Below this voltage, grid power is present but outside the normal operating range.
)

# Reject implausible generator values caused by malformed register data.
MAX_REASONABLE_GEN_POWER = 8000  # W

# ──────────────────────────────────────────────
# Input Register Map (function 0x04) — VERIFIED
# Source: EG4-18KPV-12LV Modbus Protocol + lxp-bridge
# ──────────────────────────────────────────────
REG_STATUS = 0  # Inverter status/mode (code)
REG_VPV1 = 1  # Voltage PV1 (0.1V)
REG_VPV2 = 2  # Voltage PV2 (0.1V)
REG_VPV3 = 3  # Voltage PV3 (0.1V)
REG_VBAT = 4  # Battery voltage (0.1V)
REG_SOC_SOH = 5  # SOC (low byte %) + SOH (high byte %)
REG_INTERNAL_FAULT = 6  # Internal error code
REG_PPV1 = 7  # Power PV1 (W)
REG_PPV2 = 8  # Power PV2 (W)
REG_PPV3 = 9  # Power PV3 (W)
REG_PCHARGE = 10  # Battery charging power (W)
REG_PDISCHARGE = 11  # Battery discharge power (W)
REG_VACR = 12  # Mains voltage phase R (0.1V)
REG_VACS = 13  # Mains voltage phase S (0.1V)
REG_VACT = 14  # Mains voltage phase T (0.1V)
REG_FAC = 15  # Network frequency (0.01Hz)
REG_PINV = 16  # Inverter output power (W)
REG_PREC = 17  # Mains charging power (W)
REG_IINV_RMS = 18  # Inverter current RMS (0.01A)
REG_PF = 19  # Power factor (0.001)
REG_VEPS_R = 20  # EPS voltage phase R (0.1V)
REG_VEPS_S = 21  # EPS voltage phase S (0.1V)
REG_VEPS_T = 22  # EPS voltage phase T (0.1V)
REG_FEPS = 23  # EPS frequency (0.01Hz)
REG_PEPS = 24  # EPS load power (W)
REG_SEPS = 25  # EPS Apparent Power (VA)
REG_PTOGRID = 26  # Export to the utility grid (W)
REG_PTOUSER = 27  # Import from the utility grid (W)

# Generator registers
REG_AC_INPUT_TYPE = 77  # bit 0: 0=Grid, 1=Generator
REG_GEN_VOLTAGE = 121  # 0.1V
REG_GEN_FREQUENCY = 122  # 0.01Hz
REG_GEN_POWER = 123  # W

# ──────────────────────────────────────────────
# LuxPower WiFi Dongle Protocol Constants
# ──────────────────────────────────────────────
PREFIX = b"\xa1\x1a"  # Start of packet marker
TCP_FUNC_TRANSLATED_DATA = 0xC2  # Read/Write Data
TCP_FUNC_HEARTBEAT = 0xC1  # Heartbeat from dongle
DEVICE_FUNC_READ_INPUT = 0x04  # Reading input registers (READ ONLY!)

# Serial numbers are supplied by the first-run setup wizard.
DONGLE_SERIAL = CONFIGURED_DONGLE_SERIAL.encode("ascii")
INVERTER_SERIAL = CONFIGURED_INVERTER_SERIAL.encode("ascii")

# Minimum set of registers for valid data
_REQUIRED_BASE_REGS = {REG_STATUS, REG_VBAT, REG_SOC_SOH, REG_VACR}


class InverterStatus(IntEnum):
    STANDBY = 0x00
    AC_CHARGING = 0x10
    ON_GRID = 0x20
    BATTERY_OFF_GRID = 0x40
    PV_OFF_GRID = 0x80
    PV_CHARGE_OFF_GRID = 0x88
    PV_BATTERY_OFF_GRID = 0xC0


@dataclass
class InverterData:
    """Data from the inverter (read only, passive mode)."""

    status: int = 0
    battery_voltage: float = 0.0  # V
    soc: int = 0  # %
    soh: int = 0  # %
    pv1_power: int = 0  # W
    pv2_power: int = 0  # W
    pv3_power: int = 0  # W
    pv1_voltage: float = 0.0  # V
    pv2_voltage: float = 0.0  # V
    pv3_voltage: float = 0.0  # V
    grid_voltage: float = 0.0  # V
    grid_frequency: float = 0.0  # Hz
    eps_power: int = 0  # W
    grid_power_import: int = 0  # W imported from the utility grid
    grid_power_export: int = 0  # W exported to the utility grid
    battery_charge: int = 0  # W
    battery_discharge: int = 0  # W
    inverter_power: int = 0  # W
    # Generator
    ac_input_type: int = 0  # reg 77: bit 0 = 0:Grid / 1:Gen
    gen_voltage: float = 0.0  # V
    gen_frequency: float = 0.0  # Hz
    gen_power: int = 0  # W

    @property
    def is_valid(self) -> bool:
        """Is the data valid? If the battery is 0V and SOC is 0%, it is garbage data."""
        if self.battery_voltage < 0.1 and self.soc == 0:
            return False
        return True

    @property
    def total_pv_power(self) -> int:
        return self.pv1_power + self.pv2_power + self.pv3_power

    @property
    def house_power(self) -> int:
        """Total home consumption (W) - energy balance from all sources."""
        return max(
            0,
            self.grid_power_import
            - self.grid_power_export
            + self.total_pv_power
            + self.battery_discharge
            - self.battery_charge,
        )

    @property
    def power_source(self) -> str:
        """Classify the active power source from inverter status and grid voltage."""
        if self.status in OFF_GRID_STATUSES or self.grid_voltage < MIN_GRID_VOLTAGE:
            return "no_grid"
        if self.grid_voltage < LOW_GRID_VOLTAGE:
            return "low_voltage"
        return "grid"

    @property
    def on_battery(self) -> bool:
        """Return whether utility-grid power is unavailable."""
        return self.power_source == "no_grid"

    @property
    def generator_on(self) -> bool:
        """Return whether register 77 reports an active generator input."""
        return bool(self.ac_input_type & 0x01)

    @property
    def status_name(self) -> str:
        try:
            return InverterStatus(self.status).name
        except ValueError:
            return f"UNKNOWN(0x{self.status:02X})"


# ──────────────────────────────────────────────
# CRC-16/Modbus (for checking incoming packets)
# ──────────────────────────────────────────────
def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if (crc & 1) != 0:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


# ──────────────────────────────────────────────
# ReadInput Request Builder (READ ONLY! Function 0x04)
# NEVER use 0x06 (WriteSingle) or 0x10 (WriteMulti)
# ──────────────────────────────────────────────
def _build_read_input_request(
    dongle_serial: bytes,
    inverter_serial: bytes,
    start_register: int = 0,
    register_count: int = 40,
) -> bytes:
    """Builds a ReadInput request packet (function 0x04) to read input registers.

    Input registers are PHYSICALLY READ-ONLY in Modbus - it is impossible to write to them.
    This is the same request that the LuxPower and lxp-bridge applications send.

    Packet format (protocol v1, 38 bytes):
      [0-1] 0xA1 0x1A - prefix
      [2-3] protocol u16 LE - protocol version (1)
      [4-5] frame_len u16 LE - length after these 6 bytes (32)
      [6] 0x01 — reserved
      [7] 0xC2 - TCP function TranslatedData
      [8-17] dongle serial - 10 bytes ASCII
      [18-19] data_len u16 LE - data frame length + CRC (18)
      --- data frame (16 bytes) ---
      [20] 0x00 - action
      [21] 0x04 - ReadInput (READ ONLY!)
      [22-31] inverter serial — 10 bytes ASCII
      [32-33] start_reg u16 LE - starting register
      [34-35] count u16 LE - number of registers
      [36-37] CRC-16 Modbus - data frame checksum"""
    # Data frame: action + device_func + inverter_serial + register + count
    data_frame = (
        b"\x00"  # action
        + bytes([DEVICE_FUNC_READ_INPUT])  # 0x04 = ReadInput (READ ONLY)
        + inverter_serial  # 10 bytes
        + struct.pack("<H", start_register)  # start register
        + struct.pack("<H", register_count)  # count
    )  # 16 bytes total

    crc = _crc16_modbus(data_frame)
    data_with_crc = data_frame + struct.pack("<H", crc)  # 18 bytes

    # TCP header
    frame_len = (
        1 + 1 + 10 + 2 + len(data_with_crc)
    )  # reserved + tcp_func + dongle_sn + data_len + data
    packet = (
        PREFIX  # 0xA1 0x1A
        + struct.pack("<H", 1)  # protocol v1
        + struct.pack("<H", frame_len)  # frame length
        + b"\x01"  # reserved
        + bytes([TCP_FUNC_TRANSLATED_DATA])  # 0xC2
        + dongle_serial  # 10 bytes
        + struct.pack("<H", len(data_with_crc))  # data length
        + data_with_crc  # data frame + CRC
    )

    return packet


# ──────────────────────────────────────────────
# Packet Parser
# ──────────────────────────────────────────────
def _find_packets(data: bytes) -> list[bytes]:
    """Finds all packets with the prefix 0xA1 0x1A in the byte stream."""
    packets = []
    i = 0
    while i < len(data) - 6:
        if data[i] == 0xA1 and i + 1 < len(data) and data[i + 1] == 0x1A:
            if i + 5 >= len(data):
                break
            frame_len = struct.unpack("<H", data[i + 4 : i + 6])[0]
            total_len = 6 + frame_len  # prefix(2) + proto(2) + flen(2) + rest
            if i + total_len <= len(data):
                packets.append(data[i : i + total_len])
                i += total_len
                continue
        i += 1
    return packets


def _parse_packet(packet: bytes) -> dict | None:
    """Parses one incoming LuxPower protocol packet.
    Returns a dictionary with fields or None."""
    if len(packet) < 8:
        return None

    if packet[0:2] != PREFIX:
        return None

    protocol = struct.unpack("<H", packet[2:4])[0]
    frame_len = struct.unpack("<H", packet[4:6])[0]

    if len(packet) < 6 + frame_len:
        logger.debug("Packet incomplete: expected %d, received %d", 6 + frame_len, len(packet))
        return None

    tcp_func = packet[7]
    dongle_serial = packet[8:18] if len(packet) >= 18 else b""

    if tcp_func == TCP_FUNC_HEARTBEAT:
        logger.debug("Heartbeat received from inverter dongle")
        return {"type": "heartbeat", "dongle_serial": dongle_serial}

    if tcp_func != TCP_FUNC_TRANSLATED_DATA:
        logger.debug("Unknown tcp_func: 0x%02X", tcp_func)
        return None

    if len(packet) < 20:
        return None
    data_len = struct.unpack("<H", packet[18:20])[0]

    if data_len < 4 or 20 + data_len > len(packet):
        logger.debug("Invalid data_len: %d", data_len)
        return None

    # Data frame starts at byte 20, last 2 bytes are CRC
    data_frame = packet[20 : 20 + data_len - 2]
    crc_received = struct.unpack("<H", packet[20 + data_len - 2 : 20 + data_len])[0]

    # Verify CRC
    crc_computed = _crc16_modbus(data_frame)
    if crc_received != crc_computed:
        logger.warning("CRC mismatch: received=0x%04X computed=0x%04X", crc_received, crc_computed)

    if len(data_frame) < 14:
        return None

    device_func = data_frame[1]
    inverter_serial = data_frame[2:12]
    register = struct.unpack("<H", data_frame[12:14])[0]

    result = {
        "type": "data",
        "protocol": protocol,
        "dongle_serial": dongle_serial,
        "inverter_serial": inverter_serial,
        "device_func": device_func,
        "register": register,
    }

    # Register values ​​after register addr
    # Protocol v1: no value_length_byte
    # Protocol v2+: there is value_length_byte
    values_start = 14
    if protocol != 1 and device_func in (0x03, 0x04):
        if len(data_frame) > 14:
            values_start = 15

    values_data = data_frame[values_start:]
    result["values"] = values_data

    return result


def _parse_registers_from_values(values: bytes, start_reg: int = 0) -> dict[int, int]:
    """Parses register values ​​from data. Each register = 2 bytes LE."""
    registers = {}
    for i in range(0, len(values) - 1, 2):
        reg_num = start_reg + (i // 2)
        val = struct.unpack("<H", values[i : i + 2])[0]
        registers[reg_num] = val
    return registers


def _has_required_registers(registers: dict[int, int]) -> bool:
    """Is there a minimum set of basic registers + register 77."""
    return _REQUIRED_BASE_REGS.issubset(registers) and REG_AC_INPUT_TYPE in registers


def _registers_to_data(registers: dict[int, int]) -> InverterData:
    """Converts a dictionary of registers to InverterData."""
    data = InverterData()

    if REG_STATUS in registers:
        data.status = registers[REG_STATUS] & 0xFF

    if REG_VBAT in registers:
        data.battery_voltage = registers[REG_VBAT] * 0.1

    if REG_SOC_SOH in registers:
        raw = registers[REG_SOC_SOH]
        data.soc = raw & 0xFF
        data.soh = (raw >> 8) & 0xFF

    if REG_VPV1 in registers:
        data.pv1_voltage = registers[REG_VPV1] * 0.1
    if REG_VPV2 in registers:
        data.pv2_voltage = registers[REG_VPV2] * 0.1
    if REG_VPV3 in registers:
        data.pv3_voltage = registers[REG_VPV3] * 0.1

    if REG_PPV1 in registers:
        data.pv1_power = registers[REG_PPV1]
    if REG_PPV2 in registers:
        data.pv2_power = registers[REG_PPV2]
    if REG_PPV3 in registers:
        data.pv3_power = registers[REG_PPV3]

    if REG_VACR in registers:
        data.grid_voltage = registers[REG_VACR] * 0.1

    if REG_FAC in registers:
        data.grid_frequency = registers[REG_FAC] * 0.01

    if REG_PEPS in registers:
        data.eps_power = registers[REG_PEPS]

    if REG_PTOUSER in registers:
        data.grid_power_import = registers[REG_PTOUSER]

    if REG_PTOGRID in registers:
        data.grid_power_export = registers[REG_PTOGRID]

    if REG_PCHARGE in registers:
        data.battery_charge = registers[REG_PCHARGE]
    if REG_PDISCHARGE in registers:
        data.battery_discharge = registers[REG_PDISCHARGE]
    if REG_PINV in registers:
        data.inverter_power = registers[REG_PINV]

    # Generator
    if REG_AC_INPUT_TYPE in registers:
        data.ac_input_type = registers[REG_AC_INPUT_TYPE]
    if REG_GEN_VOLTAGE in registers:
        data.gen_voltage = registers[REG_GEN_VOLTAGE] * 0.1
    if REG_GEN_FREQUENCY in registers:
        data.gen_frequency = registers[REG_GEN_FREQUENCY] * 0.01
    if REG_GEN_POWER in registers:
        raw_gen_power = registers[REG_GEN_POWER]
        if raw_gen_power > MAX_REASONABLE_GEN_POWER:
            logger.warning(
                "Ignoring implausible generator power: %dW exceeds %dW",
                raw_gen_power,
                MAX_REASONABLE_GEN_POWER,
            )
            data.gen_power = 0
        else:
            data.gen_power = raw_gen_power

    return data


# ──────────────────────────────────────────────
# Main read function — READ ONLY!
# Sends a ReadInput request (0x04) and listens for a response.
# Function 0x04 = read input registers (read-only).
# NEVER sends Write (0x06/0x10).
# ──────────────────────────────────────────────
async def read_inverter(host: str, port: int, timeout: float = 30.0) -> InverterData | None:
    """Read inverter data through passive Read Input Registers requests.

    Connects to the WiFi dongle, sends a request to read input registers
    (function 0x04 - READ ONLY, input registers are not physically writable).

    Accumulates registers from all incoming packets. If after block 0-39
    no reg77 - sends an additional ReadInput(40,40). Likewise for 121-123.

    Returns:
        InverterData or None on error/timeout."""
    logger.info("Connecting to inverter %s:%d", host, port)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=15.0)
        local = writer.get_extra_info("sockname")
        remote = writer.get_extra_info("peername")
        logger.info("TCP connected: %s -> %s", local, remote)
    except (asyncio.TimeoutError, OSError) as e:
        logger.error("Failed to connect to inverter %s:%d: %s", host, port, e)
        return None

    try:
        # Send a ReadInput request (0x04 = READ ONLY)
        request = _build_read_input_request(
            DONGLE_SERIAL,
            INVERTER_SERIAL,
            start_register=0,
            register_count=40,
        )
        writer.write(request)
        await writer.drain()
        logger.info("ReadInput request sent (registers 0-39, %d bytes)", len(request))

        # Accumulate registers from all packets.
        all_registers: dict[int, int] = {}
        buffer = b""
        total_bytes = 0
        deadline = asyncio.get_event_loop().time() + timeout
        sent_extra_40 = False
        sent_extra_120 = False

        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=min(remaining, 5.0))
                if not chunk:
                    logger.warning("Connection closed by dongle (EOF) after %d bytes", total_bytes)
                    break
                total_bytes += len(chunk)
                buffer += chunk
                logger.info("Received %d bytes (total %d)", len(chunk), total_bytes)

                # Parse all complete packets received so far.
                packets = _find_packets(buffer)
                for raw_packet in packets:
                    parsed = _parse_packet(raw_packet)
                    if parsed is None:
                        logger.warning("Failed to parse packet (%d bytes)", len(raw_packet))
                        continue

                    if parsed["type"] == "heartbeat":
                        logger.info("Heartbeat received from inverter dongle")
                        continue

                    if parsed["type"] == "data" and parsed["device_func"] in (0x04, 0x03):
                        start_reg = parsed["register"]
                        values = parsed["values"]
                        registers = _parse_registers_from_values(values, start_reg)
                        all_registers.update(registers)

                        logger.info(
                            "Data: func=0x%02X, start_reg=%d, registers=%d (total accumulated: %d)",
                            parsed["device_func"],
                            start_reg,
                            len(registers),
                            len(all_registers),
                        )

                # Remove complete packets from the receive buffer.
                if packets:
                    last_packet_end = buffer.rfind(packets[-1]) + len(packets[-1])
                    buffer = buffer[last_packet_end:]

                # Request 40-79 when base registers arrived without register 77.
                if (
                    _REQUIRED_BASE_REGS.issubset(all_registers)
                    and REG_AC_INPUT_TYPE not in all_registers
                    and not sent_extra_40
                ):
                    sent_extra_40 = True
                    req = _build_read_input_request(
                        DONGLE_SERIAL,
                        INVERTER_SERIAL,
                        start_register=40,
                        register_count=40,
                    )
                    writer.write(req)
                    await writer.drain()
                    logger.info("Fallback: ReadInput sent (registers 40-79)")

                # Request 120-159 when register 77 arrived without register 121.
                if (
                    REG_AC_INPUT_TYPE in all_registers
                    and REG_GEN_VOLTAGE not in all_registers
                    and not sent_extra_120
                ):
                    sent_extra_120 = True
                    req = _build_read_input_request(
                        DONGLE_SERIAL,
                        INVERTER_SERIAL,
                        start_register=120,
                        register_count=40,
                    )
                    writer.write(req)
                    await writer.drain()
                    logger.info("Fallback: ReadInput sent (registers 120-159)")

                # Enough data? Basic + reg77 (reg121-123 optional)
                if _has_required_registers(all_registers):
                    # Allow extra time while waiting for registers 121-123.
                    if REG_GEN_VOLTAGE not in all_registers and not sent_extra_120:
                        continue
                    # Allow the fallback request time to return generator registers.
                    if sent_extra_120 and REG_GEN_VOLTAGE not in all_registers:
                        continue
                    logger.info(
                        "Received %d registers - enough for InverterData", len(all_registers)
                    )
                    return _registers_to_data(all_registers)

            except asyncio.TimeoutError:
                elapsed = int(timeout - remaining)
                if elapsed > 0 and elapsed % 10 < 6:
                    logger.info(
                        "Waiting for a response... %d/%d sec, received %d bytes, registers %d",
                        elapsed,
                        int(timeout),
                        total_bytes,
                        len(all_registers),
                    )
                continue

        # Return the registers collected before the timeout.
        if _REQUIRED_BASE_REGS.issubset(all_registers):
            logger.info(
                "Read timed out after receiving %d usable registers; returning partial data",
                len(all_registers),
            )
            return _registers_to_data(all_registers)

        if buffer:
            logger.warning(
                "Received %d bytes, but failed to collect usable registers",
                len(buffer),
            )
        else:
            logger.warning(
                "No response from the inverter for %d sec (%d bytes received)",
                int(timeout),
                total_bytes,
            )

        return None

    except Exception as e:
        logger.error("Error when reading data from the inverter: %s", e)
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

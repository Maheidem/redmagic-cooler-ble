"""AppleSMC sensor reads for Mac temperature and fan telemetry."""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import platform
import struct
from dataclasses import dataclass, field

SMC_SELECTOR = 2
SMC_CMD_READ_BYTES = 5
SMC_CMD_READ_INDEX = 8
SMC_CMD_READ_KEYINFO = 9

CPU_SENSOR_PREFIXES = ("Tp", "Te")
GPU_SENSOR_PREFIXES = ("Tg",)


@dataclass(frozen=True)
class SmcReading:
    """Decoded value for one SMC key."""

    key: str
    data_type: str
    raw: bytes
    value: float | int | str | None


@dataclass(frozen=True)
class SmcSnapshot:
    """Mac hardware telemetry read from AppleSMC."""

    cpu_temperature_c: float | None = None
    gpu_temperature_c: float | None = None
    cpu_sensor_key: str | None = None
    gpu_sensor_key: str | None = None
    fan_rpms: tuple[int, ...] = ()
    temperature_sensors: dict[str, float] = field(default_factory=dict)
    source: str = "apple-smc"
    error: str | None = None

    @property
    def hottest_temperature_c(self) -> float | None:
        values = [value for value in (self.cpu_temperature_c, self.gpu_temperature_c) if value is not None]
        return max(values) if values else None

    @property
    def max_fan_rpm(self) -> int | None:
        return max(self.fan_rpms) if self.fan_rpms else None


class SMCVersion(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_ubyte),
        ("minor", ctypes.c_ubyte),
        ("build", ctypes.c_ubyte),
        ("reserved", ctypes.c_ubyte),
        ("release", ctypes.c_ushort),
    ]


class SMCPLimitData(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_ushort),
        ("length", ctypes.c_ushort),
        ("cpuPLimit", ctypes.c_uint32),
        ("gpuPLimit", ctypes.c_uint32),
        ("memPLimit", ctypes.c_uint32),
    ]


class SMCKeyInfoData(ctypes.Structure):
    _fields_ = [
        ("dataSize", ctypes.c_uint32),
        ("dataType", ctypes.c_uint32),
        ("dataAttributes", ctypes.c_ubyte),
    ]


class SMCKeyData(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint32),
        ("vers", SMCVersion),
        ("pLimitData", SMCPLimitData),
        ("keyInfo", SMCKeyInfoData),
        ("result", ctypes.c_ubyte),
        ("status", ctypes.c_ubyte),
        ("data8", ctypes.c_ubyte),
        ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_ubyte * 32),
    ]


class SmcError(RuntimeError):
    """AppleSMC access failed."""


class SmcClient:
    """Thin ctypes wrapper around the private AppleSMC user client."""

    def __init__(self) -> None:
        self._iokit = None
        self._libc = None
        self._service = 0
        self._connection = ctypes.c_uint32()

    def __enter__(self) -> "SmcClient":
        if platform.system() != "Darwin":
            raise SmcError("AppleSMC is only available on macOS")

        iokit_path = ctypes.util.find_library("IOKit")
        system_path = ctypes.util.find_library("System")
        if not iokit_path or not system_path:
            raise SmcError("IOKit/System libraries not found")

        self._iokit = ctypes.CDLL(iokit_path)
        self._libc = ctypes.CDLL(system_path)
        self._configure_iokit()

        self._service = self._iokit.IOServiceGetMatchingService(0, self._iokit.IOServiceMatching(b"AppleSMC"))
        if not self._service:
            raise SmcError("AppleSMC service not found")

        mach_task_self = ctypes.c_uint32.in_dll(self._libc, "mach_task_self_").value
        result = self._iokit.IOServiceOpen(self._service, mach_task_self, 0, ctypes.byref(self._connection))
        if result:
            self._release_service()
            raise SmcError(f"AppleSMC open failed: 0x{result:x}")
        return self

    def __exit__(self, *_exc_info) -> None:
        if self._iokit and self._connection.value:
            self._iokit.IOServiceClose(self._connection.value)
            self._connection = ctypes.c_uint32()
        self._release_service()

    def read_key(self, key: str) -> SmcReading | None:
        """Read and decode a single SMC key."""

        key_info = self._read_key_info(key)
        if key_info is None:
            return None

        size, data_type = key_info
        request = SMCKeyData()
        request.key = _fourcc(key)
        request.keyInfo.dataSize = size
        request.keyInfo.dataType = _fourcc(data_type)
        request.data8 = SMC_CMD_READ_BYTES
        result, response = self._call(request)
        if result or response.result:
            return None

        raw = bytes(response.bytes[:size])
        return SmcReading(key=key, data_type=data_type, raw=raw, value=_decode_value(raw, data_type))

    def iter_keys(self) -> list[str]:
        """Return all SMC key names."""

        count_reading = self.read_key("#KEY")
        if not count_reading or not isinstance(count_reading.value, int):
            return []

        keys: list[str] = []
        for index in range(count_reading.value):
            request = SMCKeyData()
            request.data8 = SMC_CMD_READ_INDEX
            request.data32 = index
            result, response = self._call(request)
            if not result and not response.result:
                keys.append(_fourcc_to_string(response.key))
        return keys

    def _read_key_info(self, key: str) -> tuple[int, str] | None:
        request = SMCKeyData()
        request.key = _fourcc(key)
        request.data8 = SMC_CMD_READ_KEYINFO
        result, response = self._call(request)
        if result or response.result:
            return None
        return response.keyInfo.dataSize, _fourcc_to_string(response.keyInfo.dataType)

    def _call(self, request: SMCKeyData) -> tuple[int, SMCKeyData]:
        if self._iokit is None or not self._connection.value:
            raise SmcError("AppleSMC connection is not open")

        response = SMCKeyData()
        response_size = ctypes.c_size_t(ctypes.sizeof(response))
        result = self._iokit.IOConnectCallStructMethod(
            self._connection.value,
            SMC_SELECTOR,
            ctypes.byref(request),
            ctypes.sizeof(request),
            ctypes.byref(response),
            ctypes.byref(response_size),
        )
        return result, response

    def _configure_iokit(self) -> None:
        self._iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        self._iokit.IOServiceMatching.restype = ctypes.c_void_p
        self._iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        self._iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
        self._iokit.IOServiceOpen.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._iokit.IOServiceOpen.restype = ctypes.c_int
        self._iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
        self._iokit.IOServiceClose.restype = ctypes.c_int
        self._iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        self._iokit.IOObjectRelease.restype = ctypes.c_int
        self._iokit.IOConnectCallStructMethod.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._iokit.IOConnectCallStructMethod.restype = ctypes.c_int

    def _release_service(self) -> None:
        if self._iokit and self._service:
            self._iokit.IOObjectRelease(self._service)
            self._service = 0


_TEMPERATURE_KEYS: tuple[str, ...] | None = None


def read_smc_snapshot() -> SmcSnapshot:
    """Read actual Mac CPU/GPU temperature and fan RPM from AppleSMC."""

    try:
        with SmcClient() as smc:
            temperature_keys = _get_temperature_keys(smc)
            temperature_sensors: dict[str, float] = {}
            for key in temperature_keys:
                reading = smc.read_key(key)
                if isinstance(reading, SmcReading) and isinstance(reading.value, (float, int)):
                    value = float(reading.value)
                    if math.isfinite(value) and -20 <= value <= 130:
                        temperature_sensors[key] = value

            cpu_key, cpu_temperature = _hottest_prefixed(temperature_sensors, CPU_SENSOR_PREFIXES)
            gpu_key, gpu_temperature = _hottest_prefixed(temperature_sensors, GPU_SENSOR_PREFIXES)
            fan_rpms = tuple(_read_fan_rpms(smc))

            if not temperature_sensors and not fan_rpms:
                return SmcSnapshot(error="no readable SMC temperature or fan keys")

            return SmcSnapshot(
                cpu_temperature_c=cpu_temperature,
                gpu_temperature_c=gpu_temperature,
                cpu_sensor_key=cpu_key,
                gpu_sensor_key=gpu_key,
                fan_rpms=fan_rpms,
                temperature_sensors=temperature_sensors,
            )
    except SmcError as exc:
        return SmcSnapshot(error=str(exc))


def _get_temperature_keys(smc: SmcClient) -> tuple[str, ...]:
    global _TEMPERATURE_KEYS
    if _TEMPERATURE_KEYS is not None:
        return _TEMPERATURE_KEYS

    keys: list[str] = []
    for key in smc.iter_keys():
        if not key.startswith("T"):
            continue
        reading = smc.read_key(key)
        if not reading or not isinstance(reading.value, (float, int)):
            continue
        value = float(reading.value)
        if math.isfinite(value) and -20 <= value <= 130:
            keys.append(key)

    _TEMPERATURE_KEYS = tuple(keys)
    return _TEMPERATURE_KEYS


def _read_fan_rpms(smc: SmcClient) -> list[int]:
    fan_count_reading = smc.read_key("FNum")
    if not fan_count_reading or not isinstance(fan_count_reading.value, int):
        return []

    rpms: list[int] = []
    for index in range(fan_count_reading.value):
        reading = smc.read_key(f"F{index}Ac")
        if not reading or not isinstance(reading.value, (float, int)):
            continue
        value = float(reading.value)
        if math.isfinite(value) and value >= 0:
            rpms.append(round(value))
    return rpms


def _hottest_prefixed(sensors: dict[str, float], prefixes: tuple[str, ...]) -> tuple[str | None, float | None]:
    values = [(key, value) for key, value in sensors.items() if key.startswith(prefixes)]
    if not values:
        return None, None
    return max(values, key=lambda item: item[1])


def _fourcc(value: str) -> int:
    encoded = value.encode("latin1")
    if len(encoded) != 4:
        raise ValueError("SMC key/type must be exactly 4 bytes")
    return struct.unpack(">I", encoded)[0]


def _fourcc_to_string(value: int) -> str:
    return struct.pack(">I", value).decode("latin1", "replace")


def _decode_value(raw: bytes, data_type: str) -> float | int | str | None:
    if data_type == "flt " and len(raw) == 4:
        return _decode_smc_float(raw)
    if data_type == "sp78" and len(raw) >= 2:
        return raw[0] + raw[1] / 256.0
    if data_type == "ui8 " and raw:
        return raw[0]
    if data_type == "ui16" and len(raw) >= 2:
        return int.from_bytes(raw[:2], "big")
    if data_type == "ui32" and len(raw) >= 4:
        return int.from_bytes(raw[:4], "big")
    if data_type.startswith("ch"):
        return raw.rstrip(b"\x00").decode("latin1", "replace")
    return None


def _decode_smc_float(raw: bytes) -> float:
    little = struct.unpack("<f", raw)[0]
    big = struct.unpack(">f", raw)[0]
    if math.isfinite(little) and -100 <= little <= 20000:
        return little
    return big

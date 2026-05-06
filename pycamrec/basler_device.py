"""Basler/pypylon camera adapter."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .schemas import CameraConfig


@dataclass(frozen=True)
class GrabbedFrame:
    frame_bytes: bytes
    camera_block_id: int | None
    camera_timestamp_raw: int | None
    camera_timestamp_ns: int | None
    host_receive_perf_counter_ns: int
    host_receive_utc_ns: int
    payload_size_bytes: int


class BaslerCamera:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.pylon: Any = None
        self.genicam: Any = None
        self.factory: Any = None
        self.camera: Any = None
        self.device_info: dict[str, Any] = {}
        self.timestamp_tick_frequency_hz: float | None = None

    def __enter__(self) -> "BaslerCamera":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        self.pylon, self.genicam = _import_pypylon()
        self.factory = self.pylon.TlFactory.GetInstance()
        devices = list(self.factory.EnumerateDevices())
        device = self._select_device(devices)
        self.camera = self.pylon.InstantCamera(self.factory.CreateDevice(device))
        self.camera.Open()
        self.pylon.FeaturePersistence.Load(str(self.cfg.pfs_path), self.camera.GetNodeMap(), False)
        self.camera.MaxNumBuffer = self.cfg.max_num_buffer
        if self.cfg.enable_chunks:
            self._enable_chunks()
        self.timestamp_tick_frequency_hz = self._read_timestamp_tick_frequency()
        self.device_info = self._collect_device_info(device)
        self._validate_camera()

    def start(self) -> None:
        self.camera.StartGrabbing(self.pylon.GrabStrategy_OneByOne)

    def stop(self) -> None:
        if self.camera is not None and self.camera.IsGrabbing():
            self.camera.StopGrabbing()

    def close(self) -> None:
        if self.camera is None:
            return
        try:
            self.stop()
        finally:
            if self.camera.IsOpen():
                self.camera.Close()
            self.camera = None

    def read_temperature_c(self) -> float | None:
        if self.camera is None:
            return None

        for selector_name in ("DeviceTemperatureSelector", "TemperatureSelector"):
            for entry_name in ("Sensor", "Mainboard", "Coreboard", "Fpga", "Camera"):
                try:
                    node_map = self.camera.GetNodeMap()
                except Exception:
                    node_map = None
                if node_map is not None:
                    _try_select_enum(node_map, selector_name, entry_name)
                temperature = _first_float_node(
                    self.camera,
                    ("DeviceTemperature", "BslDeviceTemperature", "TemperatureAbs", "Temperature"),
                )
                if temperature is not None:
                    return temperature

        return _first_float_node(
            self.camera,
            ("DeviceTemperature", "BslDeviceTemperature", "TemperatureAbs", "Temperature"),
        )

    def grab_frame(self) -> GrabbedFrame | None:
        result = self.camera.RetrieveResult(
            self.cfg.grab_timeout_ms,
            self.pylon.TimeoutHandling_Return,
        )
        if result is None:
            return None

        try:
            host_ns = time.perf_counter_ns()
            host_utc_ns = time.time_ns()
            if not result.GrabSucceeded():
                code = getattr(result, "ErrorCode", None)
                description = getattr(result, "ErrorDescription", "")
                raise RuntimeError(f"Basler grab failed: {code} {description}")

            frame_bytes = result.Array.tobytes()
            block_id = _safe_int_attr(result, "BlockID")
            timestamp_raw = _safe_int_attr(result, "TimeStamp")
            timestamp_ns = _timestamp_to_ns(timestamp_raw, self.timestamp_tick_frequency_hz)
            payload_size = len(frame_bytes)
            return GrabbedFrame(
                frame_bytes=frame_bytes,
                camera_block_id=block_id,
                camera_timestamp_raw=timestamp_raw,
                camera_timestamp_ns=timestamp_ns,
                host_receive_perf_counter_ns=host_ns,
                host_receive_utc_ns=host_utc_ns,
                payload_size_bytes=payload_size,
            )
        finally:
            result.Release()

    def _select_device(self, devices: list[Any]) -> Any:
        if not devices:
            raise RuntimeError(
                "No Basler devices found. Run `python -m pycamrec devices` and check "
                "that the CXP GenTL producer path is visible in GENICAM_GENTL64_PATH."
            )
        for device in devices:
            try:
                if device.GetSerialNumber() == self.cfg.serial:
                    return device
            except Exception:
                continue
        serials = []
        for device in devices:
            try:
                serials.append(device.GetSerialNumber())
            except Exception:
                serials.append("<unknown>")
        raise RuntimeError(f"Basler serial {self.cfg.serial!r} not found. Found: {serials}")

    def _collect_device_info(self, device: Any) -> dict[str, Any]:
        camera = self.camera
        info = {
            "serial": _call_or_none(device, "GetSerialNumber"),
            "model": _call_or_none(device, "GetModelName") or _call_or_none(camera.GetDeviceInfo(), "GetModelName"),
            "vendor": _call_or_none(device, "GetVendorName"),
            "device_class": _call_or_none(device, "GetDeviceClass"),
            "friendly_name": _call_or_none(device, "GetFriendlyName"),
            "interface_id": _property_value(device, "InterfaceID"),
            "device_id": _property_value(device, "DeviceID"),
            "device_factory": _property_value(device, "DeviceFactory"),
            "full_name": _property_value(device, "FullName"),
            "width": _node_value(camera, "Width"),
            "height": _node_value(camera, "Height"),
            "pixel_format": _node_value(camera, "PixelFormat"),
            "acquisition_frame_rate": _node_value(camera, "AcquisitionFrameRate"),
            "exposure_time": _node_value(camera, "ExposureTime"),
            "gain": _node_value(camera, "Gain"),
            "chunk_mode_active": _node_value(camera, "ChunkModeActive"),
            "timestamp_tick_frequency_hz": self.timestamp_tick_frequency_hz,
            "device_temperature_c": self.read_temperature_c(),
        }
        return {key: value for key, value in info.items() if value is not None}

    def _validate_camera(self) -> None:
        checks = {
            "Width": (self.cfg.expected_width, _node_value(self.camera, "Width")),
            "Height": (self.cfg.expected_height, _node_value(self.camera, "Height")),
            "PixelFormat": (self.cfg.expected_pixel_format, _node_value(self.camera, "PixelFormat")),
        }
        if _node_value(self.camera, "AcquisitionFrameRate") is not None:
            checks["AcquisitionFrameRate"] = (
                self.cfg.expected_fps,
                float(_node_value(self.camera, "AcquisitionFrameRate")),
            )
        failures = []
        for name, (expected, actual) in checks.items():
            if name == "AcquisitionFrameRate":
                ok = abs(float(expected) - float(actual)) < 0.01
            else:
                ok = str(expected) == str(actual)
            if not ok:
                failures.append(f"{name}: expected {expected!r}, actual {actual!r}")
        if failures and self.cfg.strict_validation:
            raise RuntimeError("Camera validation failed: " + "; ".join(failures))

    def _enable_chunks(self) -> None:
        node_map = self.camera.GetNodeMap()
        _try_set_node(node_map, "ChunkModeActive", True)
        for chunk_name in ("Timestamp", "Framecounter", "CounterValue", "ExposureTime", "Gain"):
            if _try_select_enum(node_map, "ChunkSelector", chunk_name):
                _try_set_node(node_map, "ChunkEnable", True)

    def _read_timestamp_tick_frequency(self) -> float | None:
        for node_name in ("GevTimestampTickFrequency", "TimestampTickFrequency", "BslTimestampTickFrequency"):
            value = _node_value(self.camera, node_name)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None


def _import_pypylon() -> tuple[Any, Any]:
    try:
        import pypylon.genicam as genicam
        import pypylon.pylon as pylon
    except ImportError as exc:
        raise RuntimeError("Install pypylon and Basler pylon to use PyCamRec.") from exc
    return pylon, genicam


def _safe_int_attr(obj: Any, attr: str) -> int | None:
    try:
        value = getattr(obj, attr)
    except Exception:
        method = getattr(obj, f"Get{attr}", None)
        if method is None:
            return None
        try:
            value = method()
        except Exception:
            return None
    if callable(value):
        try:
            value = value()
        except Exception:
            return None
    try:
        return int(value)
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _first_float_node(camera: Any, node_names: tuple[str, ...]) -> float | None:
    for node_name in node_names:
        value = _safe_float(_node_value(camera, node_name))
        if value is not None:
            return value
    return None


def _timestamp_to_ns(timestamp_raw: int | None, tick_frequency_hz: float | None) -> int | None:
    if timestamp_raw is None or tick_frequency_hz in (None, 0):
        return None
    return int(round(timestamp_raw * 1_000_000_000 / tick_frequency_hz))


def _call_or_none(obj: Any, method: str) -> Any:
    try:
        return getattr(obj, method)()
    except Exception:
        return None


def _property_value(obj: Any, key: str) -> Any:
    for method_name in ("GetPropertyValue", "GetProperty"):
        method = getattr(obj, method_name, None)
        if method is None:
            continue
        try:
            value = method(key)
        except Exception:
            continue
        if value not in (None, ""):
            return value
    return None


def _node_value(camera: Any, node_name: str) -> Any:
    try:
        node = getattr(camera, node_name)
        return node.GetValue()
    except Exception:
        pass

    try:
        node = camera.GetNodeMap().GetNode(node_name)
    except Exception:
        return None
    if node is None:
        return None
    for method_name in ("GetValue", "ToString"):
        method = getattr(node, method_name, None)
        if method is None:
            continue
        try:
            return method()
        except Exception:
            continue
    return None


def _try_select_enum(node_map: Any, selector_name: str, entry_name: str) -> bool:
    try:
        selector = node_map.GetNode(selector_name)
        if selector is None:
            return False
        selector.FromString(entry_name)
        return True
    except Exception:
        return False


def _try_set_node(node_map: Any, node_name: str, value: Any) -> bool:
    try:
        node = node_map.GetNode(node_name)
        if node is None:
            return False
        if hasattr(node, "SetValue"):
            node.SetValue(value)
        elif hasattr(node, "FromString"):
            node.FromString(str(value))
        else:
            return False
        return True
    except Exception:
        return False

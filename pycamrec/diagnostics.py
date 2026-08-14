"""Runtime diagnostics for pylon/pypylon device discovery."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class PylonDiagnosticReport:
    python: str
    executable: str
    pypylon_version: str
    genicam_gentl64_path: str
    genicam_gentl32_path: str
    transport_layers: list[dict[str, Any]]
    interfaces: list[dict[str, Any]]
    devices: list[dict[str, Any]]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CameraCapabilityReport:
    serial: str
    model: str
    pixel_formats: list[str]
    width: dict[str, Any]
    height: dict[str, Any]
    acquisition_frame_rate: dict[str, Any]
    device_info: dict[str, Any]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_pylon() -> PylonDiagnosticReport:
    try:
        import pypylon.pylon as pylon
    except Exception as exc:
        return PylonDiagnosticReport(
            python=sys.version,
            executable=sys.executable,
            pypylon_version="",
            genicam_gentl64_path=os.environ.get("GENICAM_GENTL64_PATH", ""),
            genicam_gentl32_path=os.environ.get("GENICAM_GENTL32_PATH", ""),
            transport_layers=[],
            interfaces=[],
            devices=[],
            error=f"Could not import pypylon.pylon: {exc!r}",
        )

    factory = pylon.TlFactory.GetInstance()
    tls: list[dict[str, Any]] = []
    interfaces: list[dict[str, Any]] = []
    devices: list[dict[str, Any]] = []
    error = ""

    try:
        for tl in factory.EnumerateTls():
            tls.append(_info_to_dict(tl))
            if "CXP" in _safe_info_value(tl, "GetFriendlyName") or "CXP" in _safe_info_value(tl, "GetDeviceClass"):
                interfaces.extend(_inspect_interfaces(factory, tl))
    except Exception as exc:
        error = f"EnumerateTls failed: {exc!r}"

    try:
        for device in factory.EnumerateDevices():
            devices.append(_info_to_dict(device))
    except Exception as exc:
        error = f"{error}; EnumerateDevices failed: {exc!r}".strip("; ")

    return PylonDiagnosticReport(
        python=sys.version,
        executable=sys.executable,
        pypylon_version=getattr(pylon, "__version__", ""),
        genicam_gentl64_path=os.environ.get("GENICAM_GENTL64_PATH", ""),
        genicam_gentl32_path=os.environ.get("GENICAM_GENTL32_PATH", ""),
        transport_layers=tls,
        interfaces=interfaces,
        devices=devices,
        error=error,
    )


def inspect_camera_capabilities(serial: str | None = None) -> CameraCapabilityReport:
    try:
        import pypylon.pylon as pylon
    except Exception as exc:
        return CameraCapabilityReport(
            serial=serial or "",
            model="",
            pixel_formats=[],
            width={},
            height={},
            acquisition_frame_rate={},
            device_info={},
            error=f"Could not import pypylon.pylon: {exc!r}",
        )

    camera = None
    try:
        factory = pylon.TlFactory.GetInstance()
        devices = list(factory.EnumerateDevices())
        device = _select_device(devices, serial)
        camera = pylon.InstantCamera(factory.CreateDevice(device))
        camera.Open()
        node_map = camera.GetNodeMap()
        device_info = _info_to_dict(device)
        return CameraCapabilityReport(
            serial=_safe_info_value(device, "GetSerialNumber"),
            model=_safe_info_value(device, "GetModelName"),
            pixel_formats=_enum_symbolics(node_map, "PixelFormat"),
            width=_node_limits(node_map, "Width"),
            height=_node_limits(node_map, "Height"),
            acquisition_frame_rate=_first_node_limits(
                node_map,
                ("AcquisitionFrameRate", "AcquisitionFrameRateAbs"),
            ),
            device_info=device_info,
        )
    except Exception as exc:
        return CameraCapabilityReport(
            serial=serial or "",
            model="",
            pixel_formats=[],
            width={},
            height={},
            acquisition_frame_rate={},
            device_info={},
            error=repr(exc),
        )
    finally:
        try:
            if camera is not None and camera.IsOpen():
                camera.Close()
        except Exception:
            pass


def _inspect_interfaces(factory: Any, tl_info: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        tl = factory.CreateTl(tl_info)
    except Exception as exc:
        return [{"transport_layer": _safe_info_value(tl_info, "GetFriendlyName"), "error": f"CreateTl failed: {exc!r}"}]

    try:
        interface_infos = tl.EnumerateInterfaces()
    except Exception as exc:
        return [{"transport_layer": _safe_info_value(tl_info, "GetFriendlyName"), "error": f"EnumerateInterfaces failed: {exc!r}"}]

    for interface_info in interface_infos:
        item = _info_to_dict(interface_info)
        try:
            interface = tl.CreateInterface(interface_info)
            try:
                interface.Open()
                item["Open"] = True
                item["Devices"] = [_info_to_dict(device) for device in interface.EnumerateDevices()]
            except Exception as exc:
                item["Open"] = False
                item["OpenError"] = repr(exc)
            finally:
                try:
                    if interface.IsOpen():
                        interface.Close()
                except Exception:
                    pass
        except Exception as exc:
            item["Open"] = False
            item["OpenError"] = repr(exc)
        results.append(item)
    return results


def _info_to_dict(info: Any) -> dict[str, Any]:
    method_names = [
        "GetDeviceClass",
        "GetDeviceFactory",
        "GetDeviceID",
        "GetDeviceVersion",
        "GetFriendlyName",
        "GetFullName",
        "GetInterfaceID",
        "GetModelName",
        "GetSerialNumber",
        "GetTLType",
        "GetVendorName",
    ]
    data: dict[str, Any] = {}
    for method_name in method_names:
        method = getattr(info, method_name, None)
        if method is None:
            continue
        try:
            value = method()
        except Exception:
            continue
        if value not in (None, ""):
            data[method_name[3:]] = value
    return data


def _safe_info_value(info: Any, method_name: str) -> str:
    try:
        return str(getattr(info, method_name)())
    except Exception:
        return ""


def _select_device(devices: list[Any], serial: str | None) -> Any:
    if not devices:
        raise RuntimeError("No Basler devices found.")
    if serial:
        for device in devices:
            if _safe_info_value(device, "GetSerialNumber") == serial:
                return device
        raise RuntimeError(f"Basler serial {serial!r} not found.")
    return devices[0]


def _enum_symbolics(node_map: Any, node_name: str) -> list[str]:
    try:
        node = node_map.GetNode(node_name)
        return [str(item) for item in node.Symbolics]
    except Exception:
        return []


def _first_node_limits(node_map: Any, node_names: tuple[str, ...]) -> dict[str, Any]:
    for node_name in node_names:
        limits = _node_limits(node_map, node_name)
        if limits:
            return limits
    return {}


def _node_limits(node_map: Any, node_name: str) -> dict[str, Any]:
    try:
        node = node_map.GetNode(node_name)
    except Exception:
        return {}
    if node is None:
        return {}
    result: dict[str, Any] = {}
    for key, method_name in (("value", "GetValue"), ("min", "GetMin"), ("max", "GetMax"), ("increment", "GetInc")):
        method = getattr(node, method_name, None)
        if method is None:
            continue
        try:
            result[key] = method()
        except Exception:
            pass
    value = _float_or_none(result.get("value"))
    minimum = _float_or_none(result.get("min"))
    maximum = _float_or_none(result.get("max"))
    if value is not None:
        in_range = (minimum is None or value >= minimum) and (maximum is None or value <= maximum)
        result["value_in_range"] = in_range
        result["safe_value"] = result.get("value") if in_range else result.get("max", result.get("value"))
    return result


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

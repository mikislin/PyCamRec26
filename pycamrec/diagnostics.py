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

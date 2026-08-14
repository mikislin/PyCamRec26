"""Camera backend interface and factory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .schemas import CameraConfig


SUPPORTED_CAMERA_MAKES = ("basler", "basler_cxp", "basler_usb")


@dataclass(frozen=True)
class GrabbedFrame:
    frame_bytes: bytes
    camera_block_id: int | None
    camera_timestamp_raw: int | None
    camera_timestamp_ns: int | None
    host_receive_perf_counter_ns: int
    host_receive_utc_ns: int
    payload_size_bytes: int


class CameraBackend(Protocol):
    device_info: dict[str, Any]
    timestamp_tick_frequency_hz: float | None

    def __enter__(self) -> "CameraBackend":
        ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        ...

    def open(self) -> None:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def close(self) -> None:
        ...

    def read_temperature_c(self) -> float | None:
        ...

    def grab_frame(self) -> GrabbedFrame | None:
        ...


def create_camera_backend(cfg: CameraConfig) -> CameraBackend:
    make = normalize_camera_make(cfg.make)
    if make in {"basler", "basler_cxp", "basler_usb"}:
        from .basler_device import BaslerCamera

        return BaslerCamera(cfg)
    choices = ", ".join(SUPPORTED_CAMERA_MAKES)
    raise ValueError(f"Unsupported camera.make {cfg.make!r}. Supported values: {choices}.")


def normalize_camera_make(value: str) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def supported_camera_makes() -> tuple[str, ...]:
    return SUPPORTED_CAMERA_MAKES

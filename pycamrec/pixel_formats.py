"""Small pixel-format helpers shared by recording, preview, and reports."""

from __future__ import annotations


def normalize_pixel_format(pixel_format: str) -> str:
    return pixel_format.replace("_", "").replace(" ", "").lower()


def bayer_pattern(pixel_format: str) -> str | None:
    normalized = normalize_pixel_format(pixel_format)
    for pattern in ("bg", "gb", "gr", "rg"):
        if normalized.startswith(f"bayer{pattern}"):
            return pattern
    return None


def is_bayer_pixel_format(pixel_format: str) -> bool:
    return bayer_pattern(pixel_format) is not None


def is_mono_pixel_format(pixel_format: str) -> bool:
    return normalize_pixel_format(pixel_format) in {"mono8", "mono10", "mono12"}


def is_rgb_pixel_format(pixel_format: str) -> bool:
    return normalize_pixel_format(pixel_format) in {"rgb8", "bgr8"}


def bytes_per_pixel(pixel_format: str) -> int:
    normalized = normalize_pixel_format(pixel_format)
    if normalized in {"mono8"} or is_bayer_pixel_format(pixel_format):
        return 1
    if normalized in {"rgb8", "bgr8"}:
        return 3
    if normalized in {"ycbcr422", "ycbcr4228", "ycbcr4228cbYcrY"}:
        return 2
    if normalized in {"mono10", "mono12", "bayerbg10", "bayergb10", "bayergr10", "bayerrg10"}:
        return 2
    if normalized in {"bayerbg10p", "bayergb10p", "bayergr10p", "bayerrg10p"}:
        raise ValueError(f"Packed 10-bit pixel format {pixel_format!r} is not supported by the writer yet.")
    raise ValueError(f"Unsupported camera pixel format for byte-size estimation: {pixel_format!r}.")


def ffmpeg_raw_pix_fmt(pixel_format: str) -> str:
    normalized = normalize_pixel_format(pixel_format)
    if normalized == "mono8" or is_bayer_pixel_format(pixel_format):
        return "gray"
    if normalized == "rgb8":
        return "rgb24"
    if normalized == "bgr8":
        return "bgr24"
    raise ValueError(f"No FFmpeg rawvideo input mapping for camera pixel format {pixel_format!r}.")


def channel_semantics(pixel_format: str) -> str:
    normalized = normalize_pixel_format(pixel_format)
    if normalized == "mono8":
        return "mono_luma"
    pattern = bayer_pattern(pixel_format)
    if pattern is not None:
        return f"raw_bayer_{pattern}"
    if normalized == "rgb8":
        return "rgb"
    if normalized == "bgr8":
        return "bgr"
    return "unsupported_or_not_yet_configured"

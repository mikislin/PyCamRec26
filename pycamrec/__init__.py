"""PyCamRec scientific camera recording package."""

from .config import load_config
from .schemas import CameraConfig, MetadataConfig, PreviewConfig, PyCamRecConfig, SessionConfig, WriterConfig

__all__ = [
    "CameraConfig",
    "MetadataConfig",
    "PreviewConfig",
    "PyCamRecConfig",
    "SessionConfig",
    "WriterConfig",
    "load_config",
]

__version__ = "0.2.0rc2"
__release_stage__ = "pre-release"

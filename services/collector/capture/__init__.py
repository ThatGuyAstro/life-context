"""Portable multi-monitor screen capture for the collector service.

The package has no audio/video behavior.  Use :class:`ScreenCaptureService`
with a real backend or inject a backend implementing :class:`CaptureBackend`
for tests and other platforms.
"""

from .backends import (
    CaptureBackend,
    CaptureBackendError,
    CaptureUnavailableError,
    MssCaptureBackend,
    MssPillowCaptureBackend,
    PythonFallbackCaptureBackend,
    create_default_backend,
)
from .images import ImageDependencyError, encode_jpeg, generate_contact_sheet
from .models import (
    CaptureSequence,
    EncodedImage,
    Monitor,
    MonitorCapture,
    RawFrame,
)
from .service import CaptureConfig, NoMonitorsError, ScreenCaptureService
from .windows import WindowsCaptureBackend, WindowsScreenCaptureBackend

__all__ = [
    "CaptureBackend",
    "CaptureBackendError",
    "CaptureConfig",
    "CaptureSequence",
    "CaptureUnavailableError",
    "EncodedImage",
    "ImageDependencyError",
    "Monitor",
    "MonitorCapture",
    "MssCaptureBackend",
    "MssPillowCaptureBackend",
    "NoMonitorsError",
    "PythonFallbackCaptureBackend",
    "RawFrame",
    "ScreenCaptureService",
    "WindowsCaptureBackend",
    "WindowsScreenCaptureBackend",
    "create_default_backend",
    "encode_jpeg",
    "generate_contact_sheet",
]

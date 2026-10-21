"""配置自检.

对外只有两件事: ``run_probe`` 产出事件流, ``supported_sections`` 说明哪些分区可测.
"""

from .base import ProbeContext, ProbeEvent, ProbeLevel, Recorder, describe_error
from .registry import PROBES, is_supported, run_probe, supported_sections

__all__ = [
    "PROBES",
    "ProbeContext",
    "ProbeEvent",
    "ProbeLevel",
    "Recorder",
    "describe_error",
    "is_supported",
    "run_probe",
    "supported_sections",
]

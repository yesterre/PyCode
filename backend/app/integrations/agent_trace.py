"""Application Trace integration surface.

The legacy ``map_agent_trace`` projection intentionally no longer exists: it
persisted Internal Trace names directly. New Agent runs use the validated v1
Backend projection exclusively.
"""

from backend.app.core.application_trace import ApplicationTraceNormalizer


__all__ = ["ApplicationTraceNormalizer"]

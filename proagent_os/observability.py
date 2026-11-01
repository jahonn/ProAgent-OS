"""Optional OpenTelemetry spans.

The default install does not import ``opentelemetry``. ``span()`` is a no-op
until ``PROAGENT_OTEL_ENABLED`` is true and the ``otel`` extra is installed
(``pip install 'proagent-os[otel]'``). A missing package logs a warning and
keeps the no-op path; startup does not require a collector.

Spans do not carry prompts, KB query text, API keys, or Authorization headers.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional

from proagent_os.config import get_settings

logger = logging.getLogger("proagent.otel")

SPAN_HTTP_REQUEST = "http.request"
SPAN_KB_SEARCH = "kb.search"
SPAN_RECOMMENDATION_APPROVE = "recommendation.approve"
SPAN_RECOMMENDATION_EXECUTE = "recommendation.execute"
SPAN_LLM_CHAT = "llm.chat"

_UNSET = object()
_tracer: Any = _UNSET
_provider: Any = None


def reset_observability() -> None:
    """Drop the cached tracer. Does not import OpenTelemetry."""
    global _tracer, _provider
    _tracer = _UNSET
    _provider = None


def shutdown_observability() -> None:
    """Flush and stop a tracer provider, if one was started."""
    global _provider
    provider = _provider
    reset_observability()
    if provider is None:
        return
    shutdown = getattr(provider, "shutdown", None)
    if shutdown is not None:
        shutdown()


def get_tracer() -> Any:
    """Return a tracer, or ``None`` when spans are disabled or unavailable.

    ``opentelemetry`` is imported only after the env flag is on.
    """
    global _tracer
    if not get_settings().otel_enabled:
        return None
    if _tracer is not _UNSET:
        return _tracer
    try:
        _tracer = _configure_tracer()
    except ImportError as exc:
        logger.warning(
            "PROAGENT_OTEL_ENABLED is set but OpenTelemetry could not be imported (%s). "
            "Spans are no-ops. Install with: pip install 'proagent-os[otel]'",
            exc,
        )
        _tracer = None
    return _tracer


def _configure_tracer() -> Any:
    """Import the SDK and install a process-wide tracer provider.

    Raises ``ImportError`` when the optional packages are not installed.
    """
    global _provider
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    settings = get_settings()
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    if settings.otel_exporter == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif settings.otel_exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        kwargs = {}
        endpoint = settings.otel_exporter_otlp_endpoint.strip()
        if endpoint:
            kwargs["endpoint"] = endpoint
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**kwargs)))
    trace.set_tracer_provider(provider)
    _provider = provider
    logger.info(
        "OpenTelemetry spans enabled exporter=%s service=%s endpoint_set=%s",
        settings.otel_exporter,
        settings.otel_service_name,
        bool(settings.otel_exporter_otlp_endpoint.strip()),
    )
    return trace.get_tracer("proagent_os")


def _apply_attributes(current: Any, attributes: Optional[Mapping[str, Any]]) -> None:
    if not attributes:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float, str)):
            current.set_attribute(key, value)
        else:
            current.set_attribute(key, str(value))


@contextmanager
def span(
    name: str,
    attributes: Optional[Mapping[str, Any]] = None,
) -> Iterator[Any]:
    """Start a span, or yield ``None`` when tracing is off."""
    tracer = get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as current:
        _apply_attributes(current, attributes)
        yield current


async def trace_http_request(request: Any, call_next: Any) -> Any:
    """HTTP middleware span. No-op unless OpenTelemetry is enabled."""
    with span(
        SPAN_HTTP_REQUEST,
        {"http.method": request.method, "http.path": request.url.path},
    ) as current:
        response = await call_next(request)
        if current is not None:
            current.set_attribute("http.status_code", response.status_code)
        return response

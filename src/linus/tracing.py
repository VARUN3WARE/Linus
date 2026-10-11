"""neatlogs setup, plus `step()` for naming pipeline stages in the trace."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import neatlogs

from linus.config import Settings


def init_tracing(settings: Settings) -> None:
    kwargs = {
        "api_key": settings.neatlogs_api_key,
        "workflow_name": settings.neatlogs_workflow_name,
    }
    if settings.neatlogs_endpoint:
        kwargs["endpoint"] = settings.neatlogs_endpoint
    neatlogs.init(**kwargs)


def shutdown_tracing() -> None:
    neatlogs.flush()
    neatlogs.shutdown()


def annotate(span: Any, **attrs: Any) -> None:
    for key, value in attrs.items():
        if not isinstance(value, (str, int, float, bool)):
            value = json.dumps(value, default=str)[:20_000]
        span.set_attribute(f"linus.{key}", value)


@contextmanager
def step(name: str, kind: str = "CHAIN", **attrs: Any) -> Iterator[Any]:
    """A user-facing span. trace() spans are internal by default, so un-hide it."""
    with neatlogs.trace(name=name, kind=kind) as span:
        span.set_attribute("neatlogs.internal", False)
        annotate(span, **attrs)
        yield span

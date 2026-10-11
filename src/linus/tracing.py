"""neatlogs setup. Call init_tracing() once at process start."""

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

from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    """Load the full Agent app lazily so SCAT-only startup avoids Agent imports."""
    if name == "app":
        from service.service import app

        globals()[name] = app
        return app
    raise AttributeError(name)

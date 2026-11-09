"""Public registry exports are loaded only when the application asks for them."""

from importlib import import_module

__all__ = [
    "DEFAULT_AGENT",
    "AgentGraph",
    "AgentGraphLike",
    "get_agent",
    "get_all_agent_info",
    "get_public_agent",
    "load_agent",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    value = getattr(import_module("agents.agents"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

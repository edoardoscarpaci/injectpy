"""Public re-exports for the providify.decorator sub-package.

Centralises all decorator imports so callers can write:

    from providify.decorator import Component, Singleton, Named, Priority

instead of knowing which internal sub-module each decorator lives in.

Thread safety:  ✅ Module-level imports — no mutable state.
Async safety:   ✅ Pure imports — no async state.
"""

from __future__ import annotations

from .lifecycle import PostConstruct, PreDestroy
from .scope import (
    Component,
    Inheritable,
    Named,
    Priority,
    Provider,
    RequestScoped,
    SessionScoped,
    Singleton,
)

__all__ = [
    # Scope decorators
    "Component",
    "Singleton",
    "RequestScoped",
    "SessionScoped",
    # Modifier decorators
    "Named",
    "Priority",
    "Inheritable",
    # Factory decorator
    "Provider",
    # Lifecycle decorators
    "PostConstruct",
    "PreDestroy",
]

"""Shared pytest fixtures for the injectable test suite.

Every test module imports from this file automatically (pytest discovers it).
The two fixtures here handle the two isolation concerns:

1. ``container`` — a fresh DIContainer *instance* per test (no global state).
   Most tests use this to avoid touching the global singleton at all.

2. ``reset_global`` — autouse fixture that wipes DIContainer._global before
   and after every test. Needed because a handful of tests exercise
   DIContainer.current() / DIContainer.scoped(), which write to the global.
   Without this, test ordering could affect outcomes.

Thread safety:  ✅ Each test gets its own container instance.
                The global reset uses DIContainer.reset() which is lock-protected.
Async safety:   ✅ pytest-asyncio runs each async test in its own event loop.
"""
from __future__ import annotations

import pytest

from injectable.container import DIContainer


@pytest.fixture
def container() -> DIContainer:
    """Return a fresh, empty DIContainer instance.

    DESIGN: returns a plain DIContainer() — not the global singleton.
    This is the preferred isolation approach: most tests don't need the global
    at all, so creating a private instance is the cleanest option.

    Returns:
        An empty DIContainer with no bindings and no cached instances.
    """
    return DIContainer()


@pytest.fixture(autouse=True)
def reset_global_container() -> None:
    """Reset DIContainer._global before and after every test.

    autouse=True — runs for every test without needing an explicit parameter.

    DESIGN: yield-based so both setup (before test) and teardown (after test)
    are guaranteed to run, even if the test raises an exception.

    Edge cases:
        - If a test calls DIContainer.current(), this fixture ensures the next
          test starts with a fresh global.
        - reset() is idempotent — safe to call even when _global is already None.
    """
    DIContainer.reset()
    yield
    DIContainer.reset()
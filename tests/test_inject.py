"""Unit tests for Inject[T], InjectInstances[T], and optional injection.

Covered:
    - Inject[T]: constructor parameter gets its dependency resolved automatically
    - InjectInstances[T]: parameter receives a list of all matching bindings
    - Inject(T, qualifier=...): named qualifier forwarded to container.get()
    - Inject(T, optional=True): returns None when no binding is found
    - Inject(T, optional=False, default): raises LookupError when binding is missing
    - Annotated[T, InjectMeta] vs plain T: only annotated hints get special treatment

DESIGN NOTE: Inject[T] and InjectInstances[T] are pure type-hint constructs —
they expand to Annotated[T, InjectMeta(...)]. The container detects the marker
in _resolve_hint_sync and acts on the metadata.
"""
from __future__ import annotations

import pytest

from injectable.container import DIContainer
from injectable.decorator.scope import Component, Provider
from injectable.type import Inject, InjectInstances


# ─────────────────────────────────────────────────────────────────
#  Domain types
# ─────────────────────────────────────────────────────────────────

class Storage:
    """Abstract-style interface for storage backends."""


@Component
class FileStorage(Storage):
    """Concrete filesystem-based storage."""


@Component(qualifier="cloud")
class CloudStorage(Storage):
    """Concrete cloud-based storage — qualifier='cloud'."""


@Component(priority=1)
class LowPriorityStorage(Storage):
    """Low-priority storage — used in get_all ordering tests."""


@Component(priority=2)
class HigherPriorityStorage(Storage):
    """Higher-priority storage — comes after LowPriorityStorage in get_all."""


# ─────────────────────────────────────────────────────────────────
#  Inject[T] tests
# ─────────────────────────────────────────────────────────────────

class TestInject:
    """Tests for the Inject[T] annotation-based injection."""

    def test_inject_parameter_receives_instance(self, container: DIContainer) -> None:
        """A constructor parameter typed Inject[Storage] should be automatically injected."""
        @Component
        class Service:
            def __init__(self, store: Inject[Storage]) -> None:  # type: ignore[valid-type]
                self.store = store

        container.bind(Storage, FileStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, FileStorage)

    def test_inject_with_qualifier_selects_named_binding(self, container: DIContainer) -> None:
        """Inject(T, qualifier='cloud') should resolve only the 'cloud' qualified binding."""
        @Component
        class Service:
            def __init__(
                self,
                store: Inject(Storage, qualifier="cloud"),  # type: ignore[valid-type]
            ) -> None:
                self.store = store

        container.bind(Storage, FileStorage)
        container.bind(Storage, CloudStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, CloudStorage)

    def test_inject_with_priority_selects_exact_priority(self, container: DIContainer) -> None:
        """Inject(T, priority=2) should resolve only the binding with priority=2."""
        @Component
        class Service:
            def __init__(
                self,
                store: Inject(Storage, priority=2),  # type: ignore[valid-type]
            ) -> None:
                self.store = store

        container.bind(Storage, LowPriorityStorage)
        container.bind(Storage, HigherPriorityStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, HigherPriorityStorage)

    def test_inject_optional_returns_none_when_absent(self, container: DIContainer) -> None:
        """Inject(T, optional=True) should inject None when no binding is registered."""
        @Component
        class Service:
            def __init__(
                self,
                store: Inject(Storage, optional=True),  # type: ignore[valid-type]
            ) -> None:
                self.store = store

        container.register(Service)   # Storage is NOT registered

        svc = container.get(Service)

        assert svc.store is None

    def test_inject_optional_false_raises_when_absent(self, container: DIContainer) -> None:
        """Inject(T, optional=False) should raise LookupError when binding is missing."""
        @Component
        class Service:
            def __init__(
                self,
                # optional=False is the default — fail-fast
                store: Inject(Storage, optional=False),  # type: ignore[valid-type]
            ) -> None:
                self.store = store

        container.register(Service)   # Storage is NOT registered

        with pytest.raises(LookupError):
            container.get(Service)

    def test_plain_type_annotation_also_resolves(self, container: DIContainer) -> None:
        """Plain type annotations (without Inject[]) are also auto-injected
        when a matching binding exists — Inject[] is only needed for extra options.
        """
        @Component
        class Service:
            def __init__(self, store: Storage) -> None:
                self.store = store

        container.bind(Storage, FileStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, FileStorage)


# ─────────────────────────────────────────────────────────────────
#  InjectInstances[T] tests
# ─────────────────────────────────────────────────────────────────

class TestInjectInstances:
    """Tests for the InjectInstances[T] multi-binding injection."""

    def test_receives_all_matching_bindings_as_list(self, container: DIContainer) -> None:
        """InjectInstances[T] should inject a list containing every bound implementation."""
        @Component
        class Service:
            def __init__(self, stores: InjectInstances[Storage]) -> None:  # type: ignore[valid-type]
                self.stores = stores

        container.bind(Storage, FileStorage)
        container.bind(Storage, CloudStorage)
        container.register(Service)

        svc = container.get(Service)

        assert len(svc.stores) == 2
        types = {type(s) for s in svc.stores}
        assert FileStorage in types
        assert CloudStorage in types

    def test_list_is_sorted_by_priority(self, container: DIContainer) -> None:
        """InjectInstances should return implementations ordered by ascending priority."""
        @Component
        class Service:
            def __init__(self, stores: InjectInstances[Storage]) -> None:  # type: ignore[valid-type]
                self.stores = stores

        container.bind(Storage, LowPriorityStorage)     # priority=1
        container.bind(Storage, HigherPriorityStorage)  # priority=2
        container.register(Service)

        svc = container.get(Service)

        # Sorted ascending: lowest priority number first
        assert isinstance(svc.stores[0], LowPriorityStorage)
        assert isinstance(svc.stores[1], HigherPriorityStorage)

    def test_inject_instances_with_qualifier(self, container: DIContainer) -> None:
        """InjectInstances(T, qualifier=...) should filter by qualifier."""
        @Component
        class Service:
            def __init__(
                self,
                stores: InjectInstances(Storage, qualifier="cloud"),  # type: ignore[valid-type]
            ) -> None:
                self.stores = stores

        container.bind(Storage, FileStorage)    # qualifier=None
        container.bind(Storage, CloudStorage)   # qualifier="cloud"
        container.register(Service)

        svc = container.get(Service)

        assert len(svc.stores) == 1
        assert isinstance(svc.stores[0], CloudStorage)

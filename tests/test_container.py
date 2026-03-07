"""Unit tests for DIContainer core API.

Covers the registration phase (bind, register, provide) and the resolution
phase (get, get_all), plus qualifier/priority filtering and the two-phase
validation model (_validated flag).

Covered:
    - bind() / register() / provide()
    - get(): basic resolution, qualifier filter, priority filter, LookupError
    - get_all(): multi-binding resolution ordered by priority
    - _validated reset when new bindings are added after first resolution
    - DIContainer.current() returns the same global instance
    - DIContainer.scoped() installs a fresh container as global and restores
      the previous one on exit
    - DIContainer as a context manager calls shutdown() on __exit__
"""
from __future__ import annotations

import pytest

from injectable.container import DIContainer
from injectable.decorator.scope import Component, Provider, Singleton
from injectable.exceptions import ClassBindingNotDecoratedError


# ─────────────────────────────────────────────────────────────────
#  Domain fixtures
# ─────────────────────────────────────────────────────────────────

class Notifier:
    """Abstract-style interface."""


@Component
class EmailNotifier(Notifier):
    pass


@Singleton
class SMSNotifier(Notifier):
    pass


@Component(qualifier="push", priority=1)
class PushNotifier(Notifier):
    pass


@Component(qualifier="push", priority=2)
class PushFallbackNotifier(Notifier):
    pass


# ─────────────────────────────────────────────────────────────────
#  Registration tests
# ─────────────────────────────────────────────────────────────────

class TestRegistration:
    """Tests for bind(), register(), and provide()."""

    def test_bind_adds_class_binding(self, container: DIContainer) -> None:
        """bind(Interface, Impl) should add one ClassBinding to _bindings."""
        container.bind(Notifier, EmailNotifier)

        assert len(container._bindings) == 1

    def test_register_adds_self_binding(self, container: DIContainer) -> None:
        """register(Cls) should add a self-binding (interface == implementation)."""
        container.register(EmailNotifier)

        binding = container._bindings[0]
        assert binding.interface is EmailNotifier
        assert binding.implementation is EmailNotifier  # type: ignore[union-attr]

    def test_register_raises_for_undecorated_class(self, container: DIContainer) -> None:
        """register() should raise TypeError when the class has no DI decorator."""
        class Bare:
            pass

        with pytest.raises(TypeError):
            container.register(Bare)

    def test_provide_adds_provider_binding(self, container: DIContainer) -> None:
        """provide(fn) should wrap the function in a ProviderBinding."""
        @Provider
        def make_notifier() -> Notifier:
            return EmailNotifier()

        container.provide(make_notifier)

        assert len(container._bindings) == 1

    def test_adding_binding_resets_validated_flag(self, container: DIContainer) -> None:
        """Adding a new binding after resolution must reset _validated so the
        next get() re-runs validate_bindings() over the full updated registry.
        """
        container.bind(Notifier, EmailNotifier)
        container.get(Notifier)         # triggers validate_bindings() → _validated=True

        assert container._validated is True

        container.bind(Notifier, SMSNotifier)   # new binding added

        # _validated must be reset — next get() will re-validate
        assert container._validated is False


# ─────────────────────────────────────────────────────────────────
#  Resolution tests — get()
# ─────────────────────────────────────────────────────────────────

class TestGet:
    """Tests for the sync get() resolution path."""

    def test_resolves_interface_to_implementation(self, container: DIContainer) -> None:
        """get(Interface) should return an instance of the bound implementation."""
        container.bind(Notifier, EmailNotifier)

        result = container.get(Notifier)

        assert isinstance(result, EmailNotifier)

    def test_resolves_registered_class(self, container: DIContainer) -> None:
        """get(Cls) after register() should return an instance of that class."""
        container.register(EmailNotifier)

        result = container.get(EmailNotifier)

        assert isinstance(result, EmailNotifier)

    def test_resolves_provider_function(self, container: DIContainer) -> None:
        """get() should call the @Provider function and return its result."""
        @Provider
        def make_notifier() -> Notifier:
            return EmailNotifier()

        container.provide(make_notifier)
        result = container.get(Notifier)

        assert isinstance(result, EmailNotifier)

    def test_raises_lookup_error_for_unregistered_type(self, container: DIContainer) -> None:
        """get() must raise LookupError when no binding matches the requested type."""
        with pytest.raises(LookupError, match="No binding found"):
            container.get(Notifier)

    def test_raises_for_async_provider_on_sync_get(self, container: DIContainer) -> None:
        """get() must raise RuntimeError when the best match is an async provider."""
        @Provider
        async def make_async() -> Notifier:
            return EmailNotifier()

        container.provide(make_async)

        with pytest.raises(RuntimeError, match="async provider"):
            container.get(Notifier)

    def test_qualifier_filter_selects_matching_binding(self, container: DIContainer) -> None:
        """get(T, qualifier=...) must return only the binding with that qualifier."""
        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, PushNotifier)

        result = container.get(Notifier, qualifier="push")

        assert isinstance(result, PushNotifier)

    def test_qualifier_filter_raises_when_no_match(self, container: DIContainer) -> None:
        """get(T, qualifier='missing') raises LookupError when qualifier is absent."""
        container.bind(Notifier, EmailNotifier)

        with pytest.raises(LookupError):
            container.get(Notifier, qualifier="does-not-exist")

    def test_priority_filter_selects_exact_priority(self, container: DIContainer) -> None:
        """get(T, priority=N) returns the binding with that exact priority value."""
        container.bind(Notifier, PushNotifier)          # priority=1
        container.bind(Notifier, PushFallbackNotifier)  # priority=2

        result = container.get(Notifier, qualifier="push", priority=2)

        assert isinstance(result, PushFallbackNotifier)

    def test_lowest_priority_wins_without_filter(self, container: DIContainer) -> None:
        """Without a priority filter, min(priority) wins — lower number = higher priority."""
        container.bind(Notifier, PushNotifier)          # priority=1
        container.bind(Notifier, PushFallbackNotifier)  # priority=2

        # Both have qualifier="push", so both are candidates.
        # Priority 1 < 2 → PushNotifier wins.
        result = container.get(Notifier, qualifier="push")

        assert isinstance(result, PushNotifier)


# ─────────────────────────────────────────────────────────────────
#  Resolution tests — get_all()
# ─────────────────────────────────────────────────────────────────

class TestGetAll:
    """Tests for the sync get_all() multi-binding resolution path."""

    def test_returns_all_matching_bindings(self, container: DIContainer) -> None:
        """get_all() should return every binding whose interface matches."""
        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SMSNotifier)

        results = container.get_all(Notifier)

        # Both implementations should be present
        assert len(results) == 2
        types = {type(r) for r in results}
        assert EmailNotifier in types
        assert SMSNotifier in types

    def test_results_sorted_by_priority_ascending(self, container: DIContainer) -> None:
        """get_all() should return instances sorted by ascending priority (lowest first)."""
        container.bind(Notifier, PushNotifier)          # priority=1
        container.bind(Notifier, PushFallbackNotifier)  # priority=2

        results = container.get_all(Notifier, qualifier="push")

        # Lowest priority number comes first
        assert isinstance(results[0], PushNotifier)
        assert isinstance(results[1], PushFallbackNotifier)

    def test_raises_when_no_bindings_found(self, container: DIContainer) -> None:
        """get_all() raises LookupError when no binding matches the type."""
        with pytest.raises(LookupError, match="No bindings found"):
            container.get_all(Notifier)

    def test_qualifier_filter_in_get_all(self, container: DIContainer) -> None:
        """get_all(T, qualifier=...) must exclude bindings with other qualifiers."""
        container.bind(Notifier, EmailNotifier)     # qualifier=None
        container.bind(Notifier, PushNotifier)      # qualifier="push"

        results = container.get_all(Notifier, qualifier="push")

        assert len(results) == 1
        assert isinstance(results[0], PushNotifier)


# ─────────────────────────────────────────────────────────────────
#  Global container tests
# ─────────────────────────────────────────────────────────────────

class TestGlobalContainer:
    """Tests for DIContainer.current() and DIContainer.scoped()."""

    def test_current_returns_same_instance(self) -> None:
        """DIContainer.current() should always return the same global instance."""
        a = DIContainer.current()
        b = DIContainer.current()

        assert a is b

    def test_scoped_installs_fresh_container(self) -> None:
        """DIContainer.scoped() should swap in a fresh container for the block duration."""
        original = DIContainer.current()

        with DIContainer.scoped() as scoped:
            assert scoped is not original
            assert DIContainer.current() is scoped

        # Previous global is restored after exiting the block
        assert DIContainer.current() is original

    def test_scoped_restores_global_on_exception(self) -> None:
        """scoped() must restore the previous global even if the with-block raises."""
        original = DIContainer.current()

        with pytest.raises(ValueError):
            with DIContainer.scoped():
                raise ValueError("test exception")

        assert DIContainer.current() is original

    def test_context_manager_calls_shutdown_on_exit(self) -> None:
        """with container: ... should call shutdown() on __exit__."""
        container = DIContainer()

        destroyed: list[bool] = []

        @Singleton
        class Resource:
            from injectable.decorator.lifecycle import PreDestroy as _PreDestroy

            @_PreDestroy
            def teardown(self) -> None:
                destroyed.append(True)

        container.register(Resource)
        container.get(Resource)     # caches the singleton

        with container:
            pass                    # __exit__ calls shutdown()

        # @PreDestroy was called during shutdown
        assert destroyed == [True]


# ─────────────────────────────────────────────────────────────────
#  Async global accessor tests
# ─────────────────────────────────────────────────────────────────

class TestAcurrent:
    """Tests for DIContainer.acurrent() — the async global accessor."""

    async def test_acurrent_returns_a_container(self) -> None:
        """acurrent() must return a DIContainer instance."""
        container = await DIContainer.acurrent()

        assert isinstance(container, DIContainer)

    async def test_acurrent_returns_same_instance_on_repeated_calls(self) -> None:
        """acurrent() must return the same global instance on every call."""
        a = await DIContainer.acurrent()
        b = await DIContainer.acurrent()

        assert a is b

    async def test_acurrent_and_current_share_the_same_global(self) -> None:
        """acurrent() and current() must both access the same _global instance.

        DESIGN: Both methods write to DIContainer._global. Callers that mix
        sync and async code must see the same container regardless of which
        accessor they use.
        """
        sync_global  = DIContainer.current()
        async_global = await DIContainer.acurrent()

        assert sync_global is async_global


# ─────────────────────────────────────────────────────────────────
#  Provider with injected dependencies
# ─────────────────────────────────────────────────────────────────

class TestProviderWithDeps:
    """Verify that @Provider functions can declare injectable parameters."""

    def test_provider_receives_injected_dep(self, container: DIContainer) -> None:
        """A @Provider function with a typed parameter must receive the resolved dep.

        DESIGN: Provider functions are resolved via _call_provider(), which calls
        _collect_kwargs_sync() exactly like _resolve_constructor(). This means
        @Provider functions participate in full dependency injection — they are
        not limited to zero-argument factories.
        """
        @Singleton
        class ConnectionPool:
            url = "postgres://localhost/db"

        @Provider
        def make_notifier(pool: ConnectionPool) -> Notifier:
            notifier = EmailNotifier()
            notifier.pool_url = pool.url  # type: ignore[attr-defined]
            return notifier

        container.register(ConnectionPool)
        container.provide(make_notifier)

        notifier = container.get(Notifier)

        assert isinstance(notifier, EmailNotifier)
        assert notifier.pool_url == "postgres://localhost/db"  # type: ignore[attr-defined]

    async def test_async_provider_receives_injected_dep(
        self, container: DIContainer
    ) -> None:
        """An async @Provider function must also have its parameters injected."""
        @Singleton
        class Settings:
            host = "redis://localhost"

        @Provider(singleton=True)
        async def make_sms(settings: Settings) -> SMSNotifier:
            instance = SMSNotifier()
            instance.host = settings.host  # type: ignore[attr-defined]
            return instance

        container.register(Settings)
        container.provide(make_sms)

        sms = await container.aget(SMSNotifier)

        assert isinstance(sms, SMSNotifier)
        assert sms.host == "redis://localhost"  # type: ignore[attr-defined]

    def test_provider_dep_chain(self, container: DIContainer) -> None:
        """Provider A can depend on the result of Provider B — full chain resolved."""
        @Singleton
        class Config:
            dsn = "sqlite://:memory:"

        @Provider(singleton=True)
        def make_sms(cfg: Config) -> SMSNotifier:
            # SMSNotifier represents a 'connection pool' that needs config
            instance = SMSNotifier()
            instance.dsn = cfg.dsn  # type: ignore[attr-defined]
            return instance

        @Provider
        def make_email(pool: SMSNotifier) -> EmailNotifier:
            # EmailNotifier represents a 'service' that needs the pool
            svc = EmailNotifier()
            svc.dsn = pool.dsn  # type: ignore[attr-defined]
            return svc

        container.register(Config)
        container.provide(make_sms)
        container.provide(make_email)

        service = container.get(EmailNotifier)

        assert service.dsn == "sqlite://:memory:"  # type: ignore[attr-defined]
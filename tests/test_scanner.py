"""Unit tests for DefaultContainerScanner and DIContainer.scan().

Covered:
    - scan(str): imports a module by dotted name and scans it
    - scan(ModuleType): accepts an already-imported module object
    - scan() registers @Component-decorated classes
    - scan() registers @Singleton-decorated classes
    - scan() registers @Provider-decorated functions
    - scan() skips members whose name starts with '_'
    - scan() skips symbols re-exported from other modules (inspect.getmodule guard)
    - scan() is idempotent — scanning the same module twice doesn't double-register
    - scan() autobinds to an abstract base class when the impl inherits from one
    - scan() self-binds when the class has no abstract base
    - scan() raises ModuleNotFoundError for unknown module names
    - container.scan() delegates to the internal _scanner

DESIGN: Fake modules are created via types.ModuleType and temporarily registered
in sys.modules. Stamping __module__ on each class/function makes inspect.getmodule()
return the fake module, satisfying the scanner's "defined here?" check.
"""
from __future__ import annotations

import sys
import types
import uuid
from abc import ABC, abstractmethod

import pytest

from injectable.container import DIContainer
from injectable.decorator.scope import Component, Provider, Singleton
from injectable.scanner import DefaultContainerScanner


# ─────────────────────────────────────────────────────────────────
#  Module-level sentinel for provider return-type tests
#
#  DESIGN: ProviderBinding resolves the return type at registration time
#  via get_type_hints(fn), which only searches fn.__globals__ (the module
#  where the function was defined).  Locally-defined classes are absent
#  from __globals__, so they cannot be used as @Provider return types.
#  This sentinel lives at module level — always resolvable from any
#  provider function defined in this file.
# ─────────────────────────────────────────────────────────────────

class _ProviderWidget:
    """Module-level sentinel used as @Provider return type in scanner tests."""


# ─────────────────────────────────────────────────────────────────
#  Helpers and fixtures
# ─────────────────────────────────────────────────────────────────

def _fresh_module_name() -> str:
    """Return a unique module name that cannot collide with real modules."""
    return f"_injectable_test_{uuid.uuid4().hex}"


def _add(mod: types.ModuleType, obj: object) -> object:
    """Stamp *obj* as 'defined in' *mod* and attach it as an attribute.

    inspect.getmodule() resolves obj.__module__ → sys.modules lookup.
    Setting obj.__module__ = mod.__name__ makes that lookup return *mod*.

    Args:
        mod: The fake module to attach the object to.
        obj: A class or function to register.

    Returns:
        The same object (for chaining).
    """
    name = getattr(obj, "__name__", None) or getattr(obj, "__qualname__", "unknown")
    obj.__module__ = mod.__name__  # type: ignore[union-attr]
    setattr(mod, name, obj)
    return obj


@pytest.fixture
def fake_mod() -> types.ModuleType:
    """Yield a fresh ModuleType registered in sys.modules.

    Removed from sys.modules after the test to avoid cross-test pollution.

    Yields:
        A types.ModuleType ready to receive DI-decorated members.
    """
    name = _fresh_module_name()
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    yield mod
    sys.modules.pop(name, None)


# ─────────────────────────────────────────────────────────────────
#  Tests: basic registration
# ─────────────────────────────────────────────────────────────────

class TestScanBasicRegistration:
    """Verify that scan() picks up the standard DI decorators."""

    def test_scan_registers_component_class(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """@Component class defined in the module must be registered."""
        @Component
        class MyService:
            pass

        _add(fake_mod, MyService)
        container.scan(fake_mod)

        result = container.get(MyService)
        assert isinstance(result, MyService)

    def test_scan_registers_singleton_class(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """@Singleton class must be registered and its scope preserved."""
        @Singleton
        class MySingleton:
            pass

        _add(fake_mod, MySingleton)
        container.scan(fake_mod)

        a = container.get(MySingleton)
        b = container.get(MySingleton)
        # Scope must be SINGLETON — same instance returned both times
        assert a is b

    def test_scan_registers_provider_function(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """@Provider function defined in the module must be registered."""
        # Return type must be a module-level class so ProviderBinding can
        # resolve it via get_type_hints(fn) at registration time.
        @Provider
        def make_widget() -> _ProviderWidget:
            return _ProviderWidget()

        _add(fake_mod, make_widget)
        container.scan(fake_mod)

        result = container.get(_ProviderWidget)
        assert isinstance(result, _ProviderWidget)

    def test_scan_by_module_name_string(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """scan(str) must import the module by name then scan it."""
        @Component
        class NamedService:
            pass

        _add(fake_mod, NamedService)
        # Pass the module name as a string — scanner must import it
        container.scan(fake_mod.__name__)

        result = container.get(NamedService)
        assert isinstance(result, NamedService)

    def test_scan_by_module_object(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """scan(ModuleType) must accept an already-imported module object."""
        @Component
        class DirectService:
            pass

        _add(fake_mod, DirectService)
        container.scan(fake_mod)   # ModuleType, not a string

        assert isinstance(container.get(DirectService), DirectService)


# ─────────────────────────────────────────────────────────────────
#  Tests: filtering / skipping
# ─────────────────────────────────────────────────────────────────

class TestScanFiltering:
    """Verify that scan() applies the private-name and re-export guards."""

    def test_scan_skips_private_members(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Members whose name starts with '_' must be silently skipped.

        DESIGN: Private members are implementation details — auto-registering
        them would break encapsulation. The '_' prefix convention in Python
        signals 'not part of the public API'.
        """
        @Component
        class _PrivateService:
            pass

        _add(fake_mod, _PrivateService)
        container.scan(fake_mod)

        # _PrivateService starts with '_' — must not be registered
        with pytest.raises(LookupError):
            container.get(_PrivateService)

    def test_scan_skips_reexported_symbols(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Symbols whose defining module is different from the scanned module must be skipped.

        DESIGN: Without this guard, scanning a module that re-exports from
        another package (e.g. `from third_party import ThirdPartyService`)
        would double-register ThirdPartyService. The guard uses
        inspect.getmodule(obj) is module to detect re-exports.
        """
        @Component
        class ReexportedService:
            pass

        # Attach to fake_mod BUT keep __module__ pointing elsewhere —
        # simulates `from other_module import ReexportedService`
        ReexportedService.__module__ = "some_other_module"
        setattr(fake_mod, "ReexportedService", ReexportedService)

        container.scan(fake_mod)

        # Should NOT be registered — wrong defining module
        with pytest.raises(LookupError):
            container.get(ReexportedService)


# ─────────────────────────────────────────────────────────────────
#  Tests: idempotency
# ─────────────────────────────────────────────────────────────────

class TestScanIdempotency:
    """Verify that scanning the same module twice doesn't double-register."""

    def test_scan_is_idempotent_for_classes(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Scanning the same module twice must not add duplicate ClassBindings.

        DESIGN: The scanner checks whether the implementation class is already
        in _bindings before appending — this prevents double-registration on
        repeated scans (e.g. in a hot-reload scenario).
        """
        @Singleton
        class IdempotentService:
            pass

        _add(fake_mod, IdempotentService)

        container.scan(fake_mod)
        container.scan(fake_mod)   # second scan — must be a no-op

        # Exactly one binding — not two
        matching = [
            b for b in container._bindings
            if getattr(b, "implementation", None) is IdempotentService
        ]
        assert len(matching) == 1

    def test_scan_is_idempotent_for_providers(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Scanning the same @Provider function twice must not add duplicate ProviderBindings."""
        # Return type must be module-level — see _ProviderWidget sentinel above.
        @Provider
        def make_widget() -> _ProviderWidget:
            return _ProviderWidget()

        _add(fake_mod, make_widget)

        container.scan(fake_mod)
        container.scan(fake_mod)

        from injectable.binding import ProviderBinding

        matching = [
            b for b in container._bindings
            if isinstance(b, ProviderBinding) and b.fn.__name__ == "make_widget"
        ]
        assert len(matching) == 1


# ─────────────────────────────────────────────────────────────────
#  Tests: abstract base class auto-binding
# ─────────────────────────────────────────────────────────────────

class TestScanAutoBinding:
    """Verify how scan() decides between interface-bind and self-bind."""

    def test_scan_autobinds_to_abstract_base(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """When an impl inherits from an ABC, it must be bound to that ABC.

        DESIGN: inspect.isabstract(base) returns True only for classes that
        have unimplemented abstract methods. _find_interfaces() walks the MRO
        looking for such bases. If found, ClassBinding(interface, impl) is used
        instead of ClassBinding(impl, impl), so callers can resolve by interface.
        """
        class IRepository(ABC):
            @abstractmethod
            def find(self) -> object: ...

        @Component
        class SqlRepository(IRepository):
            def find(self) -> object:
                return object()

        _add(fake_mod, IRepository)
        _add(fake_mod, SqlRepository)
        container.scan(fake_mod)

        # Must be resolvable via the abstract interface
        result = container.get(IRepository)
        assert isinstance(result, SqlRepository)

    def test_scan_self_binds_when_no_abstract_base(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """When no ABC is in the MRO, the class must be bound to itself.

        DESIGN: Self-binding ensures concrete classes with no interface can
        still be resolved directly — a common pattern for leaf services.
        """
        @Component
        class ConcreteLeaf:
            """No ABC — should be bound as ConcreteLeaf → ConcreteLeaf."""
            pass

        _add(fake_mod, ConcreteLeaf)
        container.scan(fake_mod)

        result = container.get(ConcreteLeaf)
        assert isinstance(result, ConcreteLeaf)

    def test_scan_binds_to_multiple_abstract_bases(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """A class implementing two ABCs must be bound to both interfaces."""
        class IReadable(ABC):
            @abstractmethod
            def read(self) -> str: ...

        class IWritable(ABC):
            @abstractmethod
            def write(self, data: str) -> None: ...

        @Component
        class ReadWriteStore(IReadable, IWritable):
            def read(self) -> str:
                return ""

            def write(self, data: str) -> None:
                pass

        _add(fake_mod, IReadable)
        _add(fake_mod, IWritable)
        _add(fake_mod, ReadWriteStore)
        container.scan(fake_mod)

        # Both abstract interfaces must resolve to the same implementation
        assert isinstance(container.get(IReadable), ReadWriteStore)
        assert isinstance(container.get(IWritable), ReadWriteStore)


# ─────────────────────────────────────────────────────────────────
#  Tests: error paths
# ─────────────────────────────────────────────────────────────────

class TestScanErrorPaths:
    """Verify scanner behaviour for invalid inputs."""

    def test_scan_raises_module_not_found_for_unknown_name(
        self, container: DIContainer
    ) -> None:
        """scan('no.such.module') must raise ModuleNotFoundError."""
        with pytest.raises(ModuleNotFoundError):
            container.scan("no_such_module_xyzzy_injectable_test")

    def test_container_scan_delegates_to_scanner(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """container.scan() must delegate to self._scanner.scan()."""
        calls: list[str] = []

        class RecordingScanner:
            def scan(self, module: object, *, recursive: bool = False) -> None:
                calls.append(getattr(module, "__name__", str(module)))

        container._scanner = RecordingScanner()  # type: ignore[assignment]
        container.scan(fake_mod)

        assert len(calls) == 1
        assert calls[0] == fake_mod.__name__

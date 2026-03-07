"""Unit tests for circular dependency detection.

Covered:
    - A → B → A: two-class cycle raises CircularDependencyError
    - A → B → C → A: three-class cycle raises CircularDependencyError
    - Error message contains the human-readable cycle chain (e.g. "A → B → A")
    - Lazy[T] breaks a cycle that would otherwise raise
    - Non-circular dep graph resolves successfully (no false positives)
    - Diamond pattern (shared dep, not a cycle) resolves correctly

DESIGN NOTE: Cycle detection uses a ContextVar[list[type]] — the resolution
stack is isolated per asyncio Task and per thread. This means a concurrent
aget() for a different type doesn't interfere with the current resolution.

WHY MODULE-LEVEL CLASSES:
All test classes are defined at module level (not inside test methods) because
`from __future__ import annotations` turns every annotation into a lazy string.
`get_type_hints()` resolves those strings from the function's __globals__, which
is the *module* namespace — not the local scope of the enclosing test function.
Classes defined locally inside a test function are invisible to get_type_hints(),
so the container would silently skip their constructor parameters.
"""
from __future__ import annotations

import pytest

from injectable.container import DIContainer
from injectable.decorator.scope import Component, Singleton
from injectable.exceptions import CircularDependencyError
from injectable.type import Lazy


# ─────────────────────────────────────────────────────────────────
#  Two-class cycle: _TwoA → _TwoB → _TwoA
#  _TwoA is defined before _TwoB, so the annotation is a forward
#  reference. With PEP 563, it becomes a lazy string resolved later
#  by get_type_hints() — both classes are in module globals by then.
# ─────────────────────────────────────────────────────────────────

@Component
class _TwoA:
    def __init__(self, b: _TwoB) -> None:
        self.b = b


@Component
class _TwoB:
    def __init__(self, a: _TwoA) -> None:
        self.a = a


# ─────────────────────────────────────────────────────────────────
#  Three-class cycle: _ThreeA → _ThreeB → _ThreeC → _ThreeA
# ─────────────────────────────────────────────────────────────────

@Component
class _ThreeA:
    def __init__(self, b: _ThreeB) -> None:
        self.b = b


@Component
class _ThreeB:
    def __init__(self, c: _ThreeC) -> None:
        self.c = c


@Component
class _ThreeC:
    def __init__(self, a: _ThreeA) -> None:
        self.a = a


# ─────────────────────────────────────────────────────────────────
#  Error-message cycle — named distinctly to check name in output
# ─────────────────────────────────────────────────────────────────

@Component
class _CycleAlpha:
    def __init__(self, beta: _CycleBeta) -> None:
        self.beta = beta


@Component
class _CycleBeta:
    def __init__(self, alpha: _CycleAlpha) -> None:
        self.alpha = alpha


# ─────────────────────────────────────────────────────────────────
#  Lazy cycle-break: _LazyA holds Lazy[_LazyB], _LazyB holds _LazyA
#  Lazy defers _LazyB's resolution past _LazyA's constructor return,
#  so the cycle-detection stack never sees both at the same time.
# ─────────────────────────────────────────────────────────────────

@Singleton
class _LazyA:
    def __init__(self, b: Lazy[_LazyB]) -> None:  # type: ignore[valid-type]
        self.b = b


@Singleton
class _LazyB:
    def __init__(self, a: _LazyA) -> None:
        self.a = a


# ─────────────────────────────────────────────────────────────────
#  Non-circular linear chain: _LinearA → _LinearB → _LinearC
# ─────────────────────────────────────────────────────────────────

@Component
class _LinearC:
    pass


@Component
class _LinearB:
    def __init__(self, c: _LinearC) -> None:
        self.c = c


@Component
class _LinearA:
    def __init__(self, b: _LinearB) -> None:
        self.b = b


# ─────────────────────────────────────────────────────────────────
#  Diamond: _DiamondA → {_DiamondB, _DiamondC} → _DiamondD
#  _DiamondD is a shared dep, not a cycle — it appears twice in the
#  resolution tree but never simultaneously in the same stack path.
# ─────────────────────────────────────────────────────────────────

@Component
class _DiamondD:
    pass


@Component
class _DiamondB:
    def __init__(self, d: _DiamondD) -> None:
        self.d = d


@Component
class _DiamondC:
    def __init__(self, d: _DiamondD) -> None:
        self.d = d


@Component
class _DiamondA:
    def __init__(self, b: _DiamondB, c: _DiamondC) -> None:
        self.b = b
        self.c = c


# ─────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────

class TestCircularDependencyDetection:
    """Tests for CircularDependencyError detection and reporting."""

    def test_two_class_cycle_raises(self, container: DIContainer) -> None:
        """_TwoA → _TwoB → _TwoA must raise CircularDependencyError."""
        container.register(_TwoA)
        container.register(_TwoB)

        with pytest.raises(CircularDependencyError):
            container.get(_TwoA)

    def test_three_class_cycle_raises(self, container: DIContainer) -> None:
        """_ThreeA → _ThreeB → _ThreeC → _ThreeA must raise CircularDependencyError."""
        container.register(_ThreeA)
        container.register(_ThreeB)
        container.register(_ThreeC)

        with pytest.raises(CircularDependencyError):
            container.get(_ThreeA)

    def test_error_message_contains_cycle_chain(self, container: DIContainer) -> None:
        """CircularDependencyError message must contain both class names in the chain."""
        container.register(_CycleAlpha)
        container.register(_CycleBeta)

        with pytest.raises(CircularDependencyError) as exc_info:
            container.get(_CycleAlpha)

        error_text = str(exc_info.value)
        assert "_CycleAlpha" in error_text
        assert "_CycleBeta" in error_text

    def test_lazy_breaks_two_class_cycle(self, container: DIContainer) -> None:
        """Lazy[T] must allow _LazyA → _LazyB → _LazyA to resolve without error.

        Lazy[_LazyB] in _LazyA's constructor creates a proxy without resolving
        _LazyB. _LazyB's constructor then resolves _LazyA (already constructed),
        so no cycle is detected.
        """
        container.register(_LazyA)
        container.register(_LazyB)

        # Must NOT raise — Lazy[_LazyB] defers _LazyB's resolution
        a = container.get(_LazyA)
        assert isinstance(a, _LazyA)

    def test_non_circular_graph_resolves_correctly(
        self, container: DIContainer
    ) -> None:
        """A linear _LinearA → _LinearB → _LinearC (no cycle) must resolve cleanly."""
        container.register(_LinearC)
        container.register(_LinearB)
        container.register(_LinearA)

        a = container.get(_LinearA)

        assert isinstance(a, _LinearA)
        assert isinstance(a.b, _LinearB)
        assert isinstance(a.b.c, _LinearC)

    def test_diamond_dependency_resolves_correctly(
        self, container: DIContainer
    ) -> None:
        """Diamond pattern must not trigger a false CircularDependencyError.

        _DiamondD is a shared dependency — it appears twice in the resolution
        tree but is never on the same stack path simultaneously, so it is not
        a cycle.
        """
        container.register(_DiamondD)
        container.register(_DiamondB)
        container.register(_DiamondC)
        container.register(_DiamondA)

        a = container.get(_DiamondA)

        assert isinstance(a.b.d, _DiamondD)
        assert isinstance(a.c.d, _DiamondD)

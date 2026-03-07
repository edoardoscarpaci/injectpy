from __future__ import annotations

import inspect
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Protocol,
    TypeAlias,
    TypeVar,
    get_type_hints,
    runtime_checkable,
)

from .decorator.lifecycle import _find_post_construct, _find_pre_destroy
from .exceptions import (
    ClassBindingNotDecoratedError,
    ProviderBindingNotDecoratedError,
    ScopeViolationDetectedError,
)
from .metadata import (
    DIMetadata,
    ProviderMetadata,
    Scope,
    _get_metadata,
    _get_provider_metadata,
)

if TYPE_CHECKING:
    from .container import DIContainer

# ─────────────────────────────────────────────────────────────────
#  Shared protocol — what the container needs from any binding
# ─────────────────────────────────────────────────────────────────
@runtime_checkable
class IBinding(Protocol):
    """Structural protocol that every binding type must satisfy.

    The container only ever talks to ``IBinding`` — never to ``ClassBinding``
    or ``ProviderBinding`` directly.  This eliminates all ``isinstance`` guards
    from ``DIContainer`` and makes adding new binding strategies O(1) effort.

    Thread safety:  ✅ Safe — bindings are immutable after construction.
                    All mutable state lives in the container's caches, not here.
    Async safety:   ✅ Safe — ``acreate`` is a coroutine; callers must await it.
                    The binding itself holds no async state.

    Edge cases:
        - ``interface`` may equal ``implementation`` (self-registration).
        - ``qualifier`` being ``None`` means "matches any qualifier" in filters.
        - ``priority`` defaults to ``0``; lower values win in sorting.
    """
    interface:    type
    qualifier:    str | None
    priority:     int
    scope:        Scope
    
    def validate(self, container: 'DIContainer') -> None:
        """Validate this binding against the container's current registry.

        Called once by :meth:`~injectable.container.DIContainer.validate_bindings`
        during the phase transition from registration to resolution.
        Implementations should raise on any detected misconfiguration
        (e.g. scope leaks).

        Args:
            container: The container holding the full binding registry,
                used to look up dependency bindings for cross-binding checks.

        Returns:
            None

        Raises:
            ScopeViolationDetectedError: If the binding introduces a scope leak.
        """
        pass

    def create(self, container: 'DIContainer') -> Any:
        """Instantiate or invoke this binding synchronously.

        Each binding type owns its own creation logic — the container never
        needs to distinguish between a class and a provider function.

        Args:
            container: The active ``DIContainer``, used to resolve
                transitive dependencies during construction.

        Returns:
            The fully constructed and injected instance.

        Raises:
            RuntimeError: If the underlying provider is ``async def`` and
                must be awaited — callers should use :meth:`acreate` instead.
            CircularDependencyError: If resolving this binding would close
                a dependency cycle.
            LookupError: If a required transitive dependency has no binding.
        """
        ...

    async def acreate(self, container: 'DIContainer') -> Any:
        """Instantiate or invoke this binding asynchronously.

        Async mirror of :meth:`create`.  Handles both sync and async providers
        transparently — sync providers are called normally; async providers are
        awaited.

        Args:
            container: The active ``DIContainer``, used to resolve
                transitive dependencies during construction.

        Returns:
            The fully constructed and injected instance.

        Raises:
            CircularDependencyError: If resolving this binding would close
                a dependency cycle.
            LookupError: If a required transitive dependency has no binding.
        """
        ...


class ClassBinding:
    """Binding that instantiates a concrete class via constructor injection.

    Reads DI metadata from the ``@Component`` / ``@Singleton`` decorator on
    *implementation* and stores lifecycle hooks discovered by MRO walk.

    Thread safety:  ✅ Safe — all attributes are set once in ``__init__``
                    and never mutated.  Caching lives in the container, not here.
    Async safety:   ✅ Safe — ``acreate`` is a coroutine; no shared async state.

    Edge cases:
        - ``interface == implementation`` is valid (self-registration via
          ``container.register()``).
        - If *implementation* has both a sync and async ``@PostConstruct``,
          ``_find_post_construct`` raises — only one hook is allowed.
        - ``pre_destroy`` is ``None`` for classes without ``@PreDestroy``.
    """

    def __init__(self, interface: type, implementation: type) -> None:
        """Create a class binding between *interface* and *implementation*.

        Validates the subclass relationship, reads DI metadata, and discovers
        lifecycle hooks.  Raises immediately on misconfiguration so errors
        surface at registration time, not at resolution time.

        Args:
            interface: The abstract type (or base class) the container will
                resolve.  Callers use this type in ``container.get()``.
            implementation: The concrete class to instantiate.  Must be a
                subclass of *interface* and decorated with ``@Component`` or
                ``@Singleton``.

        Returns:
            None

        Raises:
            TypeError: If *implementation* is not a subclass of *interface*.
            ClassBindingNotDecoratedError: If *implementation* has no DI
                metadata — i.e. it was not decorated with ``@Component`` or
                ``@Singleton``.
        """
        if not issubclass(implementation, interface):
            raise TypeError(f"{implementation.__name__} must be a subclass of {interface.__name__}")

        self.interface      = interface
        self.implementation = implementation

        meta: DIMetadata | None = _get_metadata(implementation)
        if meta is None:
            raise ClassBindingNotDecoratedError(implementation)

        self.scope          = meta.scope
        self.qualifier      = meta.qualifier
        self.priority       = meta.priority
        self.post_construct = _find_post_construct(implementation)
        self.pre_destroy    = _find_pre_destroy(implementation)

    def __repr__(self) -> str:
        qualifier_part = f", qualifier={self.qualifier!r}" if self.qualifier else ""
        return (
            f"ClassBinding("
            f"{self.interface.__name__} → {self.implementation.__name__}, "
            f"scope={self.scope.name}"
            f"{qualifier_part})"
        )

    def validate(self, container: DIContainer) -> None:
        """Check this binding for scope leaks against the container's registry.

        Delegates to :meth:`~injectable.container.DIContainer._check_scope_violation`
        to inspect every ``__init__`` parameter and confirm no dependency has a
        narrower scope than *self*.

        Args:
            container: The container whose binding registry is searched for
                each dependency type declared in ``__init__``.

        Returns:
            None

        Raises:
            ScopeViolationDetectedError: If any direct dependency has a
                narrower scope than this binding (e.g. a ``SINGLETON`` depending
                on a ``REQUEST``-scoped component).
        """
        scope_violations = container._check_scope_violation(self)
        if scope_violations:
            raise ScopeViolationDetectedError(scope_violations=scope_violations)

    def create(self, container: DIContainer) -> Any:
        """Instantiate the implementation class synchronously via constructor injection.

        Args:
            container: The active ``DIContainer``, used to resolve every
                ``__init__`` parameter of :attr:`implementation`.

        Returns:
            A fully constructed instance of :attr:`implementation` with all
            dependencies injected and ``@PostConstruct`` invoked.

        Raises:
            RuntimeError: If ``@PostConstruct`` is ``async def`` — use
                :meth:`acreate` (via ``container.aget()``) instead.
            CircularDependencyError: If resolving this class would close
                a dependency cycle.
            LookupError: If any required ``__init__`` parameter has no binding.
        """
        instance = container._resolve_constructor(self.implementation)
        container._run_post_construct_sync(instance, self.post_construct)
        return instance

    async def acreate(self, container: DIContainer) -> Any:
        """Instantiate the implementation class asynchronously via constructor injection.

        Async mirror of :meth:`create`.  Both sync and async ``@PostConstruct``
        hooks are handled — async hooks are awaited, sync hooks called normally.

        Args:
            container: The active ``DIContainer``, used to resolve every
                ``__init__`` parameter of :attr:`implementation`.

        Returns:
            A fully constructed instance of :attr:`implementation` with all
            dependencies injected and ``@PostConstruct`` invoked.

        Raises:
            CircularDependencyError: If resolving this class would close
                a dependency cycle.
            LookupError: If any required ``__init__`` parameter has no binding.
        """
        instance = await container._resolve_constructor_async(self.implementation)
        await container._run_post_construct_async(instance, self.post_construct)
        return instance

class ProviderBinding:
    """Binding that delegates instance creation to a plain function (factory).

    The function's return type annotation becomes the resolved interface.
    Supports both sync and async provider functions — async status is detected
    once at registration time via ``inspect.iscoroutinefunction``, not at
    each resolution.

    Thread safety:  ✅ Safe — all attributes are set once in ``__init__``
                    and never mutated.
    Async safety:   ✅ Safe — ``is_async`` is a plain bool set at init time;
                    no shared mutable state across tasks.

    Edge cases:
        - Provider with no return annotation → raises ``TypeError`` at registration.
        - Provider decorated ``@Provider(singleton=False)`` → ``Scope.DEPENDENT``,
          a new instance is created on every resolution.
        - Provider decorated ``@Provider(singleton=True)`` → ``Scope.SINGLETON``,
          the result is cached by the container after first call.
        - ``validate()`` is a no-op — provider functions have no constructor
          dependencies to inspect for scope leaks.
    """

    def __init__(self, fn: Callable[..., Any]) -> None:
        """Create a provider binding from a decorated factory function.

        Reads ``ProviderMetadata`` from *fn*, extracts the return type hint
        as the interface, and detects whether the provider is async.

        Args:
            fn: A callable decorated with ``@Provider``.  May be a regular
                function or ``async def``.  Must declare a return type annotation.

        Returns:
            None

        Raises:
            ProviderBindingNotDecoratedError: If *fn* has no ``ProviderMetadata``
                — i.e. it was not decorated with ``@Provider``.
            TypeError: If *fn* has no return type annotation, since the
                return type is used as the resolved interface.
        """
        meta: ProviderMetadata | None = _get_provider_metadata(fn)
        if meta is None:
            raise ProviderBindingNotDecoratedError(fn)

        try:
            # Happy path: all annotations are resolvable from fn.__globals__.
            # Works when every annotated type is defined at module level.
            hints = get_type_hints(fn)
            interface = hints.get("return")
        except Exception:
            # PEP-563 (from __future__ import annotations) makes ALL annotations
            # lazy strings.  get_type_hints() evaluates them against fn.__globals__,
            # but locally-defined PARAMETER types (defined inside test functions,
            # lambdas, etc.) are absent from __globals__, causing NameError for
            # the entire call — even when the return type itself IS resolvable.
            #
            # Fallback: evaluate just the return annotation directly.  Parameter
            # types are resolved later in _collect_kwargs_sync() via _build_localns(),
            # so we only need the return type here.
            ret = fn.__annotations__.get("return")
            if isinstance(ret, type):
                # Already a type object — PEP-563 not active in the caller's module.
                interface = ret
            elif isinstance(ret, str):
                # String annotation — evaluate against fn's own globals only.
                # If the return type is also locally-defined this also fails,
                # and interface stays None (triggering TypeError below).
                try:
                    interface = eval(ret, getattr(fn, "__globals__", {}))  # type: ignore[arg-type]
                except Exception:
                    interface = None
            else:
                interface = ret  # None or unexpected — TypeError raised below

        if interface is None:
            raise TypeError(f"Provider '{fn.__name__}' must declare a return type hint.")

        self.interface = interface
        self.fn        = fn

        # Detect async at registration time — avoids repeated inspect calls
        # on every resolution. iscoroutinefunction is cheap but registrations
        # run once; resolutions run many times.
        self.is_async: bool = inspect.iscoroutinefunction(fn)

        # DESIGN: only SINGLETON and DEPENDENT are available to providers.
        # REQUEST / SESSION require an active scope context that providers
        # cannot participate in — they are stateless factory functions.
        self.scope     = Scope.SINGLETON if meta.singleton else Scope.DEPENDENT
        self.qualifier = meta.qualifier
        self.priority  = meta.priority

    def __repr__(self) -> str:
        qualifier_part = f", qualifier={self.qualifier!r}" if self.qualifier else ""
        async_part     = ", async" if self.is_async else ""
        return (
            f"ProviderBinding("
            f"{self.interface.__name__} ← {self.fn.__name__}, "
            f"scope={self.scope.name}"
            f"{qualifier_part}"
            f"{async_part})"
        )
    
    def validate(self, _container: DIContainer) -> None:
        """No-op — provider bindings have no scope-leak semantics to check.

        Provider functions declare their own scope via ``singleton=True/False``
        on :func:`~injectable.decorator.provider.Provider`, so there are no
        injected constructor dependencies to validate.

        Args:
            _container: Unused. Present to satisfy the :class:`IBinding` protocol.

        Returns:
            None
        """

    def create(self, container: DIContainer) -> Any:
        """Invoke the provider function synchronously with all dependencies injected.

        Args:
            container: The active ``DIContainer``, used to resolve every
                parameter of :attr:`fn`.

        Returns:
            The value returned by :attr:`fn`.

        Raises:
            RuntimeError: If :attr:`fn` is ``async def`` — the container guards
                against this before calling ``create()``; use :meth:`acreate`
                via ``container.aget()`` instead.
            CircularDependencyError: If the provider's return type is already
                on the resolution stack.
            LookupError: If any required parameter of :attr:`fn` has no binding.
        """
        return container._call_provider(self.fn)

    async def acreate(self, container: DIContainer) -> Any:
        """Invoke the provider function asynchronously with all dependencies injected.

        Handles both sync and async provider functions transparently — async
        providers are awaited, sync providers are called normally.

        Args:
            container: The active ``DIContainer``, used to resolve every
                parameter of :attr:`fn`.

        Returns:
            The value returned (or awaited) from :attr:`fn`.

        Raises:
            CircularDependencyError: If the provider's return type is already
                on the resolution stack.
            LookupError: If any required parameter of :attr:`fn` has no binding.
        """
        return await container._call_provider_async(self.fn)


# DESIGN: union TypeAlias — container only ever holds AnyBinding, never concrete types.
# Adding a new binding strategy means implementing IBinding here only —
# DIContainer needs no changes. TypeAlias makes this explicit to the type checker;
# without it, a bare assignment looks like a runtime variable, not a type alias.
AnyBinding: TypeAlias = ClassBinding | ProviderBinding
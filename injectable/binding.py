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
from dataclasses import (
    field,
    dataclass
)
from abc import (
    ABC,
    abstractmethod
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
    _is_scope_leak
)


if TYPE_CHECKING:
    from .container import DIContainer

@dataclass
class BindingDescriptor:
    """
    A plain, serialisable snapshot of a single binding and its full
    recursive dependency tree.

    Produced by ``Binding.describe()`` — never constructed directly by
    callers. Rendering to ASCII is done via ``__repr__``.

    Attributes:
        interface:    Fully-qualified name of the interface type.
        implementation: Fully-qualified name of the concrete type.
        scope:        Lifecycle scope of this binding.
        qualifier:    Optional named qualifier (Jakarta-style @Named).
        dependencies: Recursively resolved dependency descriptors.
        scope_leak:   True when this binding is injected into a longer-lived
                      parent — set by the parent during tree construction.

    Thread safety:  ✅ Frozen dataclass — immutable after construction.
    Async safety:   ✅ No shared mutable state.

    Edge cases:
        - Circular deps → caller must guard; descriptor does NOT detect cycles.
        - No dependencies → ``dependencies`` is an empty tuple.
        - Unknown scope → raises ValueError in ``_is_leak()``.

    Example:
        descriptor = my_binding.describe(container)
        print(descriptor)           # → ASCII tree
        print(repr(descriptor))     # → same ASCII tree
        d = descriptor.to_dict()    # → plain dict for JSON/YAML
    """

    interface:      str
    implementation: str
    scope:          Scope
    qualifier:      str | None                    = None
    priority :      int | None                    = None 
    dependencies:   tuple[BindingDescriptor, ...] = field(default_factory=tuple)
    
    @property
    def scope_leak(self) -> bool:
        """
        True if any **direct** dependency is shorter-lived than this binding.

        Only checks one level deep — this describes the binding's own health,
        not the health of its entire subtree. Deeper leaks are visible on
        their own descriptor when inspected directly.

        Returns:
            True if at least one direct dependency has a shorter scope.

        Edge cases:
            - No dependencies → always False.
            - Equal scopes    → False (not a leak).
        """
        # Compare each direct dep's scope against this binding's scope
        return any(_is_scope_leak(self.scope,dep.scope) for dep in self.dependencies)

    # ── ASCII rendering ───────────────────────────────────────────────────────

    def __repr__(self) -> str:
        """
        Render the full dependency tree as a human-readable ASCII tree.

        Output format:
            Interface [scope] (qualifier)  ⚠️ SCOPE LEAK
            ├── DepA [scope]
            │   └── DepB [scope]  ⚠️ SCOPE LEAK
            └── DepC [scope]

        Returns:
            Multi-line ASCII string. Single trailing newline.
        """
        lines: list[str] = []
        self._render(lines, prefix="", is_last=True, is_root=True)
        return "\n".join(lines)

    def _render(
        self,
        lines: list[str],
        prefix: str,
        is_last: bool,
        is_root: bool,
        parent_scope: Scope | None = None,
    ) -> None:
        """
        Recursively append tree lines into ``lines``.

        Args:
            lines:   Accumulator — each call appends one or more lines.
            prefix:  Indentation string built up as we recurse deeper.
            is_last: Whether this node is the last sibling — controls
                     whether we draw └── or ├──.
            is_root: Root node gets no branch connector.
        """
        # ── Build the connector for this node ────────────────────────────────
        # Root has no connector; children use └── or ├── depending on position.
        if is_root:
            connector = ""
        else:
            connector = "└── " if is_last else "├── "

        # ── Format the node label ─────────────────────────────────────────────
        qualifier_str = f" ({self.qualifier})" if self.qualifier else ""
        priority_str = f"Priority({self.priority})" if self.priority else ""
        leak_flag = (
            "  ⚠️  SCOPE LEAK"
            if parent_scope is not None and _is_scope_leak(parent_scope=parent_scope,dep_scope=self.scope)
            else ""
        )
        label = (
            f"{self.interface} [{self.scope.name}]{qualifier_str}{priority_str}"
            f" → {self.implementation}{leak_flag}"
        )

        lines.append(f"{prefix}{connector}{label}")

        # ── Recurse into dependencies ─────────────────────────────────────────
        # Extend the prefix so child branches align under their parent label.
        # Last child uses spaces (no continuing vertical bar); others use │.
        child_prefix = prefix if is_root else prefix + ("    " if is_last else "│   ")

        for i, dep in enumerate(self.dependencies):
            dep._render(
                lines,
                prefix=child_prefix,
                is_last=(i == len(self.dependencies) - 1),
                is_root=False,
                # Pass THIS node's scope so the child can flag itself if needed
                parent_scope=self.scope,
            )

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """
        Convert the full descriptor tree to a plain nested dict.

        Suitable for JSON / YAML serialisation. Scope is stored as its
        string name so the output is human-readable without the IntEnum.

        Returns:
            Nested dict mirroring the descriptor tree structure.

        Example:
            import json
            print(json.dumps(descriptor.to_dict(), indent=2))
        """
        return {
            "interface":      self.interface,
            "implementation": self.implementation,
            "scope":          self.scope.name,
            "qualifier":      self.qualifier,
            "scope_leak":     self.scope_leak,
            "priority":       self.priority,
            "dependencies":   [d.to_dict() for d in self.dependencies],
        }
class Binding(ABC):
    """
    Abstract base for all binding types in the DI container.

    Maps an interface to a strategy for producing an instance of that type.
    Subclasses implement sync, async, and describe variants alongside a
    validation step.
    """

    @abstractmethod
    def validate(self, container: DIContainer) -> None:
        """
        Assert that this binding is resolvable within the given container.

        Args:
            container: The container to validate against.
        """
        pass

    @abstractmethod
    def create(self, container: DIContainer) -> Any:
        """
        Synchronously construct and return an instance for this binding.

        Args:
            container: The container used to resolve transitive dependencies.

        Returns:
            A fully constructed and injected instance of the bound type.
        """
        pass

    @abstractmethod
    async def acreate(self, container: DIContainer) -> Any:
        """
        Asynchronously construct and return an instance for this binding.

        Args:
            container: The container used to resolve transitive dependencies.

        Returns:
            A fully constructed and injected instance of the bound type.
        """
        pass

    @abstractmethod
    def describe(
        self,
        container: DIContainer,
        _visited: frozenset[type] | None = None,
    ) -> BindingDescriptor:
        """
        Build a recursive ``BindingDescriptor`` snapshot of this binding.

        Args:
            container: The container used to look up dependency bindings.
            _visited:  Internal cycle guard — do not pass from call sites.

        Returns:
            A fully populated ``BindingDescriptor`` for this binding and its
            entire dependency subtree.
        """
        pass

class ClassBinding(Binding):
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
    
    def describe(self, container, _visited: frozenset[type] | None = None) -> BindingDescriptor:
        """
        Build a full recursive ``BindingDescriptor`` for this binding.

        Scope-leak flags are applied automatically on the completed tree
        before returning, so the caller always gets a fully annotated result.

        Args:
            container: The DI container — used to look up dependency bindings.
            _visited:  Internal cycle guard — do not pass from call sites.
                       Tracks interface types already on the current path.

        Returns:
            A fully annotated ``BindingDescriptor`` tree.

        Raises:
            RecursionError: If a circular dependency exists and the container
                            does not raise ``CircularDependencyError`` itself.

        Edge cases:
            - No dependencies → descriptor has empty ``dependencies`` tuple.
            - Dependency not registered → skipped with a sentinel descriptor
              showing ``[UNRESOLVED]`` scope.
        """
        # ── Cycle guard — prevent infinite recursion ──────────────────────────
        # Uses frozenset (immutable) so each recursive path is independent.
        visited = _visited or frozenset()
        if self.interface in visited:  # type: ignore[attr-defined]
            # Return a sentinel rather than crashing — caller can see the cycle
            return BindingDescriptor(
                interface=f"{self.interface.__name__} [CYCLE DETECTED]",  # type: ignore[attr-defined]
                implementation="—",
                scope=self.scope,  # type: ignore[attr-defined]
            )

        visited = visited | {self.interface}  # type: ignore[attr-defined]

        # ── Recursively describe each dependency ──────────────────────────────
        dep_descriptors: list[BindingDescriptor] = []
        for dep_binding in container._get_dependencies(self):  # type: ignore[attr-defined]
            dep_descriptors.append(dep_binding.describe(container, _visited=visited))

        # ── Build the descriptor ──────────────────────
        return BindingDescriptor(
            interface=self.interface.__name__,  # type: ignore[attr-defined]
            implementation=self.implementation.__name__,  # type: ignore[attr-defined]
            scope=self.scope,  # type: ignore[attr-defined]
            qualifier=self.qualifier,
            dependencies=tuple(dep_descriptors),
        )

class ProviderBinding(Binding):
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
    
    def validate(self, container: DIContainer) -> None:
        """No-op — provider bindings have no scope-leak semantics to check.

        Provider functions declare their own scope via ``singleton=True/False``
        on :func:`~injectable.decorator.provider.Provider`, so there are no
        injected constructor dependencies to validate.

        Args:
            _container: Unused. Present to satisfy the :class:`IBinding` protocol.

        Returns:
            None
        """
        pass

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
    
    def describe(
        self,
        container: DIContainer,
        _visited: frozenset[type] | None = None,
    ) -> BindingDescriptor:
        """
        Build a full recursive ``BindingDescriptor`` for this binding.

        Args:
            container: The DI container — used to look up dependency bindings.
            _visited:  Internal cycle guard — do not pass from call sites.
                       Tracks interface types already on the current path.

        Returns:
            A fully annotated ``BindingDescriptor`` tree.

        Raises:
            RecursionError: If a circular dependency exists and the container
                            does not raise ``CircularDependencyError`` itself.

        Edge cases:
            - No dependencies → descriptor has empty ``dependencies`` tuple.
            - Dependency not registered → skipped with a sentinel descriptor
              showing ``[UNRESOLVED]`` scope.
        """
        # ── Cycle guard — prevent infinite recursion ──────────────────────────
        # Uses frozenset (immutable) so each recursive path is independent.
        visited = _visited or frozenset()
        if self.interface in visited:  # type: ignore[attr-defined]
            # Return a sentinel rather than crashing — caller can see the cycle
            return BindingDescriptor(
                interface=f"{self.interface.__name__} [CYCLE DETECTED]",  # type: ignore[attr-defined]
                implementation="—",
                scope=self.scope,  # type: ignore[attr-defined]
            )

        visited = visited | {self.interface}  # type: ignore[attr-defined]

        # ── Recursively describe each dependency ──────────────────────────────
        dep_descriptors: list[BindingDescriptor] = []
        for dep_binding in container._get_dependencies(self):  # type: ignore[attr-defined]
            dep_descriptors.append(dep_binding.describe(container, _visited=visited))

        # ── Build the descriptor ──────────────────────
        return BindingDescriptor(
            interface=self.interface.__name__,  # type: ignore[attr-defined]
            implementation=self.fn.__name__,  # type: ignore[attr-defined]
            scope=self.scope,  # type: ignore[attr-defined]
            qualifier=self.qualifier,
            dependencies=tuple(dep_descriptors),
        )


# DESIGN: union TypeAlias — container only ever holds AnyBinding, never concrete types.
# Adding a new binding strategy means implementing IBinding here only —
# DIContainer needs no changes. TypeAlias makes this explicit to the type checker;
# without it, a bare assignment looks like a runtime variable, not a type alias.
AnyBinding: TypeAlias = ClassBinding | ProviderBinding
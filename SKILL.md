# Providify — Project Skill Reference

> Portable memory document. Import this into any AI assistant to get full context on the Providify codebase without re-exploration.

---

## 1. What Is Providify?

**Providify** is a zero-dependency Python dependency injection (DI) container library inspired by Jakarta CDI and Spring Framework. It automates constructor injection via type hints, manages component lifecycles across multiple scopes, and supports both synchronous and asynchronous resolution patterns.

- **Version:** 0.1.1
- **Python:** 3.12+
- **License:** Apache-2.0
- **Dependencies:** None (stdlib only)
- **Repository:** https://github.com/edoardoscarpaci/providify

---

## 2. Core Mental Model

### Two-Phase Operation

```
Phase 1 — Registration:  bind() / register() / provide() / scan() / install()
Phase 2 — Resolution:    get() / aget() / get_all() / aget_all()
```

On the **first** resolution call, `validate_bindings()` fires automatically. After that, the container enters a "live" state where instances are created and cached per scope.

### Key Design Decisions

| Decision | Why |
|---|---|
| Metadata stored in `cls.__dict__` | Picklable, GC-safe, multiprocess-safe (avoids `WeakKeyDictionary` fragility) |
| `ContextVar` for resolution stack and scope IDs | Each `asyncio.Task` gets its own isolated context — no state bleed across concurrent coroutines |
| `threading.Lock` + `asyncio.Lock` for caches | Dual locking supports both sync and async callers of the same container |
| `Annotated[T, InjectMeta(...)]` for injection hints | Carries qualifier/priority metadata without changing the visible type `T` |
| Abstract `Binding` base class | `ClassBinding` vs `ProviderBinding` are pluggable strategies — new binding types can be added without touching the container |

---

## 3. Directory Map

```
providify/
├── __init__.py          — Public API surface (exports everything users need)
├── container.py         — DIContainer, ScopeContext, _ScopedContainer (core orchestration)
├── binding.py           — Binding ABC, ClassBinding, ProviderBinding (creation strategies)
├── metadata.py          — DIMetadata, ProviderMetadata, Scope enum, accessors
├── scope.py             — ScopeContext: request/session instance caching via ContextVar
├── resolution.py        — _resolution_stack (ContextVar), cycle detection, _UNRESOLVED sentinel
├── type.py              — Inject, InjectInstances, Lazy aliases; LazyProxy, InjectMeta, LazyMeta
├── utils.py             — Generic type utilities: _type_name, _is_generic_subtype, _interface_matches
├── descriptor.py        — BindingDescriptor, DIContainerDescriptor (serializable snapshots / ASCII trees)
├── exceptions.py        — All custom exceptions
└── decorator/
    ├── scope.py         — @Component, @Singleton, @RequestScoped, @SessionScoped, @Provider, @Named, @Priority, @Inheritable
    ├── lifecycle.py     — @PostConstruct, @PreDestroy
    └── module.py        — @Configuration
```

---

## 4. Public API Reference

### Container Setup

```python
from providify import DIContainer

container = DIContainer()

container.bind(Interface, Implementation)       # explicit interface → implementation
container.register(ConcreteClass)               # self-bind a concrete class
container.provide(provider_fn)                  # register a @Provider factory function
container.scan("my.module", recursive=True)     # auto-discover decorated members
container.install(MyConfigModule)               # install a @Configuration class (sync)
await container.ainstall(MyConfigModule)        # install async
```

### Resolution

```python
svc = container.get(Service)                            # single instance (sync)
svc = container.get(Service, qualifier="email")         # with qualifier
svc = container.get(Service, priority=1)                # with priority override
svcs = container.get_all(Service)                       # all matching, sorted by priority

svc = await container.aget(Service)                     # single instance (async)
svcs = await container.aget_all(Service)                # all matching (async)
```

### Lifecycle Management

```python
container.warm_up()              # pre-create all singletons (sync)
await container.awarm_up()       # pre-create all singletons (async)
container.shutdown()             # destroy singletons, call @PreDestroy hooks
await container.ashutdown()      # async shutdown
```

### Global Container Pattern

```python
# Singleton accessor — creates on first call
container = DIContainer.current()
container = await DIContainer.acurrent()

# Temporarily replace the global container (useful for tests)
with DIContainer.scoped() as c:
    c.register(MockService)
    result = c.get(Service)
# original global is restored here
```

### Scope Contexts

```python
# Request scope — new instance per request block
with container.scope_context.request():
    svc = container.get(RequestScoped)

async with container.scope_context.arequest():
    svc = await container.aget(RequestScoped)

# Session scope — instance survives multiple requests for same session ID
with container.scope_context.session("user-abc"):
    svc = container.get(SessionScoped)

container.scope_context.invalidate_session("user-abc")  # destroy session cache
```

---

## 5. Decorators Reference

### Scope Decorators

| Decorator | Scope | Instance lifetime |
|---|---|---|
| `@Component` | `DEPENDENT` | New instance on every `get()` call |
| `@Singleton` | `SINGLETON` | One instance for the container's lifetime |
| `@RequestScoped` | `REQUEST` | One instance per `scope_context.request()` block |
| `@SessionScoped` | `SESSION` | One instance per `scope_context.session(id)` block |

```python
from providify import Component, Singleton, RequestScoped, SessionScoped

@Singleton
class DatabasePool:
    pass

@RequestScoped
class RequestContext:
    pass
```

### Qualifier and Priority

```python
from providify import Singleton, Named, Priority

@Singleton
@Named("smtp")
@Priority(10)
class SmtpMailer:
    pass

# Resolved with:
mailer = container.get(Mailer, qualifier="smtp")
```

### Provider Functions

```python
from providify import Provider

@Provider
def create_db_pool(config: Inject[Config]) -> DatabasePool:
    return DatabasePool(config.dsn)

@Provider(singleton=True)
async def create_cache(config: Inject[Config]) -> Redis:
    return await Redis.connect(config.redis_url)
```

### Lifecycle Hooks

```python
from providify import Singleton, PostConstruct, PreDestroy

@Singleton
class Database:
    @PostConstruct
    async def connect(self) -> None:
        self._pool = await create_pool()

    @PreDestroy
    async def disconnect(self) -> None:
        await self._pool.close()
```

### Configuration Modules (Spring-style)

```python
from providify import Configuration, Provider, Inject

@Configuration
class InfraConfig:
    def __init__(self, settings: Inject[Settings]) -> None:
        self._settings = settings

    @Provider(singleton=True)
    def database(self) -> Database:
        return Database(self._settings.db_url)

    @Provider
    def mailer(self) -> Mailer:
        return SmtpMailer(self._settings.smtp_host)

# Install the config module:
container.install(InfraConfig)
```

---

## 6. Injection Type Annotations

These are used as constructor parameter type hints to control how the container resolves dependencies.

```python
from providify import Inject, InjectInstances, Lazy

class AlertService:
    def __init__(
        self,
        notifier: Inject[Notifier],                        # required, resolves single binding
        notifier: Inject[Notifier, qualifier="sms"],       # with qualifier
        notifier: Inject[Notifier, optional=True],         # None if not bound
        all_notifiers: InjectInstances[Notifier],          # list of all matching bindings
        lazy_svc: Lazy[HeavyService],                      # deferred — resolved on first access
    ) -> None:
        # lazy_svc is a LazyProxy[HeavyService] — access triggers resolution
        self._svc = lazy_svc
```

**`Lazy[T]`** is the key tool for breaking circular dependencies: A → B → A can be resolved if one side uses `Lazy[A]`.

---

## 7. Generic Type Support (Added in `generic_support` branch)

Providify supports binding and resolving parameterized generic types:

```python
from typing import Generic, TypeVar
from abc import ABC, abstractmethod

T = TypeVar("T")

class Repository(ABC, Generic[T]):
    @abstractmethod
    def find(self, id: int) -> T: ...

@Component
class UserRepository(Repository[User]):
    def find(self, id: int) -> User: ...

# Container resolves the parameterized generic:
repo = container.get(Repository[User])
```

**How it works:** `utils.py` provides `_is_generic_subtype()` and `_interface_matches()` that handle all four matching cases: concrete↔concrete, concrete↔generic, generic↔generic, generic↔concrete.

---

## 8. Scopes Deep Dive

```
DEPENDENT   → No caching. New instance on every get() call.
SINGLETON   → Cached in container._singleton_cache. Lives until shutdown().
REQUEST     → Cached in ScopeContext per ContextVar token. Lives until request() block exits.
SESSION     → Cached in ScopeContext keyed by session ID. Survives request() blocks within same session.
```

**Scope violation detection:** The container validates that short-lived dependencies are not injected into longer-lived components (e.g., a `DEPENDENT` component injected into a `SINGLETON`). This fires during `validate_bindings()` and raises `ScopeViolationDetectedError`.

**`@Inheritable`:** Marks DI metadata as inheritable via MRO. Without this, subclasses don't automatically inherit scope from their parent.

---

## 9. Module Auto-Discovery

```python
# Scan a module path — discovers all @Component, @Singleton, etc. decorated classes/functions
container.scan("myapp.services")
container.scan("myapp", recursive=True)   # all submodules recursively
```

**What the scanner does:**
- Inspects all module members for DI metadata
- Auto-binds to ABCs when a class implements abstract base classes
- Skips private members (prefixed `_`) and re-exports (imported from elsewhere)
- Idempotent — safe to call multiple times on the same module

---

## 10. Dependency Visualization

```python
# Single binding tree
descriptor = binding.describe(container)
print(descriptor)
# AlertService [SINGLETON]
# └── EmailNotifier [DEPENDENT]
#     └── SmtpClient [SINGLETON]

# Full container snapshot
container_desc = container.describe()
import json
print(json.dumps(container_desc.to_dict(), indent=2))
```

`BindingDescriptor.scope_leak` — `True` if the binding directly injects a shorter-lived dependency.

---

## 11. Error Types

| Exception | When raised |
|---|---|
| `CircularDependencyError` | A → B → A cycle detected during resolution |
| `ScopeViolationDetectedError` | Short-lived dep injected into long-lived component |
| `ClassBindingNotDecoratedError` | `register(cls)` called with an undecorated class |
| `ProviderBindingNotDecoratedError` | `provide(fn)` called with an undecorated function |
| `ClassAlreadyDecorated` | Decorator applied twice to the same class |
| `ProviderAlreadyDecorated` | `@Provider` applied twice to the same function |
| `BindingError` | Base class for binding-level errors |
| `ValidationError` | Base class for validation errors |

---

## 12. Testing Patterns

Tests live in `tests/`. Each test class is fully self-contained.

**Key fixtures (in `conftest.py`):**
- `container` — fresh `DIContainer` instance per test
- `reset_global_container` (autouse) — resets the global singleton before/after every test

**Test file map:**

| File | What it covers |
|---|---|
| `test_container.py` | bind/register/provide/get/get_all/current/scoped |
| `test_scopes.py` | All four scopes, scope violation, @Inheritable |
| `test_inject.py` | Inject[T], InjectInstances[T], optional |
| `test_lazy.py` | LazyProxy, Lazy[T], circular-via-lazy |
| `test_lifecycle.py` | @PostConstruct, @PreDestroy, shutdown |
| `test_async.py` | aget/aget_all, async providers, async context managers |
| `test_configuration.py` | @Configuration, install/ainstall |
| `test_circular.py` | CircularDependencyError, diamond deps |
| `test_generics.py` | Generic[T] binding and resolution |
| `test_scanner.py` | scan(), recursive, ABC auto-binding |
| `test_describe.py` | BindingDescriptor, ASCII trees, JSON |
| `test_warmup.py` | warm_up/awarm_up, validation |

**Run tests:**
```bash
cd tests
poetry install
poetry run pytest
```

---

## 13. Architecture Layers (Bottom → Top)

```
┌──────────────────────────────────────────────────┐
│ Public API: decorators/, type.py, __init__.py    │  ← User-facing surface
├──────────────────────────────────────────────────┤
│ Container: container.py                          │  ← Registry + orchestration
├──────────────────────────────────────────────────┤
│ Bindings: binding.py                             │  ← Creation strategies
├──────────────────────────────────────────────────┤
│ Resolution: resolution.py, scope.py              │  ← Caching + cycle detection
├──────────────────────────────────────────────────┤
│ Metadata: metadata.py                            │  ← Type-safe metadata storage
├──────────────────────────────────────────────────┤
│ Utilities: utils.py, descriptor.py, scanner.py  │  ← Generic types + discovery
└──────────────────────────────────────────────────┘
```

---

## 14. Known Gotchas

- **Async providers called from sync context** → raises `RuntimeError`. Always use `aget()` for async providers.
- **`@PostConstruct` / `@PreDestroy` on non-singleton** → hooks won't fire on `DEPENDENT` instances (nothing to track). Only singleton caches are destroyed on `shutdown()`.
- **`Lazy[T]` `.get()` / `.aget()` must match context** → if resolved inside an async context, call `.aget()` on the proxy, not `.get()`.
- **`scan()` auto-binds to ABCs** → if a class implements multiple ABCs, it is registered once per ABC. This can cause surprising `get_all()` results.
- **`validate_bindings()` is called once** → after the first `get()` call, new bindings added via `bind()` are NOT re-validated automatically. Call `validate_bindings()` manually if you add bindings after the first resolution.
- **`@Inheritable` is opt-in** → subclasses do not inherit scope decorators unless the parent is also decorated with `@Inheritable`.

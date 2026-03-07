from __future__ import annotations

# ─────────────────────────────────────────────────────────────────
#  DIModule / @Configuration — Spring-style module grouping
#
#  DESIGN: @Configuration marks a class as a DI configuration module.
#  The module class:
#    1. Can declare constructor dependencies — the container injects them
#       at install() time (Spring-style, unlike Guice where modules are
#       plain objects with no injection).
#    2. Groups related @Provider methods as instance methods.
#       Having the module instance lets providers access injected deps
#       (e.g. self._config.db_url) — which is the main reason to prefer
#       the Spring style.
#
#  Usage:
#      @Configuration
#      class DatabaseModule:
#          def __init__(self, config: AppConfig) -> None:
#              self._config = config   # injected at install() time
#
#          @Provider(singleton=True)
#          def connection_pool(self) -> DatabasePool:
#              return DatabasePool(self._config.db_url)
#
#      container.install(DatabaseModule)
#
#  Thread safety:  ✅ Safe — @Configuration only stamps a bool on the class.
#  Async safety:   ✅ Safe — stateless marker, no async state.
# ─────────────────────────────────────────────────────────────────

_MODULE_ATTR = "__di_module__"


def _is_module(cls: type) -> bool:
    """Return True if *cls* was decorated with @Configuration.

    Uses own __dict__ only — does not walk MRO — so subclasses of a
    @Configuration class are not treated as modules themselves.
    """
    return bool(cls.__dict__.get(_MODULE_ATTR))


def Configuration(cls: type) -> type:
    """Mark a class as a DI configuration module.

    A @Configuration class groups related @Provider methods. When installed
    via ``container.install()``, the container:

    1. Instantiates the module class with its constructor deps injected
       (Spring-style — useful when providers need shared config objects).
    2. Finds every ``@Provider``-decorated method on the class.
    3. Registers each as a bound-method binding, so ``self`` refers to
       the live module instance with its injected deps already set.

    Notes:
        - The module class is NOT registered as a component itself —
          it exists only to group providers.
        - Constructor deps must already be registered before calling
          ``install()`` (they are resolved eagerly at install time).
        - For modules whose constructor deps are async-only, use
          ``await container.ainstall()`` instead.

    Args:
        cls: The class to mark as a configuration module.

    Returns:
        The same class, with ``__di_module__ = True`` stamped on it.

    Example:
        @Configuration
        class InfraModule:
            def __init__(self, settings: Settings) -> None:
                self._settings = settings

            @Provider(singleton=True)
            def db_pool(self) -> DatabasePool:
                return DatabasePool(self._settings.db_url)
    """
    setattr(cls, _MODULE_ATTR, True)
    return cls

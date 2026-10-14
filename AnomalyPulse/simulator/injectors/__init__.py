"""Injector registry (M3-1).

An injector is added by dropping a module into this package that defines an
Injector subclass under @register. Nothing else needs editing: get() and names()
import every module here the first time they are called. Tests do not belong in
this package; they go beside it as simulator/test_<name>.py.
"""
import importlib
import pkgutil
import re

from simulator import manifest
from simulator.control_plane import MIN_LEAD_MS, SERVICES
from simulator.injectors.base import Activation, Context, ControlPlaneInjector, Injector

_NAME = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")

_registry: dict[str, type[Injector]] = {}
_discovered = False


def _problem_with(cls) -> str | None:
    """Why `cls` cannot be registered, or None if it can."""
    if not (isinstance(cls, type) and issubclass(cls, Injector)):
        return "it is not an Injector subclass"
    if cls.__abstractmethods__:
        return f"it does not implement {', '.join(sorted(cls.__abstractmethods__))}"

    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return "name must be lowercase words joined by hyphens, e.g. 'payment-failure'"
    if _registry.get(name) not in (None, cls):
        return f"the name {name!r} is already taken by {_registry[name].__name__}"

    incident_type = getattr(cls, "incident_type", None)
    if incident_type not in manifest.INCIDENT_TYPES:
        return f"incident_type must be one of {', '.join(manifest.INCIDENT_TYPES)}"
    if incident_type == "NORMAL":
        if cls.parameter is not None:
            return "a NORMAL injector injects nothing, so it has no parameter"
    elif not isinstance(cls.parameter, str) or not cls.parameter:
        return "parameter must name the native parameter its band is expressed in"

    if not isinstance(cls.targets, (tuple, list)) or len(set(cls.targets)) != len(cls.targets) \
            or any(target not in SERVICES for target in cls.targets):
        return f"targets must be a tuple of distinct services from {', '.join(SERVICES)}"
    if not isinstance(cls.lead_ms, int) or isinstance(cls.lead_ms, bool) or cls.lead_ms < 0:
        return "lead_ms must be a whole number of milliseconds, 0 or more"
    if not isinstance(cls.needs_workload, bool):
        return "needs_workload must be True or False"

    if issubclass(cls, ControlPlaneInjector):
        if not cls.targets:
            return "a control-plane injector needs at least one target service"
        if cls.lead_ms < MIN_LEAD_MS:
            return f"a control-plane injector's lead_ms cannot be below {MIN_LEAD_MS}"
    return None


def register(cls):
    """Class decorator: add an Injector subclass to the registry under its `name`.

    Raises TypeError at import time for a class that does not meet the
    interface, so a mistake shows up before any experiment runs.
    """
    problem = _problem_with(cls)
    if problem is not None:
        raise TypeError(f"cannot register {getattr(cls, '__name__', cls)!r}: {problem}")
    _registry[cls.name] = cls
    return cls


def _discover() -> None:
    """Import every module in this package so that each @register line has run."""
    global _discovered
    if _discovered:
        return
    for module in pkgutil.iter_modules(__path__):
        if not module.name.startswith("test_"):
            importlib.import_module(f"{__name__}.{module.name}")
    # Set last: if a module failed to import, the next call tries again and
    # fails the same loud way instead of quietly running without that injector.
    _discovered = True


def get(name: str) -> type[Injector]:
    """The injector class registered as `name`. Raises LookupError if there is none."""
    _discover()
    if name not in _registry:
        raise LookupError(f"no injector named {name!r}; registered: {', '.join(sorted(_registry))}")
    return _registry[name]


def names() -> list[str]:
    _discover()
    return sorted(_registry)

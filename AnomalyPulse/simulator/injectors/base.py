"""The injector interface (M3-1).

An injector creates one kind of fault. Each one is a subclass of Injector in its
own file in this package, registered by name:

    from simulator.injectors import Injector, register

    @register
    class TrafficSpike(Injector):
        name = "traffic-spike"
        incident_type = "TRAFFIC_SPIKE"
        parameter = "multiplier"
        ...

The framework takes every injector through the same steps:

    configure   class method, pure: intensity + band + seeded RNG -> params
    start       begin injecting
    stop        stop injecting, at the end of the window or on abort
    observe     after the run: what was measured, for the manifest

One instance handles one activation: one fault, on one target, for one time
window. A cascade or a confounder is several activations, each with its own
instance.
"""
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

import boto3

from simulator import manifest
from simulator.control_plane import MIN_LEAD_MS, ControlPlane


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Activation:
    """One planned fault: what an injector instance is asked to do."""
    experiment_id: str
    index: int
    injector: str
    target: str | None
    intensity: float | None
    band: dict | None
    params: dict
    active_from_ms: int
    active_until_ms: int

    @property
    def fault_id(self) -> str:
        return manifest.fault_id(self.experiment_id, self.index)

    def manifest_entry(self, *, observed: dict | None = None, active_from_ms: int | None = None,
                       active_until_ms: int | None = None) -> dict:
        """This activation as an entry for the manifest's `activations` list.

        Pass a window bound only where the fault really began or ended at a
        different time than planned, and `observed` once the run is over.
        """
        return manifest.build_activation(
            self.experiment_id, self.index,
            injector=self.injector, target=self.target, intensity=self.intensity,
            band=self.band, params=self.params, observed=observed,
            active_from_ms=self.active_from_ms if active_from_ms is None else active_from_ms,
            active_until_ms=self.active_until_ms if active_until_ms is None else active_until_ms,
        )


@dataclass(frozen=True)
class Context:
    """What the framework hands every injector instance."""
    experiment_id: str
    session: boto3.Session        # under the simulator role; for any AWS call the injector makes
    control_plane: ControlPlane
    workload: object = None       # the workload driver (M3-2); None when the run has none


class Injector(ABC):
    """Base class for every injector. Subclass it, set the attributes, add @register.

    Rules every injector follows:
    - Never hardcode a magnitude. The value comes from the band passed to
      configure(), so M4 can keep training and test bands apart.
    - Everything resolved or measured goes in the manifest only (Tier-2). Never
      put it in a telemetry record, and never have a handler log it.
    - Do not change persistent infrastructure settings (table capacity, Lambda
      configuration). Nothing would restore them if the simulator crashed.
    - start() and stop() return quickly. Work that lasts the whole window runs
      in a thread the injector starts in start() and ends in stop().
    """

    # Registry name. Also recorded as `injector` in the manifest and control plane.
    name: str
    # The label a run of this injector alone gets. One of manifest.INCIDENT_TYPES.
    # An injector whose type is NORMAL injects nothing and is left out of the
    # manifest's activations.
    incident_type: str
    # The native parameter the band is expressed in, e.g. "timeout_ms". None only
    # for NORMAL.
    parameter: str | None = None
    # The services this injector can be aimed at. Empty when the fault has no
    # single service behind it (a traffic spike).
    targets: tuple = ()
    # start() is called at least this long before the window opens. 0 means
    # start() is called as the window opens.
    lead_ms: int = 0

    def __init__(self, ctx: Context, activation: Activation):
        self.ctx = ctx
        self.activation = activation

    @classmethod
    @abstractmethod
    def configure(cls, intensity: float | None, band: dict | None, rng: random.Random) -> dict:
        """Resolve `intensity` (0-1) within `band` into the native params to apply.

        Must be pure: no AWS calls, no clock, no randomness except `rng`. The same
        arguments and the same RNG state must give the same dict, which is what
        makes a manifest reproducible. The dict is recorded as `params` and must
        be plain JSON (no Decimal). native_value() does the usual linear mapping.
        """

    @abstractmethod
    def start(self) -> int | None:
        """Begin injecting for self.activation's window.

        Return the epoch ms at which the fault really became active if that
        differs from activation.active_from_ms, else None.
        """

    @abstractmethod
    def stop(self) -> int | None:
        """Stop injecting and undo anything start() did.

        Called when the window closes, and also when a run fails or is aborted,
        which can be before start() was called or after it raised. So it must be
        safe to call at any time and more than once. Return the epoch ms at
        which the fault really ended if that differs from
        activation.active_until_ms, else None.
        """

    def observe(self) -> dict:
        """What this injector measured, e.g. a throttle count from CloudWatch.

        Called once, after the run's recovery period. Recorded as `observed` in
        the manifest. Plain JSON, like params.
        """
        return {}

    @staticmethod
    def native_value(intensity: float, band: dict) -> float:
        """Where `intensity` falls in `band`, linearly: 0 is band low, 1 is band high."""
        return band["low"] + intensity * (band["high"] - band["low"])

    @classmethod
    def resolve_target(cls, target: str | None = None) -> str | None:
        """The service an activation is aimed at, checked against `targets`.

        With no `target` given, an injector with exactly one possible target
        gets that one.
        """
        if not cls.targets:
            if target is not None:
                raise ValueError(f"{cls.name}: takes no target")
            return None
        if target is None:
            if len(cls.targets) == 1:
                return cls.targets[0]
            raise ValueError(f"{cls.name}: choose a target from {', '.join(cls.targets)}")
        if target not in cls.targets:
            raise ValueError(f"{cls.name}: target must be one of {', '.join(cls.targets)}, "
                             f"got {target!r}")
        return target

    @classmethod
    def plan(cls, experiment_id: str, index: int, *, active_from_ms: int, active_until_ms: int,
             rng: random.Random, intensity: float | None = None, band: dict | None = None,
             target: str | None = None) -> Activation:
        """Check the inputs, call configure(), and return the Activation to run.

        `index` numbers the activations within the experiment (0, 1, ...).
        Raises ValueError for inputs this injector cannot take.
        """
        if active_from_ms >= active_until_ms:
            raise ValueError(f"{cls.name}: the window must end after it starts")
        if cls.incident_type == "NORMAL":
            if intensity is not None or band is not None:
                raise ValueError(f"{cls.name}: injects nothing, so it takes no intensity or band")
        else:
            cls._check_intensity_and_band(intensity, band)
            band = dict(band)
        target = cls.resolve_target(target)

        params = cls.configure(intensity, band, rng)
        if not isinstance(params, dict):
            raise TypeError(f"{cls.name}: configure() must return a dict, "
                            f"got {type(params).__name__}")

        return Activation(
            experiment_id=experiment_id, index=index, injector=cls.name, target=target,
            intensity=intensity, band=band, params=params,
            active_from_ms=active_from_ms, active_until_ms=active_until_ms,
        )

    @classmethod
    def _check_intensity_and_band(cls, intensity, band) -> None:
        if not _is_number(intensity) or not 0 <= intensity <= 1:
            raise ValueError(f"{cls.name}: intensity must be a number from 0 to 1, "
                             f"got {intensity!r}")
        if not isinstance(band, dict) or set(band) != {"parameter", "low", "high"}:
            raise ValueError(f"{cls.name}: band must have exactly parameter, low and high")
        if band["parameter"] != cls.parameter:
            raise ValueError(f"{cls.name}: band is for {band['parameter']!r}, "
                             f"but this injector's parameter is {cls.parameter!r}")
        if not _is_number(band["low"]) or not _is_number(band["high"]):
            raise ValueError(f"{cls.name}: band low and high must be numbers")
        if band["low"] > band["high"]:
            raise ValueError(f"{cls.name}: band low is above band high")


class ControlPlaneInjector(Injector):
    """An injector whose fault is applied by a service's own handler.

    start() schedules a fault-config item for the activation's target and
    window; the handler reads it through fault_config.active_faults() and acts
    on `injector` and `params`. A subclass only sets the class attributes and
    writes configure(). What the handler does with the params is the injector
    owner's side of the work.
    """

    lead_ms = MIN_LEAD_MS

    def start(self) -> None:
        activation = self.activation
        self.ctx.control_plane.schedule(
            service=activation.target, fault_id=activation.fault_id, injector=self.name,
            params=activation.params, active_from_ms=activation.active_from_ms,
            active_until_ms=activation.active_until_ms,
        )

    def stop(self) -> None:
        # The window closes by itself, so on the normal path this only tidies
        # the table. On abort it is what ends the fault.
        self.ctx.control_plane.cancel(self.activation.target, self.activation.fault_id)

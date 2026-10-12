"""The no-op injector: goes through every step and injects nothing.

It is what a NORMAL (baseline) run uses, and the smallest example of the
interface. Nothing is injected, so a no-op run records no activation in its
manifest.
"""
from simulator.injectors import Injector, register


@register
class NoOp(Injector):
    name = "noop"
    incident_type = "NORMAL"

    @classmethod
    def configure(cls, intensity, band, rng):
        return {}

    def start(self):
        return None

    def stop(self):
        return None

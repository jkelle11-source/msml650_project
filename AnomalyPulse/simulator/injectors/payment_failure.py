"""Payment-failure injector (M3-4).

Makes the payment handler slow and then fail while the fault window is open.
The handler reads this fault from the fault-config table; this file only
decides what the fault's parameters are.
"""
from simulator.injectors import ControlPlaneInjector, register


@register
class PaymentFailure(ControlPlaneInjector):
    name = "payment-failure"
    incident_type = "DEPENDENCY_FAILURE"
    parameter = "timeout_ms"
    targets = ("payment-service",)

    @classmethod
    def configure(cls, intensity, band, rng):
        return {"timeout_ms": round(cls.native_value(intensity, band))}
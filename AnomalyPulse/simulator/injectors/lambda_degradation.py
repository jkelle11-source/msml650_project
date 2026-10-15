"""Lambda-degradation injector (M3-4).

Makes a service's Lambda do extra computation on each request while the fault
window is open, which raises lambda_duration_ms (CPU time). The handler reads
this fault from the fault-config table; this file only decides the parameters.
"""
from simulator.injectors import ControlPlaneInjector, register


@register
class LambdaDegradation(ControlPlaneInjector):
    name = "lambda-degradation"
    incident_type = "LAMBDA_DEGRADATION"
    parameter = "added_latency_ms"
    targets = ("product-service", "cart-service", "order-service", "payment-service")

    @classmethod
    def configure(cls, intensity, band, rng):
        return {"added_latency_ms": round(cls.native_value(intensity, band))}
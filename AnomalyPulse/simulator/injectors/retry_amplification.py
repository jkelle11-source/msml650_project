"""Retry amplification (M3-5): timeouts make users retry, and retries add traffic.

This is the last link of the cascade in the project plan:

    ... requests time out -> users retry -> traffic increases further -> (loop)

The injector does not send any traffic itself. It switches on a retry behaviour
in the workload: from `start()` until `stop()`, a workload request that times
out or fails is sent again, `retries_per_failure` times on average (2.5 means
two retries, and a third half of the time). The feedback loop is real because
the retries answer to what the application actually does. The slower the
downstream services get, the more requests time out, and the more retries the
workload sends.

What it needs from the workload (agree this with M3-2). `ctx.workload` must have:

    set_retry_policy(retries_per_failure: float)
        Retry failed or timed-out requests that many times on average. 0
        switches retries off. Must be safe to call at any time, and repeatedly.

and may have:

    retry_stats() -> dict
        What the retries did, e.g. {"retries_sent": 1830, "requests_retried": 940}.
        Plain JSON. Recorded as `observed`.

The names are this injector's request, not an existing API: if the workload
calls them something else, change them in `start()`, `stop()` and `observe()`.

Use it inside a cascade plan (simulator/cascade.py), after the stages that make
requests time out. Its label is CASCADING_FAILURE because that is the only
experiment it makes sense in.
"""
from simulator.injectors import Injector, register


@register
class RetryAmplification(Injector):
    name = "retry-amplification"
    incident_type = "CASCADING_FAILURE"
    parameter = "retries_per_failure"
    # targets stays (): the retries come from the users, not from one service.

    @classmethod
    def configure(cls, intensity, band, rng):
        retries = cls.native_value(intensity, band)
        if retries < 0:
            raise ValueError(f"{cls.name}: retries_per_failure cannot be negative, got {retries}")
        return {"retries_per_failure": round(retries, 3)}

    def start(self):
        workload = self.ctx.workload
        if not hasattr(workload, "set_retry_policy"):
            # Failing here is deliberate. A run that quietly skipped the
            # amplification would be recorded as having had it.
            raise RuntimeError(f"{self.name}: this run's workload cannot retry "
                               f"(it has no set_retry_policy)")
        params = self.activation.params
        workload.set_retry_policy(params["retries_per_failure"])
        return None

    def stop(self):
        # Safe before start(), after a failed start(), and twice: switching
        # retries off when they are already off changes nothing.
        workload = self.ctx.workload
        if hasattr(workload, "set_retry_policy"):
            workload.set_retry_policy(0)
        return None

    def observe(self):
        stats = getattr(self.ctx.workload, "retry_stats", None)
        return dict(stats()) if callable(stats) else {}

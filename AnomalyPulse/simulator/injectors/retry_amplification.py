"""Retry amplification (M3-5): timeouts make users retry, and retries add traffic.

This is the last link of the cascade in the project plan:

    ... requests time out -> users retry -> traffic increases further -> (loop)

The injector does not send any traffic itself. It switches on a retry behaviour
in the workload: from `start()` until `stop()`, a workload request that fails
(a 5xx or no response) is sent again, `retries_per_failure` times on average
(2.5 means two retries, and a third half of the time), each a second after the
attempt before it failed, until one succeeds. The feedback loop is real because
the retries answer to what the application actually does. The slower the
downstream services get, the more requests fail, and the more retries the
workload sends.

It uses `Workload.set_retry_policy()` and `Workload.retry_stats()`
(simulator/workload.py), which define exactly what counts as a failure and how
retries are counted. `retry_stats()` is recorded as `observed`.

Use it inside a cascade plan (simulator/cascade.py), after the stages that make
requests fail. Its label is CASCADING_FAILURE because that is the only
experiment it makes sense in, and a CASCADING_FAILURE manifest needs two or more
faults active at once, so a run of this injector alone is refused.
"""
from simulator.injectors import Injector, register


@register
class RetryAmplification(Injector):
    name = "retry-amplification"
    incident_type = "CASCADING_FAILURE"
    parameter = "retries_per_failure"
    # targets stays (): the retries come from the users, not from one service.
    # The runner refuses a plan with this injector and no workload.
    needs_workload = True

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

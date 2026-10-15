"""Traffic-spike injector (M3-2): the MVP incident.

Multiplies the workload's arrival rate for the fault's window, e.g. 20 rps x 10
= 200 rps, then puts it back. The multiplier comes from the band
(`--band 2 4` with `--intensity 0.5` is 3x), never from this file.

Nothing is changed on the stack: the fault is more traffic from the simulator
itself, so there is nothing to undo if the run dies (the workload dies with it).
"""
import time

from simulator.injectors import Injector, register


@register
class TrafficSpike(Injector):
    name = "traffic-spike"
    incident_type = "TRAFFIC_SPIKE"
    parameter = "multiplier"
    # targets stays (): a spike has no single service behind it.
    # The runner refuses a plan with this injector and no workload.
    needs_workload = True

    @classmethod
    def configure(cls, intensity, band, rng):
        multiplier = round(cls.native_value(intensity, band), 3)
        if multiplier <= 1:
            raise ValueError(f"{cls.name}: the multiplier must be above 1 to be a spike, "
                             f"got {multiplier:g} (check --band)")
        return {"multiplier": multiplier}

    def __init__(self, ctx, activation):
        super().__init__(ctx, activation)
        self._started = None   # (monotonic s, workload outcomes) when the spike began
        self._stopped = None   # same, when it ended

    def _workload(self):
        workload = self.ctx.workload
        if not (hasattr(workload, "set_rate_multiplier") and hasattr(workload, "outcomes")):
            raise RuntimeError(f"{self.name}: needs a workload that can change its rate "
                               f"(simulator.workload.Workload)")
        return workload

    def start(self):
        workload = self._workload()
        workload.set_rate_multiplier(self.activation.params["multiplier"])
        self._started = (time.monotonic(), workload.outcomes())
        return None

    def stop(self):
        # Safe before start(), after a failed start(), and twice.
        workload = self.ctx.workload
        if not hasattr(workload, "set_rate_multiplier"):
            return None
        workload.set_rate_multiplier(1)
        if self._started is not None and self._stopped is None:
            self._stopped = (time.monotonic(), workload.outcomes())
        return None

    def observe(self):
        """What the client sent and got back during the window (Tier-2, manifest only).

        `responses` counts by class the responses that came back inside the
        window. Requests throttled before reaching a handler land in telemetry
        as nothing at all, so their 5xx here is the only record of them.
        """
        if self._started is None or self._stopped is None:
            return {}
        seconds = self._stopped[0] - self._started[0]
        if seconds <= 0:
            return {}
        (_, before), (_, after) = self._started, self._stopped
        sent = after["sent"] - before["sent"]
        classes = {"2xx", "4xx", "5xx", "error"} | set(before["responses"]) | set(after["responses"])
        return {
            "requests_sent": sent,
            "achieved_rps": round(sent / seconds, 2),
            "requests_dropped": after["dropped"] - before["dropped"],
            "responses": {k: after["responses"].get(k, 0) - before["responses"].get(k, 0)
                          for k in sorted(classes)},
        }

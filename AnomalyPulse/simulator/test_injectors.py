import json
import random
import sys
import time
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

# simulator/ is a package, so it is imported by name from the directory above
# it. pytest's default import mode already puts that directory on the path;
# this line keeps the import working under any other mode or runner.
sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import control_plane as cp
from simulator import injectors
from simulator import manifest as mf
from simulator.injectors import Activation, Context, ControlPlaneInjector, Injector

TABLE = "anomalypulse-fault-config"
BUCKET = "anomalypulse-telemetry-000000000000-us-east-2"
EXPERIMENT_ID = "exp-20261012T041500Z-3f9a"
MINUTE = 60_000

# A run that starts at T0 with its fault active from minute 5 to minute 10.
T0 = 1_791_778_500_000
START = T0 + 5 * MINUTE
END = T0 + 10 * MINUTE

BAND = {"parameter": "added_latency_ms", "low": 200, "high": 500}


# --- helpers ---------------------------------------------------------------

def _injector_class(base=Injector, without=(), **attributes):
    # A complete, valid injector class. Tests override or drop one thing at a
    # time to show what the registry and plan() insist on.
    def configure(cls, intensity, band, rng):
        return {"added_latency_ms": cls.native_value(intensity, band)}

    body = {
        "name": "test-fault",
        "incident_type": "LAMBDA_DEGRADATION",
        "parameter": "added_latency_ms",
        "targets": ("product-service", "order-service"),
        "configure": classmethod(configure),
        "start": lambda self: None,
        "stop": lambda self: None,
    }
    body.update(attributes)
    for key in without:
        del body[key]
    return type("TestFault", (base,), body)


def _plan(cls, **overrides):
    fields = {
        "active_from_ms": START,
        "active_until_ms": END,
        "rng": random.Random(7),
        "intensity": 0.5,
        "band": BAND,
        "target": "order-service",
    }
    fields.update(overrides)
    return cls.plan(EXPERIMENT_ID, 0, **fields)


def _context(control_plane=None, workload=None):
    return Context(experiment_id=EXPERIMENT_ID, session=None, control_plane=control_plane,
                   workload=workload)


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def plane():
    # A real ControlPlane over a stubbed table: a queued response is checked
    # against the expected parameters and returned without touching the
    # network, and a call with nothing queued raises. So no test in this file
    # can reach AWS, even by mistake. The credentials are placeholders.
    resource = boto3.resource("dynamodb", region_name="us-east-2",
                              aws_access_key_id="testing", aws_secret_access_key="testing")
    table = resource.Table(TABLE)
    with Stubber(table.meta.client) as stubber:
        yield cp.ControlPlane(table), stubber


# --- registry --------------------------------------------------------------

def test_register_returns_the_class_and_makes_it_gettable(registry):
    cls = _injector_class()
    assert injectors.register(cls) is cls  # so it works as a decorator
    assert injectors.get("test-fault") is cls
    assert "test-fault" in injectors.names()


def test_names_are_sorted(registry):
    injectors.register(_injector_class(name="zz-last"))
    injectors.register(_injector_class(name="aa-first"))
    assert injectors.names() == sorted(injectors.names())


def test_unknown_name_says_what_is_registered():
    with pytest.raises(LookupError, match="no injector named 'traffic-spke'; registered: .*noop"):
        injectors.get("traffic-spke")


def test_two_injectors_cannot_share_a_name(registry):
    # Otherwise whichever module happened to be imported last would win.
    injectors.register(_injector_class())
    with pytest.raises(TypeError, match="already taken by TestFault"):
        injectors.register(_injector_class())


def test_registering_the_same_class_twice_is_harmless(registry):
    cls = injectors.register(_injector_class())
    assert injectors.register(cls) is cls


@pytest.mark.parametrize("attributes, problem", [
    ({"name": "Payment Failure"}, "lowercase words joined by hyphens"),
    ({"name": "payment_failure"}, "lowercase words joined by hyphens"),
    ({"name": ""}, "lowercase words joined by hyphens"),
    ({"name": None}, "lowercase words joined by hyphens"),
    ({"incident_type": "OUTAGE"}, "incident_type must be one of NORMAL"),
    ({"incident_type": None}, "incident_type must be one of NORMAL"),
    ({"parameter": None}, "parameter must name the native parameter"),
    ({"parameter": ""}, "parameter must name the native parameter"),
    ({"incident_type": "NORMAL"}, "a NORMAL injector injects nothing"),
    ({"targets": ("payments",)}, "targets must be a tuple of distinct services"),
    ({"targets": "payment-service"}, "targets must be a tuple of distinct services"),  # a bare string
    ({"targets": {"payment-service"}}, "targets must be a tuple of distinct services"),  # no order
    ({"targets": ("order-service", "order-service")}, "targets must be a tuple of distinct services"),
    ({"lead_ms": -1}, "lead_ms must be a whole number"),
    ({"lead_ms": 2.5}, "lead_ms must be a whole number"),
    ({"needs_workload": 1}, "needs_workload must be True or False"),
    ({"needs_workload": None}, "needs_workload must be True or False"),
])
def test_register_rejects_a_class_that_breaks_the_interface(registry, attributes, problem):
    # Checked when the module is imported, so a mistake shows up before any
    # experiment runs instead of halfway through one.
    with pytest.raises(TypeError, match=problem):
        injectors.register(_injector_class(**attributes))
    assert "test-fault" not in registry


@pytest.mark.parametrize("missing", ["configure", "start", "stop"])
def test_register_rejects_a_class_missing_a_step(registry, missing):
    with pytest.raises(TypeError, match=f"does not implement {missing}"):
        injectors.register(_injector_class(without=[missing]))


def test_register_rejects_something_that_is_not_an_injector(registry):
    class NotAnInjector:
        name = "not-an-injector"

    with pytest.raises(TypeError, match="not an Injector subclass"):
        injectors.register(NotAnInjector)


def test_a_normal_injector_has_no_parameter(registry):
    cls = _injector_class(name="another-noop", incident_type="NORMAL", parameter=None, targets=())
    assert injectors.register(cls) is cls


# --- discovery -------------------------------------------------------------------

def test_noop_is_found_without_anyone_importing_it():
    # Adding an injector is adding a file: get() imports every module in the
    # package, so there is no shared list for four people to edit.
    assert injectors.get("noop").__module__ == "simulator.injectors.noop"


def test_discovery_skips_test_modules(monkeypatch, registry):
    imported = []

    class Module:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(injectors, "_discovered", False)
    monkeypatch.setattr(injectors.pkgutil, "iter_modules",
                        lambda path: [Module("noop"), Module("test_noop")])
    monkeypatch.setattr(injectors.importlib, "import_module", imported.append)
    injectors.names()
    assert imported == ["simulator.injectors.noop"]


def test_a_module_that_fails_to_import_fails_every_lookup(monkeypatch, registry):
    # A broken injector file must not turn into "that injector does not exist":
    # a batch would then run without it. Every lookup raises until it is fixed.
    class Module:
        name = "broken"

    def fail(name):
        raise ImportError(f"cannot import {name}")

    monkeypatch.setattr(injectors, "_discovered", False)
    monkeypatch.setattr(injectors.pkgutil, "iter_modules", lambda path: [Module()])
    monkeypatch.setattr(injectors.importlib, "import_module", fail)
    with pytest.raises(ImportError):
        injectors.names()
    with pytest.raises(ImportError):
        injectors.get("noop")


# --- plan: the framework's half of "parameterize" ---------------------------------

def test_plan_returns_the_activation_to_run():
    activation = _plan(_injector_class())
    assert activation == Activation(
        experiment_id=EXPERIMENT_ID, index=0, injector="test-fault", target="order-service",
        intensity=0.5, band=BAND, params={"added_latency_ms": 350.0},
        active_from_ms=START, active_until_ms=END,
    )
    assert activation.fault_id == f"{EXPERIMENT_ID}#0"


def test_plan_numbers_fault_ids_by_index():
    cls = _injector_class()
    activation = cls.plan(EXPERIMENT_ID, 2, active_from_ms=START, active_until_ms=END,
                          rng=random.Random(7), intensity=0.5, band=BAND, target="order-service")
    assert activation.fault_id == f"{EXPERIMENT_ID}#2"


def test_native_value_maps_intensity_linearly_onto_the_band():
    band = {"parameter": "timeout_ms", "low": 2500, "high": 3500}
    assert Injector.native_value(0, band) == 2500
    assert Injector.native_value(0.8, band) == 3300  # the handoff's example
    assert Injector.native_value(1, band) == 3500


def test_same_seed_gives_the_same_params():
    # M3-1's reproducibility requirement. configure() may only draw from the RNG
    # it is handed, so the same seed resolves the same params every time.
    def configure(cls, intensity, band, rng):
        return {"added_latency_ms": cls.native_value(intensity, band), "jitter": rng.random()}

    cls = _injector_class(configure=classmethod(configure))
    first = _plan(cls, rng=random.Random(42)).params
    again = _plan(cls, rng=random.Random(42)).params
    other = _plan(cls, rng=random.Random(43)).params
    assert first == again
    assert first != other


def test_plan_keeps_its_own_copy_of_the_band():
    # The band comes from shared config; editing that later must not rewrite
    # what an activation recorded.
    band = dict(BAND)
    activation = _plan(_injector_class(), band=band)
    band["high"] = 9999
    assert activation.band == BAND


@pytest.mark.parametrize("intensity", [-0.1, 1.1, None, "0.5", True, float("nan")])
def test_plan_rejects_an_intensity_outside_zero_to_one(intensity):
    with pytest.raises(ValueError, match="intensity must be a number from 0 to 1"):
        _plan(_injector_class(), intensity=intensity)


@pytest.mark.parametrize("intensity", [0, 1, 0.5])
def test_plan_accepts_the_whole_intensity_range(intensity):
    assert _plan(_injector_class(), intensity=intensity).intensity == intensity


@pytest.mark.parametrize("band, problem", [
    (None, "band must have exactly parameter, low and high"),
    ({"parameter": "added_latency_ms", "low": 200}, "band must have exactly parameter, low and high"),
    ({**BAND, "phase": "train"}, "band must have exactly parameter, low and high"),
    ({**BAND, "parameter": "multiplier"}, "band is for 'multiplier'"),  # another injector's band
    ({**BAND, "low": "200"}, "band low and high must be numbers"),
    ({**BAND, "low": 500, "high": 200}, "band low is above band high"),
])
def test_plan_rejects_a_band_it_cannot_use(band, problem):
    with pytest.raises(ValueError, match=problem):
        _plan(_injector_class(), band=band)


@pytest.mark.parametrize("active_until_ms", [START, START - 1])
def test_plan_rejects_a_window_with_no_length(active_until_ms):
    with pytest.raises(ValueError, match="window must end after it starts"):
        _plan(_injector_class(), active_until_ms=active_until_ms)


def test_plan_rejects_params_that_are_not_a_dict():
    cls = _injector_class(configure=classmethod(lambda cls, intensity, band, rng: 350))
    with pytest.raises(TypeError, match="configure\\(\\) must return a dict, got int"):
        _plan(cls)


# --- targets -----------------------------------------------------------------------

def test_an_injector_with_one_target_needs_no_choice():
    cls = _injector_class(targets=("payment-service",))
    assert _plan(cls, target=None).target == "payment-service"


def test_an_injector_with_several_targets_must_be_told_which():
    with pytest.raises(ValueError, match="choose a target from product-service, order-service"):
        _plan(_injector_class(), target=None)


def test_target_must_be_one_the_injector_supports():
    with pytest.raises(ValueError, match="target must be one of product-service, order-service"):
        _plan(_injector_class(), target="payment-service")


def test_an_injector_without_targets_takes_none():
    # A traffic spike has no single service behind it; its target is null.
    cls = _injector_class(targets=())
    assert _plan(cls, target=None).target is None
    with pytest.raises(ValueError, match="takes no target"):
        _plan(cls, target="product-service")


# --- activation -> manifest entry -----------------------------------------------------

def test_manifest_entry_defaults_to_the_plan():
    activation = _plan(_injector_class())
    assert activation.manifest_entry() == mf.build_activation(
        EXPERIMENT_ID, 0, injector="test-fault", target="order-service", intensity=0.5,
        band=BAND, params={"added_latency_ms": 350.0}, active_from_ms=START, active_until_ms=END)


def test_manifest_entry_records_what_really_happened():
    # start() and stop() may report a different window than planned, and
    # observe() what was measured.
    entry = _plan(_injector_class()).manifest_entry(
        observed={"throttled_requests": 412}, active_from_ms=START + 2000, active_until_ms=END - 500)
    assert entry["observed"] == {"throttled_requests": 412}
    assert entry["active_from"] == mf.to_iso(START + 2000)
    assert entry["active_until"] == mf.to_iso(END - 500)


def test_a_planned_activation_makes_a_valid_manifest():
    entry = _plan(_injector_class()).manifest_entry()
    manifest = mf.build_manifest(
        experiment_id=EXPERIMENT_ID, incident_type="LAMBDA_DEGRADATION", seed=7, created_by="jake",
        started_at_ms=T0, workload={}, activations=[entry], bucket=BUCKET)
    assert mf.validate(manifest) == (True, [])


# --- the instance ------------------------------------------------------------------------

def test_instance_holds_its_context_and_activation():
    cls = _injector_class()
    context, activation = _context(workload="the workload driver"), _plan(cls)
    injector = cls(context, activation)
    assert injector.ctx is context
    assert injector.activation is activation
    assert injector.ctx.workload == "the workload driver"


def test_observe_reports_nothing_by_default():
    cls = _injector_class()
    assert cls(_context(), _plan(cls)).observe() == {}


def test_an_incomplete_injector_cannot_be_instantiated():
    cls = _injector_class(without=["stop"])
    with pytest.raises(TypeError):
        cls(_context(), None)


# --- control-plane injectors ---------------------------------------------------------------

def _control_plane_class(**attributes):
    return _injector_class(base=ControlPlaneInjector, without=["start", "stop"],
                           name="test-control-plane-fault", **attributes)


def test_control_plane_injector_starts_by_scheduling_its_fault(plane):
    # The window has to be in the real future, because schedule() checks the
    # lead against the wall clock.
    control_plane, stubber = plane
    cls = _control_plane_class()
    now = int(time.time() * 1000)
    activation = _plan(cls, active_from_ms=now + MINUTE, active_until_ms=now + 2 * MINUTE)
    stubber.add_response("put_item", {}, expected_params={
        "TableName": TABLE,
        "Item": {
            "service": "order-service",
            "fault_id": f"{EXPERIMENT_ID}#0",
            "injector": "test-control-plane-fault",
            "params": {"added_latency_ms": 350},
            "active_from_ms": now + MINUTE,
            "active_until_ms": now + 2 * MINUTE,
            "expires_at": (now + 2 * MINUTE) // 1000 + cp.EXPIRY_GRACE_SECONDS,
        },
    })
    assert cls(_context(control_plane), activation).start() is None
    stubber.assert_no_pending_responses()


def test_control_plane_injector_stops_by_cancelling_its_fault(plane):
    # Never started, then stopped twice: stop() has to be safe at any time,
    # because the runner calls it for every injector when a run fails or is
    # aborted.
    control_plane, stubber = plane
    cls = _control_plane_class()
    injector = cls(_context(control_plane), _plan(cls))
    for _ in range(2):
        stubber.add_response("delete_item", {}, expected_params={
            "TableName": TABLE,
            "Key": {"service": "order-service", "fault_id": f"{EXPERIMENT_ID}#0"}})
    assert injector.stop() is None
    assert injector.stop() is None
    stubber.assert_no_pending_responses()


def test_control_plane_injector_asks_to_be_started_ahead_of_its_window():
    assert ControlPlaneInjector.lead_ms == cp.MIN_LEAD_MS
    assert Injector.lead_ms == 0


def test_control_plane_injector_needs_a_target(registry):
    # A fault-config item lives under a service; without one there is nowhere
    # to write it.
    with pytest.raises(TypeError, match="needs at least one target service"):
        injectors.register(_control_plane_class(targets=()))


def test_control_plane_injector_cannot_shorten_the_lead(registry):
    with pytest.raises(TypeError, match="lead_ms cannot be below 10000"):
        injectors.register(_control_plane_class(lead_ms=0))


def test_control_plane_injector_registers_with_only_configure(registry):
    cls = _control_plane_class()
    assert injectors.register(cls) is cls


# --- no-op injector --------------------------------------------------------------------------

def test_noop_is_the_normal_runs_injector():
    noop = injectors.get("noop")
    assert noop.incident_type == "NORMAL"
    assert noop.parameter is None
    assert noop.targets == ()
    assert noop.lead_ms == 0


def test_noop_goes_through_every_step_and_touches_nothing():
    # The context has no session, control plane or workload, so any attempt to
    # use one would raise.
    noop = injectors.get("noop")
    activation = noop.plan(EXPERIMENT_ID, 0, active_from_ms=START, active_until_ms=END,
                           rng=random.Random(7))
    assert activation.params == {}
    assert activation.target is None
    injector = noop(_context(), activation)
    assert injector.stop() is None  # before start
    assert injector.start() is None
    assert injector.stop() is None
    assert injector.stop() is None
    assert injector.observe() == {}


def test_noop_takes_no_intensity_or_band():
    noop = injectors.get("noop")
    with pytest.raises(ValueError, match="takes no intensity or band"):
        noop.plan(EXPERIMENT_ID, 0, active_from_ms=START, active_until_ms=END,
                  rng=random.Random(7), intensity=0.5)


def test_noop_does_not_draw_from_the_rng():
    # A NORMAL run and an incident run with the same seed must leave the RNG in
    # the same state for whatever is planned next.
    rng = random.Random(7)
    before = rng.getstate()
    injectors.get("noop").plan(EXPERIMENT_ID, 0, active_from_ms=START, active_until_ms=END, rng=rng)
    assert rng.getstate() == before


# --- contract: every registered injector ------------------------------------------------------
# These run against each injector in the package, so a new injector file is
# checked against the interface as soon as it exists.

def _plan_any(cls, seed):
    if cls.incident_type == "NORMAL":
        intensity, band = None, None
    else:
        intensity, band = 0.5, {"parameter": cls.parameter, "low": 1, "high": 2}
    return cls.plan(EXPERIMENT_ID, 0, active_from_ms=START, active_until_ms=END,
                    rng=random.Random(seed), intensity=intensity, band=band,
                    target=cls.targets[0] if cls.targets else None)


@pytest.mark.parametrize("name", injectors.names())
def test_every_injector_resolves_the_same_params_from_the_same_seed(name):
    cls = injectors.get(name)
    assert _plan_any(cls, seed=1).params == _plan_any(cls, seed=1).params


@pytest.mark.parametrize("name", injectors.names())
def test_every_injectors_params_are_plain_json(name):
    # A Decimal or NaN here would fail the manifest write in the middle of a run.
    params = _plan_any(injectors.get(name), seed=1).params
    assert json.loads(json.dumps(params, allow_nan=False)) == params


@pytest.mark.parametrize("name", injectors.names())
def test_every_injectors_activation_fits_the_manifest(name):
    cls = injectors.get(name)
    activation = _plan_any(cls, seed=1)
    # A NORMAL injector injects nothing, so its run records no activation.
    entries = [] if cls.incident_type == "NORMAL" else [activation.manifest_entry()]
    manifest = mf.build_manifest(
        experiment_id=EXPERIMENT_ID, incident_type=cls.incident_type, seed=1, created_by="jake",
        started_at_ms=T0, workload={}, activations=entries, bucket=BUCKET)
    assert mf.validate(manifest) == (True, [])

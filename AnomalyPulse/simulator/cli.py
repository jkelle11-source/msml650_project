"""Incident simulator CLI (M3-1).

Run from AnomalyPulse/, with the `sim` AWS profile set up as described in
Handoffs/M3-1_Handoff.md. Start every real run under `caffeinate -is` (macOS):
a sleeping machine stops the run, and the runner then records it as FAILED.

    caffeinate -is python -m simulator start-incident --type noop --duration 60
    caffeinate -is python -m simulator start-incident --type <injector> --intensity 0.8 \\
        --duration 300 --band <low> <high>
    caffeinate -is python -m simulator start-cascade --spec simulator/cascades/<spec>.json
    python -m simulator cleanup

Add --dry-run to either command to see the manifest a run would start with,
without touching AWS. Every run sends baseline traffic for its whole length
(M3-2): 20 requests per second in the plan's endpoint mix, unless --rps,
--users or --mix say otherwise. M4 keeps the 20 rps default for every run.

    caffeinate -is python -m simulator start-incident --type noop --duration 600
    caffeinate -is python -m simulator start-incident --type traffic-spike --intensity 0.5 \\
        --band 2 4 --duration 300

M4's dataset is generated in batches on the runner host (Docs/DATASET.md):

    python -m simulator check-stack                    # the frozen-configuration check alone
    python -m simulator run-batch --queue simulator/queues/<file>.json [--dry-run]
    python -m simulator dataset-status --dataset v1
    python -m simulator export --dataset v1            # run-batch does this when it ends
"""
import argparse
import getpass
import json
import secrets
import sys
import time
from collections import Counter

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from simulator import batch, cascade, dataset, export, injectors, manifest, runner, stack_config, workload
from simulator.lock import LockHeld
from simulator.stack import Stack


def number(text: str):
    """An int when the text is one, else a float, so 2500 is recorded as 2500, not 2500.0."""
    try:
        return int(text)
    except ValueError:
        return float(text)


def _fail(message) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _connect(args) -> Stack:
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    return Stack.connect(session)


def _plan(args) -> runner.Plan:
    cls = injectors.get(args.type)
    band = None
    if cls.incident_type != "NORMAL":
        if args.intensity is None or args.band is None:
            raise ValueError(f"{args.type} needs --intensity and --band LOW HIGH "
                             f"(the band is in {cls.parameter})")
        band = {"parameter": cls.parameter, "low": args.band[0], "high": args.band[1]}
    elif args.intensity is not None or args.band is not None:
        raise ValueError(f"{args.type} injects nothing, so it takes no --intensity or --band")

    return runner.Plan.single(
        args.type,
        duration_s=args.duration, baseline_s=args.baseline, recovery_s=args.recovery,
        # No seed given: pick one, and it is recorded in the manifest, so the
        # run can still be repeated.
        seed=secrets.randbelow(2**32) if args.seed is None else args.seed,
        created_by=args.created_by or getpass.getuser(),
        intensity=args.intensity, band=band, target=args.target,
    )


def _workload(args, plan) -> workload.Workload:
    """The run's baseline traffic, from --rps, --users and --mix."""
    mix = None if args.mix is None else dict(zip(workload.ROUTES, args.mix))
    # The plan's seed, so the run's request sequence repeats with its params.
    return workload.Workload(rps=args.rps, users=args.users, mix=mix, seed=plan.seed)


def _start_incident(args) -> int:
    # Everything that can be wrong with the request is found here, before AWS
    # is involved. An error raised later, during the run, is a real failure and
    # is left to show its traceback.
    try:
        plan = _plan(args)
        load = _workload(args, plan)
        preview = runner.preview(plan, workload=load)
    except (ValueError, LookupError) as e:
        return _fail(e)
    if args.dry_run:
        print(json.dumps(preview, indent=2))
        return 0

    try:
        stack = _connect(args)
        load.base_url = workload.api_base_url(stack.session)
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return _fail(f"could not reach the stack with AWS profile {args.profile!r}: {e}")
    try:
        runner.run(plan, stack, workload=load)
    except (LockHeld, runner.FellBehind) as e:
        # Not bugs: another run holds the lock, or the machine slept mid-run.
        # Either way the reason is the message, not a traceback.
        return _fail(e)
    return 0


def _start_cascade(args) -> int:
    # Same split as _start_incident: a bad spec is reported before AWS is involved.
    try:
        plan = cascade.build_plan(
            **cascade.load_spec(args.spec),
            seed=secrets.randbelow(2**32) if args.seed is None else args.seed,
            created_by=args.created_by or getpass.getuser(),
            **({"max_run_s": args.max_run} if args.max_run is not None else {}))
        load = _workload(args, plan)
        preview = runner.preview(plan, workload=load)
    except (ValueError, LookupError, OSError) as e:
        return _fail(e)
    if args.dry_run:
        print(json.dumps(preview, indent=2))
        print("\nplanned concurrency:\n" + cascade.describe_timeline(preview))
        return 0

    try:
        stack = _connect(args)
        load.base_url = workload.api_base_url(stack.session)
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return _fail(f"could not reach the stack with AWS profile {args.profile!r}: {e}")
    try:
        final = runner.run(plan, stack, workload=load)
    except (LockHeld, runner.FellBehind) as e:
        return _fail(e)
    print("\nconcurrency:\n" + cascade.describe_timeline(final))
    return 0


def _cleanup(args) -> int:
    try:
        stack = _connect(args)
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return _fail(f"could not reach the stack with AWS profile {args.profile!r}: {e}")

    holder = stack.lock.holder()
    if holder is not None and holder["expires_at"] > time.time() and not args.force:
        return _fail(f"{LockHeld(holder)}. If that run is dead, repeat with --force; "
                     f"if it is still going, --force will break it.")

    deleted = stack.control_plane.cancel_all()
    print(f"deleted {deleted} fault(s)")
    if holder is None:
        print("no lock was held")
    elif stack.lock.release(holder["experiment_id"]):
        print(f"released the lock of {holder['experiment_id']}")
    else:
        print("the lock changed hands while cleaning up; left it alone")
    return 0


def _check_stack(args) -> int:
    try:
        config = stack_config.read(boto3.Session(profile_name=args.profile, region_name=args.region))
    except (BotoCoreError, ClientError) as e:
        return _fail(f"could not read the stack's configuration with AWS profile {args.profile!r}: {e}")
    print(json.dumps(config, indent=2))
    problems = stack_config.problems(config)
    for problem in problems:
        print(f"NOT FROZEN: {problem}", file=sys.stderr)
    if not problems:
        print("the stack has M4's frozen configuration")
    return 1 if problems else 0


def _run_batch(args) -> int:
    try:
        queue = batch.load_queue(args.queue)
    except (ValueError, LookupError, OSError) as e:
        return _fail(e)
    if args.dry_run:
        total_s = 0
        for entry in queue.entries:
            seconds = batch.run_seconds(batch.plan_for(entry, "dry-run"))
            total_s += seconds
            print(f"slot {entry['slot']:>4}  {entry['phase']:<6} {queue.incident_types[entry['slot']]:<20} "
                  f"{seconds / 60:g} min")
        print(f"dataset {queue.dataset}: {len(queue.entries)} experiments, {total_s / 3600:.1f} h of runs "
              f"before any reruns")
        return 0

    try:
        stack = _connect(args)
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return _fail(f"could not reach the stack with AWS profile {args.profile!r}: {e}")
    try:
        record = batch.run_batch(queue, stack, created_by=args.created_by,
                                 max_attempts=args.max_attempts, repin=args.repin)
    except batch.BatchRefused as e:
        return _fail(e)

    if not args.no_export:
        accepted = record["results"]["accepted_in_batch"]
        if accepted:
            # The shipper lands the last run's records within a few minutes.
            landed_ms = max(manifest.to_epoch_ms(a["ended_at"]) for a in accepted) \
                + int(export.LANDING_MARGIN.total_seconds() * 1000)
            wait_s = max(0, landed_ms / 1000 - time.time())
            print(f"exporting in {wait_s / 60:.0f} min, once the shipper has landed every record")
            time.sleep(wait_s)
        export.export_dataset(stack.session, stack.bucket, queue.dataset)
    slots = record["results"]["queue_slots"] or {}
    return 0 if len(slots.get("ACCEPTED", [])) == len(queue.entries) else 1


def _export(args) -> int:
    try:
        stack = _connect(args)
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return _fail(f"could not reach the stack with AWS profile {args.profile!r}: {e}")
    try:
        export.export_dataset(stack.session, stack.bucket, args.dataset, force=args.force)
    except export.LeakError as e:
        return _fail(e)
    return 0


def _dataset_status(args) -> int:
    try:
        stack = _connect(args)
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return _fail(f"could not reach the stack with AWS profile {args.profile!r}: {e}")
    registry = dataset.Registry(stack.s3, stack.bucket, args.dataset)
    records = registry.slots()
    print(f"dataset {args.dataset}: {len(records)} registered experiments")
    for phase in dataset.PHASES:
        rows = [r for r in records.values() if r["phase"] == phase]
        if not rows:
            continue
        statuses = Counter(r["status"] for r in rows)
        accepted = [r for r in rows if r["status"] == "ACCEPTED"]
        dates = sorted({r["generation_date"] for r in accepted})
        print(f"  {phase}: {len(rows)} slots ({', '.join(f'{s} {n}' for s, n in sorted(statuses.items()))}); "
              f"{sum(len(r['attempts']) for r in rows)} runs; generated "
              f"{dates[0] + ' to ' + dates[-1] if dates else 'not yet'}")
        print("    accepted by type: " + ", ".join(
            f"{t} {n}" for t, n in sorted(Counter(r["incident_type"] for r in accepted).items())))
    for r in sorted(records.values(), key=lambda r: r["slot"]):
        if r["status"] != "ACCEPTED":
            last = r["attempts"][-1] if r["attempts"] else None
            print(f"  slot {r['slot']} {r['status']}, {len(r['attempts'])} attempts"
                  + (f"; last: {'; '.join(last['reasons']) or last['verdict']}" if last else ""))
    batches = registry.batches()
    if batches:
        last = batches[-1]
        print(f"latest batch: {last['batch_id']} {last['status']}, {last['started_at']} to {last['ended_at']}"
              + (f"; {last['stop_reason']}" if last["stop_reason"] else ""))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m simulator",
                                     description="AnomalyPulse incident simulator.")
    commands = parser.add_subparsers(dest="command", required=True)

    def aws_arguments(command):
        command.add_argument("--profile", default="sim",
                             help="AWS profile that assumes the simulator role (default: sim)")
        command.add_argument("--region", default="us-east-2")

    def traffic_arguments(command):
        command.add_argument("--rps", type=number, default=workload.DEFAULT_RPS, metavar="RPS",
                             help="baseline traffic sent for the whole run, in requests per second "
                                  f"(default: {workload.DEFAULT_RPS}, the plan's normal rate)")
        command.add_argument("--users", type=int, default=workload.DEFAULT_USERS, metavar="N",
                             help="simulated users the traffic is spread over "
                                  f"(default: {workload.DEFAULT_USERS})")
        command.add_argument("--mix", type=number, nargs=len(workload.ROUTES), metavar="W",
                             help="endpoint weights in the order " + ", ".join(workload.ROUTES)
                                  + " (default: 60 15 10 10 5)")

    start = commands.add_parser(
        "start-incident", help="run one experiment",
        description="Run one experiment: baseline, the fault's window, recovery. "
                    "One experiment at a time can run on the shared stack.")
    start.add_argument("--type", required=True, metavar="INJECTOR",
                       help="registered injector to run; noop is a NORMAL run")
    start.add_argument("--intensity", type=float,
                       help="0-1 position within the band (not for noop)")
    start.add_argument("--duration", type=number, required=True, metavar="SECONDS",
                       help="how long the fault is active")
    start.add_argument("--band", type=number, nargs=2, metavar=("LOW", "HIGH"),
                       help="band the intensity is mapped onto, in the injector's native "
                            "parameter (not for noop)")
    start.add_argument("--target", metavar="SERVICE",
                       help="service to aim at, for an injector with more than one")
    start.add_argument("--baseline", type=number, default=300, metavar="SECONDS",
                       help="normal operation before the fault (default: 300)")
    start.add_argument("--recovery", type=number, default=300, metavar="SECONDS",
                       help="after the fault (default: 300)")
    start.add_argument("--seed", type=int,
                       help="RNG seed (default: a random one, recorded in the manifest)")
    start.add_argument("--created-by", metavar="NAME",
                       help="recorded in the manifest (default: your username)")
    traffic_arguments(start)
    start.add_argument("--dry-run", action="store_true",
                       help="print the manifest the run would start with; touches no AWS")
    aws_arguments(start)
    start.set_defaults(handler=_start_incident)

    casc = commands.add_parser(
        "start-cascade", help="run one cascading-failure experiment from a spec file",
        description="Run a cascade: several faults that begin one after another and overlap. "
                    "The spec file lists the stages, their bands and onset delays.")
    casc.add_argument("--spec", required=True, metavar="FILE", help="cascade spec (JSON)")
    casc.add_argument("--seed", type=int,
                      help="RNG seed (default: a random one, recorded in the manifest)")
    casc.add_argument("--created-by", metavar="NAME",
                      help="recorded in the manifest (default: your username)")
    casc.add_argument("--max-run", type=number, metavar="SECONDS",
                      help=f"longest run allowed (default: {cascade.MAX_RUN_S}); the workload "
                           f"sends traffic the whole time, so this bounds the cost")
    traffic_arguments(casc)
    casc.add_argument("--dry-run", action="store_true",
                      help="print the manifest and concurrency the run would start with; "
                           "touches no AWS")
    aws_arguments(casc)
    casc.set_defaults(handler=_start_cascade)

    cleanup = commands.add_parser(
        "cleanup", help="delete leftover faults and a dead run's lock",
        description="Teardown after a run that died: delete every fault-config item and "
                    "release the lock. Refuses while a lock is still live unless --force.")
    cleanup.add_argument("--force", action="store_true",
                         help="clean up even though the lock has not expired")
    aws_arguments(cleanup)
    cleanup.set_defaults(handler=_cleanup)

    check = commands.add_parser(
        "check-stack", help="check the stack has M4's frozen configuration",
        description="Print the stack's configuration and check it is M4's: 1,769 MB functions, Products "
                    "provisioned at 200 RCU, and a Scan of Products costing 0.5 RCU. Costs one Scan.")
    aws_arguments(check)
    check.set_defaults(handler=_check_stack)

    run_batch = commands.add_parser(
        "run-batch", help="run a queue of experiments and register them (M4)",
        description="Run every experiment in a queue file that the dataset's registry hasn't accepted, "
                    "checking each run, then export the dataset. Rerun the same command to resume.")
    run_batch.add_argument("--queue", required=True, metavar="FILE", help="queue file (JSON)")
    run_batch.add_argument("--max-attempts", type=int, default=batch.MAX_ATTEMPTS, metavar="N",
                           help=f"runs of one experiment before it is given up (default: {batch.MAX_ATTEMPTS})")
    run_batch.add_argument("--repin", action="store_true",
                           help="accept a stack or runner different from the one the dataset is pinned to")
    run_batch.add_argument("--no-export", action="store_true", help="don't export the dataset at the end")
    run_batch.add_argument("--created-by", metavar="NAME",
                           help="recorded in each manifest (default: m4-batch:<dataset>)")
    run_batch.add_argument("--dry-run", action="store_true",
                           help="check the queue and print what it would run; touches no AWS")
    aws_arguments(run_batch)
    run_batch.set_defaults(handler=_run_batch)

    for name, handler, text in (("export", _export, "export accepted experiments into the dataset layout"),
                                ("dataset-status", _dataset_status, "summarize a dataset's registry")):
        command = commands.add_parser(name, help=text, description=text[0].upper() + text[1:] + ".")
        command.add_argument("--dataset", default=dataset.DEFAULT_DATASET,
                             help=f"dataset name (default: {dataset.DEFAULT_DATASET})")
        if name == "export":
            command.add_argument("--force", action="store_true", help="re-export experiments already exported")
        aws_arguments(command)
        command.set_defaults(handler=handler)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        # run() has already wound the experiment down and recorded it as ABORTED.
        print("interrupted", file=sys.stderr)
        return 130

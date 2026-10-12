"""Incident simulator CLI (M3-1).

Run from AnomalyPulse/, with the `sim` AWS profile set up as described in
Handoffs/M3-1_Handoff.md:

    python -m simulator start-incident --type noop --duration 60
    python -m simulator start-incident --type <injector> --intensity 0.8 \\
        --duration 300 --band <low> <high>
    python -m simulator cleanup

Add --dry-run to start-incident to see the manifest a run would start with,
without touching AWS.
"""
import argparse
import getpass
import json
import secrets
import sys
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from simulator import injectors, runner
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


def _start_incident(args) -> int:
    # Everything that can be wrong with the request is found here, before AWS
    # is involved. An error raised later, during the run, is a real failure and
    # is left to show its traceback.
    try:
        plan = _plan(args)
        preview = runner.preview(plan)
    except (ValueError, LookupError) as e:
        return _fail(e)
    if args.dry_run:
        print(json.dumps(preview, indent=2))
        return 0

    try:
        stack = _connect(args)
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return _fail(f"could not reach the stack with AWS profile {args.profile!r}: {e}")
    try:
        runner.run(plan, stack)
    except LockHeld as e:
        return _fail(e)
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m simulator",
                                     description="AnomalyPulse incident simulator.")
    commands = parser.add_subparsers(dest="command", required=True)

    def aws_arguments(command):
        command.add_argument("--profile", default="sim",
                             help="AWS profile that assumes the simulator role (default: sim)")
        command.add_argument("--region", default="us-east-2")

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
    start.add_argument("--dry-run", action="store_true",
                       help="print the manifest the run would start with; touches no AWS")
    aws_arguments(start)
    start.set_defaults(handler=_start_incident)

    cleanup = commands.add_parser(
        "cleanup", help="delete leftover faults and a dead run's lock",
        description="Teardown after a run that died: delete every fault-config item and "
                    "release the lock. Refuses while a lock is still live unless --force.")
    cleanup.add_argument("--force", action="store_true",
                         help="clean up even though the lock has not expired")
    aws_arguments(cleanup)
    cleanup.set_defaults(handler=_cleanup)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        # run() has already wound the experiment down and recorded it as ABORTED.
        print("interrupted", file=sys.stderr)
        return 130

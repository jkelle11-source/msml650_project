"""Launch SageMaker training jobs (M5-1).
    
    cd AnomalyPulse
    python -m ml.launch smoke --dry-run     # print the requests; upload and create nothing
    python -m ml.launch smoke               # one smoke job per image, as --profile ml
    python -m ml.launch train --dry-run     # print the training request; create nothing
    python -m ml.launch train -C 0.5        # train once; train.py's default for what isn't given
    python -m ml.launch tune --dry-run      # print the tuning request; create nothing
    python -m ml.launch tune                # tune C: TUNE_JOBS training jobs, TUNE_PARALLEL at a time

Every job runs as anomalypulse-sagemaker-training with network isolation on: its code 
arrives through a `code` channel and its data through manifest channels, which list 
exactly the files it may read, from v1's training and validation runs only 
(integrity.temporal_split). The launcher never reads a feature or a label itself; the
`ml` profile can't.

`smoke` runs ml/job/smoke.py in each candidate image on slot 1 and judges the report it
writes: what reached the job, and that its network is blocked. It prints what the image 
has (Python, libraries, environment) without judging it. Exits 0 when every job completed
and passed, 1 when not, 2 when the launcher lost contact with a job that may still be
running.

`train` runs ml/job/train.py in our copy of the XGBoost image (the anomalypulse-ml
stack's training image: at AWS's own address SageMaker would let a tuning job tune
only XGBoost's settings, never C). Its `train` channel copies in v1's
slots 1-560 and its `validation` channel 561-700, each run's features and labels at their
dataset paths. It prints the package's model.json and checks it: the job read every run
sent, and its libraries are ml/requirements-image.txt's, the ones the Diagnosis Lambda
loads the model with. Exit codes as for smoke.

`tune` runs train's job under a SageMaker tuning job: the same image, code, channels and
role, with C chosen by the tuner from TUNE_RANGES and each job scored by the
validation:macro_f1 line train.py prints. It lists every job it launched, then checks the
best one's model as `train` does, and that the score the tuner read is model.json's. Exit
codes as for smoke; 1 also when any of its training jobs failed.
"""
import argparse
import io
import json
import os
import sys
import tarfile
import time

from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from simulator import dataset as ds
from simulator import integrity

REGION = "us-east-2"
DATASET = ds.DEFAULT_DATASET

IMAGES = {
    "sklearn": "257758044811.dkr.ecr.us-east-2.amazonaws.com/sagemaker-scikit-learn:1.4-2-cpu-py3",
    "xgboost": "257758044811.dkr.ecr.us-east-2.amazonaws.com/sagemaker-xgboost:3.0-5",
}
# Only ml.m5.large and ml.m5.xlarge allowed
INSTANCE_TYPE = "ml.m5.large"
MAX_RUNTIME_S = 900
POLL_S = 30
FINISHED = ("Completed", "Failed", "Stopped")
LOG_GROUP = "/aws/sagemaker/TrainingJobs"

# With no network the toolkit can't fetch the code from S3; the code channel puts it here.
SUBMIT_DIRECTORY = "/opt/ml/input/data/code/sourcedir.tar.gz"
JOB_DIR = Path(__file__).resolve().parent / "job"

# Each data channel copies in files from one tree of the dataset
CHANNEL_TREES = {"features": "tier1/features/", "labels": "tier2/labels/"}
CHANNEL_FILES = {"features": "features.jsonl", "labels": "labels.jsonl"}

SMOKE_SLOT = 1

# Sent only to see how settings arrive; smoke.py doesn't use them.
SMOKE_HYPERPARAMETERS = {"C": 0.5, "max_iter": 200}

# What the toolkit's source says it passes: names sorted, one dash for a one-letter name.
SMOKE_ARGV_PREDICTED = ["-C", "0.5", "--max_iter", "200"]
SMOKE_ENV = ("SM_MODEL_DIR", "SM_CHANNEL_CODE", "SM_CHANNEL_FEATURES", "SM_CHANNEL_LABELS")

# The job imports join_labels from beside train.py, so the bundle carries it.
TRAIN_CODE = (JOB_DIR / "train.py", JOB_DIR.parent.parent / "features" / "join_labels.py")
# {channel: its temporal_split partition}. Both channels copy in both trees.
TRAIN_CHANNELS = {"train": "train", "validation": "val"}
MODEL_FORMAT = 1
# The Diagnosis Lambda installs these versions, so the model must have been trained with them.
PINS = JOB_DIR.parent / "requirements-image.txt"

# What the tuner may set. C on a log scale: each tenfold step changes the model about as much.
TUNE_RANGES = {"ContinuousParameterRanges": [
    {"Name": "C", "MinValue": "0.001", "MaxValue": "100", "ScalingType": "Logarithmic"}]}
TUNE_JOBS = 4
TUNE_PARALLEL = 2
# Fixed, so a rerun tries the same values.
TUNE_SEED = 650
OBJECTIVE = "validation:macro_f1"
# Read from the job's log, from the lines train.py prints. train:macro_f1 shows each job's gap.
METRICS = [{"Name": "validation:macro_f1", "Regex": "validation:macro_f1=([0-9\\.]+)"},
           {"Name": "train:macro_f1", "Regex": "train:macro_f1=([0-9\\.]+)"}]
# The tuner reads a score rounded to 6 decimals; model.json keeps every digit.
SCORE_TOLERANCE = 1e-6

class LostContact(Exception):
    """A describe call failed: the job itself may still be running."""
    
def lookup(session):
    """The buckets, the training role and the training image, from the two stacks' SSM
    parameters. The image is our copy of IMAGES["xgboost"], which the smoke jobs of
    2026-10-27 chose: it has scikit-learn and xgboost (M5-2, M5-3)."""
    names = {"telemetry_bucket": "/anomalypulse/telemetry/bucket/name",
             "ml_bucket": "/anomalypulse/ml/bucket/name",
             "role_arn": "/anomalypulse/ml/training-role-arn",
             "training_image": "/anomalypulse/ml/training-image-uri"}
    found = session.client("ssm").get_parameters(Names = list(names.values()))["Parameters"]
    values = {p["Name"]: p["Value"] for p in found}
    missing = [name for name in names.values() if name not in values]
    if missing:
        raise RuntimeError(f"SSM parameters not found: {missing}")
    return {key: values[name] for key, name in names.items()}

def train_val_records(s3, bucket):
    """{slot: experiments.jsonl line} for the runs in the train or val partition.
    temporal_split also refuses a test run generated on or before the last training day."""
    body = s3.get_object(Bucket = bucket, Key = ds.index_key(DATASET))["Body"].read().decode("utf-8")
    records = [json.loads(line) for line in body.splitlines() if line.strip()]
    split = integrity.temporal_split(records)
    return {r["slot"]: r for r in records if split.get(r.get("experiment_id")) in ("train", "val")}

def list_sizes(s3, bucket, prefix):
    """{key: size} for every object under prefix."""
    sizes = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket = bucket, Prefix = prefix):
        sizes.update({o["Key"]: o["Size"] for o in page.get("Contents", [])})
    return sizes

def channel_prefix(channel):
    return f"{ds.root(DATASET)}{CHANNEL_TREES[channel]}"

def channel_files(experiment_ids):
    """{channel: keys relative to channel_prefix(channel)} for the given runs."""
    return {channel: [f"experiment_id={e}/{name}" for e in experiment_ids]
            for channel, name in CHANNEL_FILES.items()}

def check_present(s3, bucket, files):
    """{channel: {relative key: size}}, from one listing per tree. Raises if any file is
    missing: the job would fail copying it in, after it had started billing."""
    sizes, missing = {}, []
    for channel, keys in files.items():
        prefix = channel_prefix(channel)
        listed = list_sizes(s3, bucket, prefix)
        sizes[channel] = {key: listed[prefix + key] for key in keys if prefix + key in listed}
        missing += [prefix + key for key in keys if prefix + key not in listed]
    if missing:
        raise RuntimeError(f"{len(missing)} channel files not in s3://{bucket}/, e.g. {missing[:3]}")
    return sizes

def split_ids(records):
    """{channel: experiment ids in slot order}, from train_val_records: slots 1-560 to
    train, 561-700 to validation."""
    return {channel: [r["experiment_id"] for slot, r in sorted(records.items())
                      if integrity.split_of(slot) == part]
            for channel, part in TRAIN_CHANNELS.items()}

def dataset_keys(files):
    """channel_files' keys made relative to the dataset's root instead of their tree: a
    training channel holds both trees."""
    return [CHANNEL_TREES[tree] + key for tree, keys in files.items() for key in keys]

def bundle(paths):
    """sourcedir.tar.gz's bytes: each file at the top level under its own name, where the
    toolkit looks for sagemaker_program. Never a requirements.txt: the toolkit would
    pip-install it before the script runs, which fails with no network or, if it could
    reach one, would replace a library the image pins."""
    paths = [Path(p) for p in paths]
    names = [p.name for p in paths]
    if "requirements.txt" in names:
        raise ValueError("no requirements.txt in a bundle: the image's libraries are the pinned ones")
    if len(set(names)) != len(names):
        raise ValueError(f"two files in the bundle share a name: {names}")
    buf = io.BytesIO()
    with tarfile.open(fileobj = buf, mode = "w:gz") as tar:
        for path in paths:
            if not path.is_file():
                raise ValueError(f"not a file: {path}")
            tar.add(path, arcname = path.name)
    return buf.getvalue()

def manifest(bucket, prefix, relative_keys):
    """A ManifestFile channel's body: the prefix, then every file under it to copy in."""
    if not prefix.endswith("/") or any(key.startswith("/") for key in relative_keys):
        raise ValueError("the prefix ends with / and the keys are relative to it")
    return json.dumps([{"prefix": f"s3://{bucket}/{prefix}"}] + list(relative_keys))

def job_name(kind, variant, now):
    """variant: the image for smoke, the model for train."""
    name = f"anomalypulse-{kind}-{variant}-{now:%Y%m%dT%H%M%SZ}"
    if len(name) > 63:
        raise ValueError(f"training job names are at most 63 characters: {name}")
    return name

def tuning_job_name(now):
    """Each training job a tuning job launches is named <its name>-<number>-<hash>, so it
    starts with anomalypulse- too, as the trainer role requires."""
    name = f"anomalypulse-hpo-{now:%y%m%d%H%M%S}"
    if len(name) > 32:
        raise ValueError(f"tuning job names are at most 32 characters: {name}")
    return name

def code_key(name):
    return f"code/{name}/sourcedir.tar.gz"

def manifest_key(name, channel):
    return f"channels/{name}/{channel}.manifest"

def job_request(name, image_uri, entry_point, hyperparameters, config, channels = tuple(CHANNEL_TREES)):
    """The create_training_job request; no AWS call. Every setting is sent JSON-encoded,
    as AWS's SageMaker SDK sends them and the toolkits decode them. No Tags: the trainer
    role can't add tags, so a request with any is denied."""
    if any(key.startswith("sagemaker_") for key in hyperparameters):
        raise ValueError("sagemaker_* settings are the launcher's")
    settings = dict(hyperparameters, sagemaker_program = entry_point,
                    sagemaker_submit_directory = SUBMIT_DIRECTORY)
    
    def s3_channel(channel, data_type, key):
        return {"ChannelName": channel, 
                "DataSource": {"S3DataSource": {
                    "S3DataType": data_type, "S3Uri": f"s3://{config['ml_bucket']}/{key}",
                    "S3DataDistributionType": "FullyReplicated"}}}
    
    return {
        "TrainingJobName": name,
        "RoleArn": config["role_arn"],
        "AlgorithmSpecification": {"TrainingImage": image_uri, "TrainingInputMode": "File"},
        "HyperParameters": {key: json.dumps(value) for key, value in settings.items()},
        "InputDataConfig": [s3_channel("code", "S3Prefix", code_key(name))]
                           + [s3_channel(c, "ManifestFile", manifest_key(name, c)) for c in channels],
        "OutputDataConfig": {"S3OutputPath": f"s3://{config['ml_bucket']}/training/"},
        "ResourceConfig": {"InstanceType": INSTANCE_TYPE, "InstanceCount": 1, "VolumeSizeInGB": 5},
        "StoppingCondition": {"MaxRuntimeInSeconds": MAX_RUNTIME_S},
        "EnableNetworkIsolation": True,
        # Not used; off, the job writes only what our code puts in /opt/ml/model.
        "ProfilerConfig": {"DisableProfiler": True},
    }

def tuning_request(name, config):
    """The create_hyper_parameter_tuning_job request; no AWS call. Its training jobs are
    train's, from job_request, so they can't drift from it: C is left to the tuner, and
    the tuning API has no TrainingJobName or ProfilerConfig."""
    job = job_request(name, config["training_image"], "train.py", {}, config, channels = tuple(TRAIN_CHANNELS))
    definition = {key: value for key, value in job.items()
                  if key not in ("TrainingJobName", "HyperParameters", "ProfilerConfig")}
    definition["StaticHyperParameters"] = job["HyperParameters"]
    definition["AlgorithmSpecification"] = dict(job["AlgorithmSpecification"], MetricDefinitions = METRICS)
    return {
        "HyperParameterTuningJobName": name,
        "HyperParameterTuningJobConfig": {
            "Strategy": "Bayesian",
            "HyperParameterTuningJobObjective": {"Type": "Maximize", "MetricName": OBJECTIVE},
            "ResourceLimits": {"MaxNumberOfTrainingJobs": TUNE_JOBS, "MaxParallelTrainingJobs": TUNE_PARALLEL},
            "ParameterRanges": TUNE_RANGES,
            "TrainingJobEarlyStoppingType": "Off",
            "RandomSeed": TUNE_SEED,
        },
        "TrainingJobDefinition": definition,
    }

def upload(s3, bucket, uploads):
    """Put {key: bytes} in the ML bucket."""
    for key, body in uploads.items():
        s3.put_object(Bucket = bucket, Key = key, Body = body)

def launch(s3, sm, bucket, request, uploads):
    """Upload {key: bytes} to the ML bucket, then create the job, which reads them as it starts."""
    upload(s3, bucket, uploads)
    sm.create_training_job(**request)

def wait(sm, names, out = sys.stdout, poll_s = POLL_S, sleep = time.sleep):
    """{name: describe_training_job} once every job has finished, printing each status change.
    A failed describe raises LostContact, never a failed job: expired credentials or a
    dropped network stop us watching, not the job."""
    last, done = {}, {}
    while True:
        for name in names:
            if name in done:
                continue
            try:
                d = sm.describe_training_job(TrainingJobName = name)
            except (BotoCoreError, ClientError) as err:
                raise LostContact(f"{name}: {type(err).__name__}: {err}") from err
            status = (d["TrainingJobStatus"], d.get("SecondaryStatus"))
            if status != last.get(name):
                print(f"{datetime.now(timezone.utc):%H:%M:%S} {name}: {status[0]} / {status[1]}", file = out)
                last[name] = status
            if status[0] in FINISHED:
                done[name] = d
        if len(done) == len(names):
            return done
        sleep(poll_s)

def wait_tuning(sm, name, out = sys.stdout, poll_s = POLL_S, sleep = time.sleep):
    """describe_hyper_parameter_tuning_job once the tuning job has finished, printing each
    change in its status or in its training jobs' counts. A failed describe raises LostContact."""
    last = None
    while True:
        try:
            d = sm.describe_hyper_parameter_tuning_job(HyperParameterTuningJobName = name)
        except (BotoCoreError, ClientError) as err:
            raise LostContact(f"{name}: {type(err).__name__}: {err}") from err
        counts = tuple((key, n) for key, n in sorted((d.get("TrainingJobStatusCounters") or {}).items()) if n)
        status = (d["HyperParameterTuningJobStatus"], counts)
        if status != last:
            jobs = ", ".join(f"{key} {n}" for key, n in counts) or "none yet"
            print(f"{datetime.now(timezone.utc):%H:%M:%S} {name}: {status[0]}; training jobs: {jobs}", file = out)
            last = status
        if status[0] in FINISHED:
            return d
        sleep(poll_s)

def tuning_jobs(sm, name):
    """The summaries of every training job the tuning job launched, by C."""
    jobs = []
    for page in sm.get_paginator("list_training_jobs_for_hyper_parameter_tuning_job").paginate(
            HyperParameterTuningJobName = name):
        jobs += page["TrainingJobSummaries"]
    return sorted(jobs, key = lambda job: float(job["TunedHyperParameters"]["C"]))

def read_report(s3, description, member = "smoke.json"):
    """One file from the job's model.tar.gz (SageMaker's archive of /opt/ml/model)."""
    bucket, key = description["ModelArtifacts"]["S3ModelArtifacts"][len("s3://"):].split("/", 1)
    body = s3.get_object(Bucket = bucket, Key = key)["Body"].read()
    with tarfile.open(fileobj = io.BytesIO(body), mode = "r:gz") as tar:
        for info in tar.getmembers():
            if os.path.normpath(info.name) == member:
                return json.load(tar.extractfile(info))
        raise RuntimeError(f"no {member} in s3://{bucket}/{key}: it holds {tar.getnames()}")
    
def tail_log(logs, name, lines = 20):
    """The job's last log lines (its streams are named <job>/algo-1-<time>)."""
    try:
        events = []
        for page in logs.get_paginator("filter_log_events").paginate(
            logGroupName = LOG_GROUP, logStreamNamePrefix = f"{name}/"):
            events += page["events"]
    except ClientError as err:
        return [f"(no log: {err})"]
    return [e["message"].rstrip() for e in sorted(events, key = lambda e: e["timestamp"])[-lines:]]

def judge(report, expected):
    """[(check, passed, detail)]: what reached the job, and that it was cut off.
    expected: {"channels": {channel: {relative_path: bytes}}, "code": [...],
    "hyperparameters": {name: text as sent}}. code passes when every file we sent was
    unpacked: the XGBoost image's toolkit adds its own (setup.py and the like)."""
    code = report.get("code") or []
    got_channels = {channel: {f["path"]: f["bytes"] for f in files}
                    for channel, files in (report.get("channels") or {}).items()}
    network = report.get("network") or ""
    results = [
        ("errors", report.get("errors") == {}, report.get("errors")),
        ("network", network.startswith("blocked"), network),
        ("channels", got_channels == expected["channels"], got_channels),
        ("code", set(expected["code"]) <= set(code), code),
        ("hyperparameters", report.get("hyperparameters") == expected["hyperparameters"],
         report.get("hyperparameters")),
    ]
    return [(check, passed, detail if passed else f"got {detail}, expected {expected.get(check, '')}")
            for check, passed, detail in results]

def findings(report):
    """What the image has: reported, not judged."""
    libraries = report.get("libraries") or {}
    env = report.get("env") or {}
    argv = report.get("argv")
    covers = bool(libraries.get("scikit-learn") and libraries.get("xgboost"))
    return [
        f"python     {str(report.get('python')).split()[0]}",
        "libraries  " + ", ".join(f"{name} {version or '-'}" for name, version in sorted(libraries.items())),
        f"covers all of M5 (scikit-learn and xgboost): {'yes' if covers else 'no'}",
        f"argv       {argv} " + ("(as predicted)" if argv == SMOKE_ARGV_PREDICTED
                                 else f"(predicted {SMOKE_ARGV_PREDICTED})"),
        "env        " + ", ".join(f"{name}={env.get(name, '-')}" for name in SMOKE_ENV),
    ]

def read_pins(path = PINS):
    """{name: version} from a requirements file's name==version lines."""
    pins = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        if not sep or not name.strip() or not version.strip():
            raise ValueError(f"{path}: not name==version: {line!r}")
        pins[name.strip()] = version.strip()
    return pins

def check_model(model, ids, pins):
    """[(check, passed, detail)] for a training job's model.json: our package format, one
    run read per run sent, the pinned library versions, and a solver that converged."""
    runs = {channel: len(experiment_ids) for channel, experiment_ids in ids.items()}
    libraries = {name: (model.get("libraries") or {}).get(name) for name in pins}
    expected = {"format": MODEL_FORMAT, "runs": runs, "libraries": pins, "converged": True}
    got = {"format": model.get("format"), "runs": model.get("runs"), "libraries": libraries,
           "converged": model.get("converged")}
    return [(check, got[check] == expected[check],
             got[check] if got[check] == expected[check] else f"got {got[check]}, expected {expected[check]}")
            for check in expected]

def summary(model):
    """model.json in a few lines: what the job printed, and what it was fitted with."""
    lines = [f"{channel:<10} {model['runs'][channel]} runs, {model['rows'][channel]} rows, "
             + ", ".join(f"{metric} {value:.4f}" for metric, value in model["metrics"][channel].items())
             for channel in TRAIN_CHANNELS]
    return lines + [f"settings   {model['settings']}",
                    f"columns    {len(model['columns'])}",
                    f"classes    {', '.join(model['classes'])}"]

def smoke(args, out=sys.stdout):
    import boto3
    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    config = lookup(session)
    s3, sm = session.client("s3"), session.client("sagemaker")

    record = train_val_records(s3, config["telemetry_bucket"]).get(SMOKE_SLOT)
    if record is None:
        raise RuntimeError(f"slot {SMOKE_SLOT} is not an accepted train or val run of {DATASET}")
    files = channel_files([record["experiment_id"]])
    sizes = check_present(s3, config["telemetry_bucket"], files)
    code = bundle([JOB_DIR / "smoke.py"])
    print(f"slot {SMOKE_SLOT}: {record['experiment_id']}; bundle {len(code)} bytes", file=out)

    now = datetime.now(timezone.utc)
    jobs = {}
    for image in args.images:
        name = job_name("smoke", image, now)
        request = job_request(name, IMAGES[image], "smoke.py", SMOKE_HYPERPARAMETERS, config)
        uploads = {code_key(name): code}
        for channel, keys in files.items():
            body = manifest(config["telemetry_bucket"], channel_prefix(channel), keys)
            uploads[manifest_key(name, channel)] = body.encode("utf-8")
        if args.dry_run:
            print(json.dumps(request, indent=2, sort_keys=True), file=out)
            for key, body in uploads.items():
                if key.endswith(".manifest"):
                    print(f"{key}: {body.decode('utf-8')}", file=out)
            continue
        launch(s3, sm, config["ml_bucket"], request, uploads)
        print(f"created {name}", file=out)
        jobs[name] = request
    if args.dry_run:
        return 0

    try:
        done = wait(sm, list(jobs), out=out)
    except LostContact as err:
        print(f"LOST CONTACT {err}\nThe jobs keep running. Check each with: aws sagemaker "
              f"describe-training-job --training-job-name <name> --profile {args.profile}", file=out)
        return 2

    logs = session.client("logs")
    passed_all = True
    for name, d in done.items():
        print(f"\n{name}: {d['TrainingJobStatus']}, billed {d.get('BillableTimeInSeconds', '?')} s", file=out)
        if d["TrainingJobStatus"] != "Completed":
            passed_all = False
            print(f"  {d.get('FailureReason', '(no reason given)')}", file=out)
            for line in tail_log(logs, name):
                print(f"  | {line}", file=out)
            continue
        report = read_report(s3, d)
        expected = {"channels": dict(sizes, code={"sourcedir.tar.gz": len(code)}),
                    "code": ["smoke.py"],
                    "hyperparameters": jobs[name]["HyperParameters"]}
        for check, passed, detail in judge(report, expected):
            passed_all &= passed
            print(f"  {'PASS' if passed else 'FAIL'} {check:<16} {detail}", file=out)
        for line in findings(report):
            print(f"  {line}", file=out)
        print(f"  report: {d['ModelArtifacts']['S3ModelArtifacts']} (smoke.json)", file=out)
    return 0 if passed_all else 1

def prepare(s3, config, name, out):
    """(ids, uploads) for a training or tuning job named `name`: the train/validation split,
    every file of it checked present, and the code and manifests to upload. Prints what the
    channels hold."""
    ids = split_ids(train_val_records(s3, config["telemetry_bucket"]))
    empty = [channel for channel, experiment_ids in ids.items() if not experiment_ids]
    if empty:
        raise RuntimeError(f"no accepted {DATASET} runs for the {' or '.join(empty)} channel")
    sizes = check_present(s3, config["telemetry_bucket"], channel_files(ids["train"] + ids["validation"]))
    code = bundle(TRAIN_CODE)
    uploads = {code_key(name): code}
    for channel, experiment_ids in ids.items():
        keys = dataset_keys(channel_files(experiment_ids))
        body = manifest(config["telemetry_bucket"], ds.root(DATASET), keys)
        uploads[manifest_key(name, channel)] = body.encode("utf-8")
        print(f"{channel}: {len(experiment_ids)} runs, {len(keys)} files", file = out)
    total = sum(sum(tree.values()) for tree in sizes.values())
    print(f"{total / 1e6:.1f} MB of data; bundle {len(code)} bytes", file = out)
    return ids, uploads

def print_dry_run(request, uploads, out):
    print(json.dumps(request, indent = 2, sort_keys = True), file = out)
    for key, body in uploads.items():
        if key.endswith(".manifest"):
            entries = json.loads(body)
            print(f"{key}: {entries[0]}, then {len(entries) - 1} keys from {entries[1]}", file = out)

def report_model(s3, d, ids, pins, out):
    """(model.json, [(check, passed, detail)]) for a completed training job, printing both
    and where its model is."""
    model = read_report(s3, d, member = "model.json")
    for line in summary(model):
        print(f"  {line}", file = out)
    results = check_model(model, ids, pins)
    for check, passed, detail in results:
        print(f"  {'PASS' if passed else 'FAIL'} {check:<16} {detail}", file = out)
    artifact = d["ModelArtifacts"]["S3ModelArtifacts"]
    bucket, key = artifact[len("s3://"):].split("/", 1)
    # The bucket keeps every version; this one is the model just trained.
    version = s3.head_object(Bucket = bucket, Key = key).get("VersionId")
    print(f"  model: {artifact} (version {version})", file = out)
    return model, results

def train(args, out = sys.stdout):
    import boto3
    session = boto3.Session(profile_name = args.profile, region_name = REGION)
    config = lookup(session)
    s3, sm = session.client("s3"), session.client("sagemaker")

    name = job_name("train", "logreg", datetime.now(timezone.utc))
    ids, uploads = prepare(s3, config, name, out)
    pins = read_pins()
    # Only what was given: train.py's defaults hold for the rest, and model.json records both.
    hyperparameters = {setting: value for setting, value in (("C", args.C), ("max_iter", args.max_iter))
                       if value is not None}
    request = job_request(name, config["training_image"], "train.py", hyperparameters, config,
                          channels = tuple(TRAIN_CHANNELS))
    if args.dry_run:
        print_dry_run(request, uploads, out)
        return 0

    launch(s3, sm, config["ml_bucket"], request, uploads)
    print(f"created {name}", file = out)
    try:
        d = wait(sm, [name], out = out)[name]
    except LostContact as err:
        print(f"LOST CONTACT {err}\nThe job keeps running. Check it with: aws sagemaker "
              f"describe-training-job --training-job-name {name} --profile {args.profile}", file = out)
        return 2

    print(f"\n{name}: {d['TrainingJobStatus']}, billed {d.get('BillableTimeInSeconds', '?')} s", file = out)
    if d["TrainingJobStatus"] != "Completed":
        print(f"  {d.get('FailureReason', '(no reason given)')}", file = out)
        for line in tail_log(session.client("logs"), name):
            print(f"  | {line}", file = out)
        return 1
    _, results = report_model(s3, d, ids, pins, out)
    return 0 if all(passed for _, passed, _ in results) else 1

def tune(args, out = sys.stdout):
    import boto3
    session = boto3.Session(profile_name = args.profile, region_name = REGION)
    config = lookup(session)
    s3, sm = session.client("s3"), session.client("sagemaker")

    name = tuning_job_name(datetime.now(timezone.utc))
    ids, uploads = prepare(s3, config, name, out)
    pins = read_pins()
    request = tuning_request(name, config)
    if args.dry_run:
        print_dry_run(request, uploads, out)
        return 0

    upload(s3, config["ml_bucket"], uploads)
    sm.create_hyper_parameter_tuning_job(**request)
    print(f"created {name}", file = out)
    try:
        d = wait_tuning(sm, name, out = out)
        jobs = tuning_jobs(sm, name)
    except LostContact as err:
        print(f"LOST CONTACT {err}\nThe tuning job keeps running. Check it with: aws sagemaker "
              f"describe-hyper-parameter-tuning-job --hyper-parameter-tuning-job-name {name} "
              f"--profile {args.profile}", file = out)
        return 2

    print(f"\n{name}: {d['HyperParameterTuningJobStatus']}", file = out)
    for job in jobs:
        score = (job.get("FinalHyperParameterTuningJobObjectiveMetric") or {}).get("Value")
        print(f"  C {float(job['TunedHyperParameters']['C']):<10.4g} {job['TrainingJobStatus']:<10} "
              f"{OBJECTIVE} {'-' if score is None else f'{score:.4f}'}  {job['TrainingJobName']}", file = out)
    failed = [job for job in jobs if job["TrainingJobStatus"] != "Completed"]
    best = d.get("BestTrainingJob")
    if d["HyperParameterTuningJobStatus"] != "Completed" or failed or not best:
        if d.get("FailureReason"):
            print(f"  {d['FailureReason']}", file = out)
        # Every training job runs the same code, so the first failure's log usually explains the rest.
        if failed:
            print(f"  {failed[0]['TrainingJobName']}: {failed[0].get('FailureReason', '(no reason given)')}", file = out)
            for line in tail_log(session.client("logs"), failed[0]["TrainingJobName"]):
                print(f"  | {line}", file = out)
        return 1

    print(f"\nbest: {best['TrainingJobName']}, C {float(best['TunedHyperParameters']['C']):.4g}", file = out)
    model, results = report_model(s3, sm.describe_training_job(TrainingJobName = best["TrainingJobName"]),
                                  ids, pins, out)
    # The score the tuner read from the log must be the validation score model.json records.
    read = best["FinalHyperParameterTuningJobObjectiveMetric"]["Value"]
    recorded = model["metrics"]["validation"]["macro_f1"]
    same = abs(read - recorded) <= SCORE_TOLERANCE
    print(f"  {'PASS' if same else 'FAIL'} {'objective':<16} "
          + (f"{read}" if same else f"the tuner read {read}, model.json has {recorded}"), file = out)
    return 0 if same and all(passed for _, passed, _ in results) else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m ml.launch", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("smoke", help="run ml/job/smoke.py once in each image and judge its report")
    s.add_argument("--images", nargs="+", choices=sorted(IMAGES), default=sorted(IMAGES))
    s.add_argument("--dry-run", action="store_true", help="print the requests; upload and create nothing")
    s.add_argument("--profile", default="ml")
    t = sub.add_parser("train", help = "train ml/job/train.py's model once on v1 and check its package")
    t.add_argument("-C", type = float, help = "inverse regularization strength (train.py's default if not given)")
    t.add_argument("--max-iter", type = int, help = "the solver's iteration limit (train.py's default if not given)")
    t.add_argument("--dry-run", action = "store_true", help = "print the request; upload and create nothing")
    t.add_argument("--profile", default = "ml")
    h = sub.add_parser("tune", help = "tune train's C with a SageMaker tuning job and check the best model")
    h.add_argument("--dry-run", action = "store_true", help = "print the request; upload and create nothing")
    h.add_argument("--profile", default = "ml")
    args = p.parse_args(argv)
    try:
        return {"smoke": smoke, "train": train, "tune": tune}[args.command](args)
    except BotoCoreError as err:
        print(f"AWS: {err} (an expired SSO session? aws sso login)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

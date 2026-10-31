"""Launch SageMaker training jobs (M5-1).
    
    cd AnomalyPulse
    python -m ml.launch smoke --dry-run     # print the requests; upload and create nothing
    python -m ml.launch smoke               # one smoke job per image, as --profile ml
    python -m ml.launch train --dry-run     # print the training request; create nothing
    python -m ml.launch train -C 0.5        # train once; train.py's default for what isn't given
    python -m ml.launch train --script ml/job/train_xgb.py --set max_depth=4   # another job script
    python -m ml.launch tune --dry-run      # print the tuning request; create nothing
    python -m ml.launch tune                # tune C: TUNE_JOBS training jobs, TUNE_PARALLEL at a time
    python -m ml.launch tune --script ml/job/train_xgb.py --range learning_rate=0.01:0.3:log --jobs 8
    python -m ml.launch promote <job> --dry-run   # check the job's model; write nothing
    python -m ml.launch promote <job>       # point the Diagnosis Lambda at it

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

`--script ml/job/<name>.py` runs another job script the same way (train.py is the
default). It is bundled with train.py and join_labels.py, so it can `import train` for the
helpers that read the channels, and its jobs are named anomalypulse-train-<name>-<time>
(without a leading train_). `--set NAME=VALUE` sends any setting; -C and --max-iter are
train.py's, as --set C=... and --set max_iter=....

`tune` runs train's job under a SageMaker tuning job: the same image, code, channels and
role, with the settings each --range names chosen by the tuner (C from TUNE_RANGES for
train.py when none is given), those --set names fixed, and each job scored by the
validation:macro_f1 line the script prints. It lists every job it launched, then checks the
best one's model as `train` does, and that the score the tuner read is model.json's. Exit
codes as for smoke; 1 also when any of its training jobs failed.

`promote` points the Diagnosis Lambda at one completed training job's model: it checks the
model as `train` does, then writes the job, the package's S3 address, its version and its
validation metrics to the SSM parameter /anomalypulse/ml/models/<the model's task>, which the
Lambda reads when it starts. It prints the pointer it replaces; promoting that job again
undoes it. Exits 0 when promoted (or, with --dry-run, when it would be), 1 when not.
"""
import argparse
import io
import json
import math
import os
import re
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
#CHANGED BY MELISA
TRAIN_CODE = (JOB_DIR / "train.py", JOB_DIR.parent.parent / "features" / "join_labels.py",
              JOB_DIR / "detection.py")

# --script: a file in ml/job/ that the toolkit can run as a module (python -m <name>), and
# whose name doesn't hide a library it imports: the code directory comes first on sys.path.
SCRIPT_NAME = re.compile(r"[a-z][a-z0-9_]*")
# CHANGED BY MELISA
SHADOWED = {"xgboost", "sklearn", "numpy", "scipy", "joblib", "pandas", "join_labels", "detection"}
# {channel: its temporal_split partition}. Both channels copy in both trees.
TRAIN_CHANNELS = {"train": "train", "validation": "val"}
MODEL_FORMAT = 1
# The Diagnosis Lambda installs these versions, so every library a model lists must be one of them.
PINS = JOB_DIR.parent / "requirements-image.txt"
# Every model's pickle uses these, so every package must list them.
REQUIRED_LIBRARIES = ("scikit-learn", "numpy", "scipy", "joblib")
# A classifier answers with class probabilities; a detector with a score and its own threshold.
MODEL_KINDS = ("classifier", "detector")

# One pointer per task, /anomalypulse/ml/models/<task>, for the Diagnosis Lambda to load.
MODEL_POINTERS = "/anomalypulse/ml/models/"
TASK_NAME = re.compile(r"[a-z][a-z0-9_]*")

# What the tuner may set for train.py when no --range is given. C on a log scale: each
# tenfold step changes the model about as much.
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

def job_script(path):
    """(script, variant, code) for --script: the script's path in ml/job/, the job-name variant
    (train.py's is "logreg"; ml/job/train_xgb.py's is "xgb"), and the files to bundle. Any
    script but train.py is bundled with train.py and join_labels.py, so it can import train's
    helpers. Raises ValueError on a script the toolkit couldn't run or import."""
    script = Path(path)
    if not script.is_file():
        script = JOB_DIR / path
    script = script.resolve()
    if script.parent != JOB_DIR or script.suffix != ".py" or not script.is_file():
        raise ValueError(f"--script must be a .py file in {JOB_DIR}: {path}")
    if script.name == "smoke.py":
        raise ValueError("smoke.py has its own command: python -m ml.launch smoke")
    if not SCRIPT_NAME.fullmatch(script.stem):
        raise ValueError(f"{script.name}: the toolkit runs it as python -m {script.stem}, "
                         f"so its name must be lowercase letters, digits and _")
    if script.stem in SHADOWED:
        raise ValueError(f"{script.name} would hide the {script.stem} library from the job's imports")
    if script == JOB_DIR / "train.py":
        return script, "logreg", TRAIN_CODE
    return script, script.stem.removeprefix("train_").replace("_", "-"), (script,) + TRAIN_CODE

def setting(text):
    """(name, value) from --set NAME=VALUE: the value as JSON when it parses as JSON (200,
    0.1, true), else as text, so --set booster=gbtree sends the text "gbtree"."""
    name, sep, value = text.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"not NAME=VALUE: {text!r}")
    try:
        return name, json.loads(value)
    except json.JSONDecodeError:
        return name, value

def parameter_range(text):
    """(kind, range) from --range NAME=LOW:HIGH or NAME=LOW:HIGH:log. Two integers make an
    IntegerParameterRanges entry, anything else a ContinuousParameterRanges one; :log searches
    on a log scale, which needs LOW > 0."""
    name, sep, spec = text.partition("=")
    parts = spec.split(":")
    if not sep or not name or len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "log"):
        raise argparse.ArgumentTypeError(f"not NAME=LOW:HIGH or NAME=LOW:HIGH:log: {text!r}")
    integer = all(re.fullmatch(r"-?\d+", part) for part in parts[:2])
    try:
        low, high = (int(part) if integer else float(part) for part in parts[:2])
    except ValueError:
        raise argparse.ArgumentTypeError(f"LOW and HIGH must be numbers: {text!r}") from None
    if not low < high:
        raise argparse.ArgumentTypeError(f"LOW must be below HIGH: {text!r}")
    log = len(parts) == 3
    if log and low <= 0:
        raise argparse.ArgumentTypeError(f"a log range needs LOW > 0: {text!r}")
    return ("IntegerParameterRanges" if integer else "ContinuousParameterRanges",
            {"Name": name, "MinValue": str(low), "MaxValue": str(high),
             "ScalingType": "Logarithmic" if log else "Linear"})

def tuned(settings):
    """[(name, value)]: a training job's tuned settings by name, numbers to 4 significant
    digits (C 25.818801896635723 is "25.82")."""
    def number(value):
        try:
            return f"{float(value):.4g}"
        except ValueError:
            return value
    return [(name, number(value)) for name, value in sorted(settings.items())]

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

def tuning_request(name, config, entry_point = "train.py", static = None, ranges = None,
                   jobs = TUNE_JOBS, parallel = TUNE_PARALLEL):
    """The create_hyper_parameter_tuning_job request; no AWS call. Its training jobs are
    train's, from job_request, so they can't drift from it: `ranges` (TUNE_RANGES if None)
    are left to the tuner, `static` settings go to every job, and the tuning API has no
    TrainingJobName or ProfilerConfig."""
    ranges = TUNE_RANGES if ranges is None else ranges
    tuned_names = {entry["Name"] for kind in ranges.values() for entry in kind}
    overlap = sorted(tuned_names & set(static or {}))
    if overlap:
        raise ValueError(f"tuned and fixed at once: {overlap}")
    job = job_request(name, config["training_image"], entry_point, static or {}, config,
                      channels = tuple(TRAIN_CHANNELS))
    definition = {key: value for key, value in job.items()
                  if key not in ("TrainingJobName", "HyperParameters", "ProfilerConfig")}
    definition["StaticHyperParameters"] = job["HyperParameters"]
    definition["AlgorithmSpecification"] = dict(job["AlgorithmSpecification"], MetricDefinitions = METRICS)
    return {
        "HyperParameterTuningJobName": name,
        "HyperParameterTuningJobConfig": {
            "Strategy": "Bayesian",
            "HyperParameterTuningJobObjective": {"Type": "Maximize", "MetricName": OBJECTIVE},
            "ResourceLimits": {"MaxNumberOfTrainingJobs": jobs, "MaxParallelTrainingJobs": parallel},
            "ParameterRanges": ranges,
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
    """The summaries of every training job the tuning job launched, by their tuned settings
    (by name, then value)."""
    jobs = []
    for page in sm.get_paginator("list_training_jobs_for_hyper_parameter_tuning_job").paginate(
            HyperParameterTuningJobName = name):
        jobs += page["TrainingJobSummaries"]
    def order(job):
        values = []
        for _, value in sorted(job["TunedHyperParameters"].items()):
            try:
                values.append((0, float(value), ""))
            except ValueError:
                values.append((1, 0.0, value))
        return values
    return sorted(jobs, key = order)

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
    """[(check, passed, detail)] for a training job's model.json: our package format, a kind the
    Lambda can load, a task a pointer can be named after, one run read per run sent, the pinned
    version of every library it lists (the four every model uses among them), and no solver that
    failed to converge (a model without one records no `converged`)."""
    def result(check, got, expected):
        return (check, got == expected, got if got == expected else f"got {got}, expected {expected}")
    runs = {channel: len(experiment_ids) for channel, experiment_ids in ids.items()}
    recorded = {name: version for name, version in (model.get("libraries") or {}).items() if name != "python"}
    names = sorted(set(REQUIRED_LIBRARIES) | set(recorded))
    kind, threshold, task = model.get("kind", "classifier"), model.get("threshold"), model.get("task")
    kind_ok = kind == "classifier" or (kind == "detector" and type(threshold) in (int, float)
                                       and math.isfinite(threshold))
    return [result("format", model.get("format"), MODEL_FORMAT),
            ("kind", kind_ok, kind if kind_ok else
             f"got kind {kind!r}, threshold {threshold!r}; expected one of {MODEL_KINDS}, a detector with a finite threshold"),
            ("task", isinstance(task, str) and bool(TASK_NAME.fullmatch(task)),
             task if isinstance(task, str) and TASK_NAME.fullmatch(task) else f"got {task!r}, expected {TASK_NAME.pattern}"),
            result("runs", model.get("runs"), runs),
            result("libraries", {name: recorded.get(name) for name in names}, {name: pins.get(name) for name in names}),
            ("converged", model.get("converged") is not False, model.get("converged", "not recorded"))]

def summary(model):
    """model.json in a few lines: what the job printed, and what it was fitted with."""
    lines = [f"{channel:<10} {model['runs'][channel]} runs, {model['rows'][channel]} rows, "
             + ", ".join(f"{metric} {value:.4f}" for metric, value in model["metrics"][channel].items())
             for channel in TRAIN_CHANNELS]
    lines += [f"settings   {model['settings']}", f"columns    {len(model['columns'])}"]
    if model.get("kind", "classifier") == "detector":
        return lines + [f"threshold  {model.get('threshold')}"]
    return lines + [f"classes    {', '.join(model['classes'])}"]

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

def prepare(s3, config, name, out, code = TRAIN_CODE):
    """(ids, uploads) for a training or tuning job named `name`: the train/validation split,
    every file of it checked present, and the code (`code` bundled) and manifests to upload.
    Prints what the channels hold."""
    ids = split_ids(train_val_records(s3, config["telemetry_bucket"]))
    empty = [channel for channel, experiment_ids in ids.items() if not experiment_ids]
    if empty:
        raise RuntimeError(f"no accepted {DATASET} runs for the {' or '.join(empty)} channel")
    sizes = check_present(s3, config["telemetry_bucket"], channel_files(ids["train"] + ids["validation"]))
    code = bundle(code)
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

def promote(args, out = sys.stdout):
    import boto3
    session = boto3.Session(profile_name = args.profile, region_name = REGION)
    config = lookup(session)
    s3, sm, ssm = session.client("s3"), session.client("sagemaker"), session.client("ssm")

    d = sm.describe_training_job(TrainingJobName = args.job)
    if d["TrainingJobStatus"] != "Completed":
        print(f"{args.job} is {d['TrainingJobStatus']}: only a completed job's model can be promoted", file = out)
        return 1
    artifact = d["ModelArtifacts"]["S3ModelArtifacts"]
    bucket, key = artifact[len("s3://"):].split("/", 1)
    # SageMaker writes each job's package once, so its latest version is the one checked below.
    version = s3.head_object(Bucket = bucket, Key = key).get("VersionId")
    if version is None:
        print(f"{artifact} has no version: the Lambda could not pin it", file = out)
        return 1

    model = read_report(s3, d, member = "model.json")
    for line in summary(model):
        print(f"  {line}", file = out)
    results = check_model(model, split_ids(train_val_records(s3, config["telemetry_bucket"])), read_pins())
    for check, passed, detail in results:
        print(f"  {'PASS' if passed else 'FAIL'} {check:<16} {detail}", file = out)
    if not all(passed for _, passed, _ in results):
        return 1

    # One parameter, so the job and its version always change together.
    name = MODEL_POINTERS + model["task"]
    pointer = json.dumps({"job": args.job, "uri": artifact, "version_id": version,
                          "validation": model["metrics"]["validation"]})
    try:
        current = ssm.get_parameter(Name = name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        current = "none"
    # To undo a promotion, promote the job this names again.
    print(f"pointer  {name}", file = out)
    print(f"current  {current}", file = out)
    print(f"new      {pointer}", file = out)
    if args.dry_run:
        return 0
    written = ssm.put_parameter(Name = name, Type = "String", Overwrite = True, Value = pointer)
    print(f"promoted: {name} is now version {written['Version']}", file = out)
    return 0

def train(args, out = sys.stdout):
    import boto3
    session = boto3.Session(profile_name = args.profile, region_name = REGION)
    config = lookup(session)
    s3, sm = session.client("s3"), session.client("sagemaker")

    script, variant, code = args.script
    name = job_name("train", variant, datetime.now(timezone.utc))
    ids, uploads = prepare(s3, config, name, out, code)
    pins = read_pins()
    # Only what was given: the script's defaults hold for the rest, and model.json records both.
    request = job_request(name, config["training_image"], script.name, args.settings, config,
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

    script, _, code = args.script
    name = tuning_job_name(datetime.now(timezone.utc))
    ids, uploads = prepare(s3, config, name, out, code)
    pins = read_pins()
    request = tuning_request(name, config, script.name, args.settings, args.ranges, args.jobs, args.parallel)
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
        picked = " ".join(f"{option} {value:<10}" for option, value in tuned(job["TunedHyperParameters"]))
        print(f"  {picked} {job['TrainingJobStatus']:<10} "
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

    print(f"\nbest: {best['TrainingJobName']}, "
          + ", ".join(f"{option} {value}" for option, value in tuned(best["TunedHyperParameters"])), file = out)
    model, results = report_model(s3, sm.describe_training_job(TrainingJobName = best["TrainingJobName"]),
                                  ids, pins, out)
    # The score the tuner read from the log must be the validation score model.json records.
    read = best["FinalHyperParameterTuningJobObjectiveMetric"]["Value"]
    recorded = model["metrics"]["validation"]["macro_f1"]
    same = abs(read - recorded) <= SCORE_TOLERANCE
    print(f"  {'PASS' if same else 'FAIL'} {'objective':<16} "
          + (f"{read}" if same else f"the tuner read {read}, model.json has {recorded}"), file = out)
    return 0 if same and all(passed for _, passed, _ in results) else 1


def script_argument(path):
    """job_script for argparse, which reports an ArgumentTypeError's message as it is."""
    try:
        return job_script(path)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from None

def job_arguments(args):
    """Sets args.settings ({name: value} from --set, and train's -C and --max-iter) and, for
    tune, args.ranges (None: TUNE_RANGES). Returns what's wrong with them, or None."""
    given = list(args.set)
    for name, value in (("C", getattr(args, "C", None)), ("max_iter", getattr(args, "max_iter", None))):
        if value is not None:
            given.append((name, value))
    names = [name for name, _ in given]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        return f"set more than once: {', '.join(twice)}"
    args.settings = dict(given)
    if args.command != "tune":
        return None
    tuned_names = [entry["Name"] for _, entry in args.range]
    twice = sorted({name for name in tuned_names if tuned_names.count(name) > 1})
    if twice:
        return f"--range given more than once for: {', '.join(twice)}"
    both = sorted(set(tuned_names) & set(args.settings))
    if both:
        return f"both tuned and fixed: {', '.join(both)}"
    if not args.range and args.script[0].name != "train.py":
        return f"say what to tune in {args.script[0].name} with --range"
    if not 1 <= args.parallel <= args.jobs:
        return "--parallel must be from 1 to --jobs"
    args.ranges = None
    if args.range:
        args.ranges = {}
        for kind, entry in args.range:
            args.ranges.setdefault(kind, []).append(entry)
    return None

def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m ml.launch", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("smoke", help="run ml/job/smoke.py once in each image and judge its report")
    s.add_argument("--images", nargs="+", choices=sorted(IMAGES), default=sorted(IMAGES))
    s.add_argument("--dry-run", action="store_true", help="print the requests; upload and create nothing")
    s.add_argument("--profile", default="ml")
    t = sub.add_parser("train", help = "train a job script's model once on v1 and check its package")
    t.add_argument("--script", type = script_argument, default = "train.py",
                   help = "the job script in ml/job/ (default train.py, the logistic regression)")
    t.add_argument("--set", type = setting, action = "append", default = [], metavar = "NAME=VALUE",
                   help = "a setting to send the script, repeatable (the script's default for the rest)")
    t.add_argument("-C", type = float, help = "train.py's inverse regularization strength: --set C=...")
    t.add_argument("--max-iter", type = int, help = "train.py's solver iteration limit: --set max_iter=...")
    t.add_argument("--dry-run", action = "store_true", help = "print the request; upload and create nothing")
    t.add_argument("--profile", default = "ml")
    h = sub.add_parser("tune", help = "tune a job script's settings with a SageMaker tuning job and check the best model")
    h.add_argument("--script", type = script_argument, default = "train.py",
                   help = "the job script in ml/job/ (default train.py, whose C is tuned unless --range is given)")
    h.add_argument("--range", type = parameter_range, action = "append", default = [], metavar = "NAME=LOW:HIGH[:log]",
                   help = "a setting for the tuner to choose, repeatable; two integers make an integer range")
    h.add_argument("--set", type = setting, action = "append", default = [], metavar = "NAME=VALUE",
                   help = "a setting every job gets, repeatable")
    h.add_argument("--jobs", type = int, default = TUNE_JOBS, help = f"training jobs in all (default {TUNE_JOBS})")
    h.add_argument("--parallel", type = int, default = TUNE_PARALLEL,
                   help = f"training jobs at a time (default {TUNE_PARALLEL})")
    h.add_argument("--dry-run", action = "store_true", help = "print the request; upload and create nothing")
    h.add_argument("--profile", default = "ml")
    m = sub.add_parser("promote", help = "check a training job's model and point the Diagnosis Lambda at it")
    m.add_argument("job", help = "the training job's name (for a tuned model, the best child's)")
    m.add_argument("--dry-run", action = "store_true", help = "run the checks and print the pointer; write nothing")
    m.add_argument("--profile", default = "ml")
    args = p.parse_args(argv)
    if args.command in ("train", "tune"):
        problem = job_arguments(args)
        if problem:
            p.error(problem)
    try:
        return {"smoke": smoke, "train": train, "tune": tune, "promote": promote}[args.command](args)
    except BotoCoreError as err:
        print(f"AWS: {err} (an expired SSO session? aws sso login)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

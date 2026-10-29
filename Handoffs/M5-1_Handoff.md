# M5-1 Handoff — SageMaker Training, Model Packages & the Diagnosis Lambda

> **The one-sentence version:** Models now train and tune in SageMaker, land versioned in S3 as a two-file **model package**, and reach the **Diagnosis Lambda** through one command, `promote` — so your M5 job is to **write a training script that writes that package**, run it through the launcher, and promote the one you pick. Jake has built the launcher, the package format, the promotion pointer and the Lambda that loads it, and has trained and promoted a logistic-regression baseline. Everything is **live** in `us-east-2` (stack `anomalypulse-ml`); the launcher and the job scripts are in `AnomalyPulse/ml/`.

**Where it stands (2026-10-29):** M5-1 is merged (PR #79). The tuned baseline is promoted and answering real calls. The Lambda already installs xgboost and knows how to load an Isolation Forest, and the launcher trains and tunes any job script you add (`--script`, §2). **Not built yet:** the Lambda serves one model per call; running the detector and the classifier in one call is M8-2's.

The reference of record is [`Docs/ML_INFERENCE.md`](../Docs/ML_INFERENCE.md).

---

## 1. Mental model (read this first)

**SageMaker trains; Lambda predicts.** A SageMaker endpoint would bill every idle hour. Instead, a training job writes a small package to S3, and the Lambda loads it when it starts. Between calls nothing runs, so the idle cost is storage only (about $0.06 a month).

**A training job sees only what it's handed.** Jobs run with network isolation: no internet, no `pip install`, no AWS calls. Your code arrives in a `code` channel. The data arrives in two channels, `train` (v1's slots 1–560) and `validation` (561–700), which the launcher builds from `integrity.temporal_split`, so a job can't read a test run. Each channel mirrors the dataset: `tier1/features/experiment_id=<id>/features.jsonl` and `tier2/labels/experiment_id=<id>/labels.jsonl` for every run in it.

**A model package is two files** in the job's `model.tar.gz`:
- `model.joblib`: your fitted model, a scikit-learn estimator or a `Pipeline` ending in one;
- `model.json`: what using it takes (the columns in order, the classes or threshold, the library versions) and what it was trained on.

**The library versions must match exactly.** `model.joblib` is a pickle, which a different scikit-learn, numpy or xgboost version can load wrongly or not at all. So the training image's versions are pinned in `ml/requirements-image.txt`, and the Lambda installs exactly those: scikit-learn 1.5.2, numpy 2.1.0, scipy 1.15.0, joblib 1.6.0 and xgboost 3.0.5. Never train with others.

**Promotion is one pointer per task.** `promote <job>` writes the SSM parameter `/anomalypulse/ml/models/<task>`. It holds the job, the package's S3 address, its exact S3 version and its validation metrics. The Lambda reads the pointer at each cold start and loads exactly that version. To undo a promotion, promote the previous job again.

**The Lambda refuses rather than guesses.** A window with a missing, extra or empty feature is refused. So is a package whose format, versions or classes don't match. Filling a missing feature with 0 silently changes predictions: in an M5-1 test, zeroing `cart_db_latency_ms_mean` changed 29 of 375 NORMAL validation answers.

---

## 2. What you do: add your model

**1. A training script,** `AnomalyPulse/ml/job/train_<model>.py`, modeled on `ml/job/train.py`. Name it after your model, never after a library: the job's code folder comes first on its import path, so an `xgboost.py` would hide xgboost itself. The launcher refuses names like that. Reuse `train.py`'s helpers rather than copying them; `import train` works, because the launcher bundles `train.py` with your script:
- `load_runs`, `training_rows` and `to_matrix` read a channel and turn it into `(X, y)`;
- `read_settings(path, known)` reads the settings SageMaker passes, given yours as `{name: (int, float or str, default)}`, for example `{"max_depth": (int, 6), "learning_rate": (float, 0.3)}`. It skips SageMaker's own settings, including the one every tuning job adds;
- `scores` computes macro F1 and accuracy.

Your script must:
- **write `model.joblib` and `model.json`** to `<root>/model`, where `<root>` is `/opt/ml` in a job;
- **print `validation:<metric>=<value>`** lines, which the tuner reads from the log;
- **stop with a clear message** on bad input. SageMaker's own failure reason only ever says "exit code 1"; your message is in the log, and the launcher prints its tail.

**2. The `model.json` fields** (the full table is in the doc):

| Field | Your value |
|---|---|
| `format` | `1` |
| `kind` | omit it for a classifier; `"detector"` for an Isolation Forest |
| `task` | the pointer's name: `incident_type` for the six-class classifier, your own for a detector (lowercase letters, digits and `_`) |
| `estimator` | a short name, such as `"xgboost"` |
| `columns` | the feature names in the order your vectors use (`train.py` sorts them) |
| `classes` | classifiers only: the class names, in `classes_` order |
| `threshold` | detectors only: a number on your model's `score_samples` scale; below it is anomalous |
| `settings`, `rows`, `runs`, `metrics` | as `train.py` writes them; `runs` must be 560 / 140 |
| `converged` | only for a model with an iterative solver; omit it otherwise |
| `libraries` | `python` plus **every library your model uses**, always including scikit-learn, numpy, scipy and joblib (add xgboost if you use it) |

**3. Launch it with `--script`.** No launcher change is needed:

```bash
python -m ml.launch train --script ml/job/train_<model>.py --set max_depth=4 --set learning_rate=0.1
python -m ml.launch tune --script ml/job/train_<model>.py --range max_depth=3:8 \
    --range learning_rate=0.01:0.3:log --set n_estimators=300 --jobs 8 --parallel 2
```

- **`--script`** bundles your script with `train.py` and `features/join_labels.py`, and names the job `anomalypulse-train-<model>-<time>` (without the `train_`).
- **`--set NAME=VALUE`** sends a setting, and is repeatable. A value that reads as JSON (`200`, `0.1`, `true`) is sent as that; anything else as text (`--set booster=gbtree`). Settings you don't send take your script's defaults.
- **`--range NAME=LOW:HIGH`** lets the tuner choose a setting. Two integers make an integer range (`max_depth=3:8`); write `1.0:100` for a continuous one. Add `:log` to search on a log scale, which needs LOW above 0.
- **`--jobs`** (default 4) and **`--parallel`** (default 2) set how many training jobs the tuner runs in all, and at once.

`train` and `tune` then check the package as they do `train.py`'s, and `promote` works for any package.

---

## 3. Try it on your machine first

Free, and fast to iterate on. You need Docker and AWS read access ([`M1-1_Handoff.md`](M1-1_Handoff.md) §5).

**Get v1's training and validation runs** (1,400 files, 33 MB, about 30 s), laid out like a job's `/opt/ml`. From `AnomalyPulse/`:

```bash
AWS_PROFILE=<your-profile> JOB_ROOT=~/anomalypulse-job python - <<'EOF'
import os, boto3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from ml import launch as L

session = boto3.Session(profile_name=os.environ["AWS_PROFILE"], region_name="us-east-2")
s3 = session.client("s3")
bucket = L.lookup(session)["telemetry_bucket"]
root = Path(os.environ["JOB_ROOT"]).expanduser()
downloads = []
for channel, ids in L.split_ids(L.train_val_records(s3, bucket)).items():
    for tree, keys in L.channel_files(ids).items():
        for key in keys:
            path = root / "input" / "data" / channel / L.CHANNEL_TREES[tree] / key
            path.parent.mkdir(parents=True, exist_ok=True)
            downloads.append((L.channel_prefix(tree) + key, str(path)))
with ThreadPoolExecutor(16) as pool:
    list(pool.map(lambda d: s3.download_file(bucket, *d), downloads))
(root / "input" / "config").mkdir(parents=True, exist_ok=True)
(root / "input" / "config" / "hyperparameters.json").write_text("{}")
print(f"{len(downloads)} files in {root}")
EOF
```

**Run a training script in the training image's Python and versions.** Build the environment once, from `AnomalyPulse/ml/`:

```bash
docker build -t anomalypulse-job:py310 -f - . <<'EOF'
FROM python:3.10-slim
COPY requirements-image.txt /tmp/
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements-image.txt
EOF
```

Then, from `AnomalyPulse/`:

```bash
docker run --rm -v ~/anomalypulse-job:/opt/ml -v "$PWD":/src:ro -e PYTHONPATH=/src \
    anomalypulse-job:py310 python /src/ml/job/train.py      # or your script
```

**Check:** `train.py` prints `validation:macro_f1=0.988426`, the same as its SageMaker job, and writes `~/anomalypulse-job/model/`. Settings go in `input/config/hyperparameters.json` as JSON-encoded text, e.g. `{"C": "0.5"}`.

Why Docker: on macOS 27, scipy 1.15's wheels fail to load in a plain venv (a `__thread_bss` error), and the job runs Python 3.10 anyway.

**Load your package in the Lambda, locally.** Build the Lambda's image from `AnomalyPulse/ml/`. Drop `--platform linux/arm64` on an Intel or AMD machine; it's only for the real deploy.

```bash
docker build --platform linux/arm64 -t anomalypulse-diagnosis:local -f diagnosis/Dockerfile .
docker run --rm -p 9000:8080 -e ANOMALYPULSE_MODEL_DIR=/model \
    -v ~/anomalypulse-job/model:/model:ro anomalypulse-diagnosis:local
```

In a second terminal, send it a run's **second** window. The first has empty change and rolling features (§8).

```bash
F=$(ls -d ~/anomalypulse-job/input/data/validation/tier1/features/experiment_id=* | head -n 1)/features.jsonl
sed -n 2p "$F" | python3 -c 'import json, sys; print(json.dumps({"windows": [json.loads(sys.stdin.readline())]}))' > event.json
curl -s -XPOST http://localhost:9000/2015-03-31/functions/function/invocations -d @event.json
```

With `train.py`'s package, that window is NORMAL 0.9908. A package the Lambda refuses fails at startup with `BadModel` and the reason.

---

## 4. Running it in SageMaker

Jobs are launched as the **trainer role**. It can create jobs named `anomalypulse-*` on `ml.m5.large` or `ml.m5.xlarge`, with network isolation on, and nothing else. Add this profile once to `~/.aws/config`:

```ini
[profile ml]
role_arn = <value of /anomalypulse/ml/trainer-role-arn in SSM>
source_profile = <your-profile>
region = us-east-2
```

```bash
aws ssm get-parameter --name /anomalypulse/ml/trainer-role-arn --query Parameter.Value --output text --profile <your-profile>
aws sts get-caller-identity --profile ml      # should show assumed-role/anomalypulse-trainer
```

Then, from `AnomalyPulse/`:

```bash
python -m ml.launch train --dry-run        # print the request; upload and create nothing
python -m ml.launch train -C 0.5           # one job: waits, prints the package's checks and its S3 version
python -m ml.launch tune                   # tunes train.py's C: 4 jobs, 2 at a time
python -m ml.launch train --script ml/job/train_<model>.py --dry-run    # your script (§2)
python -m ml.launch promote <job> --dry-run
python -m ml.launch promote <job>          # points the Lambda at it
```

- **Cost:** a job bills about 2 minutes of an `ml.m5.large`. The baseline's tuning run billed 382 s for its 4 jobs, and `--jobs` multiplies that. Jobs stop at 15 minutes.
- **Always dry-run first.** It checks every file the channels list exists, and prints the request.
- **`promote` refuses** a job that didn't complete, a package with no S3 version, or a `model.json` that fails a check. It prints the pointer it replaces; promoting that job again undoes it.
- **After a promotion,** warm copies of the Lambda keep the old model until Lambda retires them. Every reply names its model (`model.job`), so check that.
- **Don't deploy `anomalypulse-ml`;** Jake deploys it. `anomalypulse-shared` isn't deployed at all until M9-1.

---

## 5. Calling the Lambda

```bash
aws lambda invoke --function-name anomalypulse-diagnosis --cli-binary-format raw-in-base64-out \
  --payload file://event.json reply.json --profile <your-profile>
```

**Request:** `{"windows": [window, ...]}`. Each window is one `features.jsonl` line, unchanged: `window_start` plus exactly the model's columns, each an int or a float. A call carries up to about 1,300 windows.

**Reply:**

```json
{"model": {"task": "incident_type", "estimator": "logistic_regression",
           "job": "anomalypulse-hpo-261028173159-003-28af89d1", "version_id": "kNUehxgFp8NvWKB4nurs3JgoXXPOT_yY"},
 "predictions": [{"window_start": "2026-10-24T11:11:00.000Z", "label": "NORMAL",
                  "probabilities": {"NORMAL": 0.9952, "TRAFFIC_SPIKE": 0.0042, "...": "..."}}],
 "inference_ms": 0.595}
```

**Errors** come back with `FunctionError: Unhandled`:
- **`BadWindow`** for a bad window. The message names the features, for example `features missing: cart_request_rate; not in the model: cart_request_rat`.
- **`BadModel`** at startup for a bad package. Every call then fails until a good one is promoted.

**Timing:** a cold start takes 1.5–5 s, and a warm call 2–3 ms. The first start after a deploy that changes the libraries can take up to about 40 s: it runs past Lambda's 10 s startup limit and is retried inside the call. That's why the timeout is 60 s.

---

## 6. Per-person quick start (your M5 ticket)

Everyone: branch per ticket (`m5-<n>-<name>`), add your script, try it locally (§3), then in SageMaker with `--script` (§4), and open a PR.

- **Rachel — Incident classifier + threshold baseline (M5-3).**
  - **Model 0** (the threshold rule) needs no SageMaker and no package. Score it on the same validation windows (slots 561–700).
  - **The ML baseline to beat** is already trained. Logistic regression (C 1) scores validation macro F1 **0.988** (12 of 885 windows wrong, 9 of them NORMAL vs TRAFFIC_SPIKE), and tuning C to 25.8 gives **0.9923**.
  - **XGBoost** is in the training image and the Lambda. It trains on class numbers, so encode labels with `LabelEncoder` and write `model.json`'s `classes` as **`list(encoder.classes_)`, in that order**. The Lambda reads class `i` as `classes[i]`, and only checks the count, so any other order mislabels silently. List `xgboost` 3.0.5 in `libraries`, and omit `converged`.
  - **To tune,** use `tune --script` with a `--range` per setting (§2). Read your settings with `train.read_settings`, which skips `_tuning_objective_metric`, the setting SageMaker adds to every tuning job. Print `validation:macro_f1=` for the objective.
  - **Promote your pick as `incident_type`.** The Lambda then serves it with no code change, which is your Definition of Done's "loads and predicts in the Diagnosis Lambda". Check with a real call.
- **Melisa — Anomaly detection (M5-2), then the live path (M8-2).**
  - **The supervised binary detector** is a classifier package. Derive its target from the six-class label (anything but NORMAL is an anomaly), and give it its own `task`.
  - **The Isolation Forest** is a `"kind": "detector"` package with your chosen cut-off as `"threshold"` on `score_samples`. `handler.detect(pipeline, meta, vectors)` returns each window's `(score, anomalous)`. With the threshold at the model's `offset_`, it flags exactly what scikit-learn's `predict` flags.
  - **The cut-off decides almost everything.** In a quick check by Jake (not a result: default settings, fitted on NORMAL training windows), the built-in cut-off caught 67% of 510 anomalous validation windows with 30 false alarms of 375 NORMAL; a cut-off allowing 10% false alarms caught all of them. Choose it on validation, never on test.
  - **For M8-2:** the Lambda loads one model and refuses to serve a detector. Your PR adds the detector's pointer (an environment variable), its `ssm:GetParameter` in the Lambda's role in `infra/ml/template.yaml`, and the window → detector → classifier path, using `detect` and the existing handler.
- **Josh — GenAI (M6).** Bedrock permissions and anything else M6 deploys go in the `anomalypulse-ml` stack, not the shared one. The Lambda's reply (§5) is what M8-3 hands to Bedrock.
- **Linu — Dashboard (M7).** The reply's `model.job` and `model.version_id` name the model behind every diagnosis, and the cost figures for the cost view are in the doc's last section.

---

## 7. For M8 and M9 (nothing to do yet)

- **The Lambda has no trigger yet.** M8 wires it in, and invokes it directly.
- **Live windows need history.** A run's first window has 42 empty change and rolling features (§8). Training never sees it, and the Lambda refuses it. The live path should skip such windows rather than send them.
- **Cost per inference (M9-4)** is a Logs Insights query over the Lambda's `REPORT` lines; the query and the prices are in the doc. About $2.4 × 10⁻⁷ per warm call and $2.0 × 10⁻⁵ per cold start.
- **Before M9-1's first test batch, `train.py` must refuse test runs itself** (Jake). IAM can't tell test labels from training labels, so today only the launcher's channel manifests keep them out. This is in M9-1's deliverables.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A job fails with `ExecuteUserScriptError … exit code 1` | Your script stopped; SageMaker never shows why | The launcher prints the log's tail; your script's message is there. |
| `AccessDenied` creating a job | Not `--profile ml`; or the name isn't `anomalypulse-*`; or another instance type; or network isolation is off | Use the launcher with `--profile ml` (the default). |
| A tuning job is refused: `untunable hyperparameters` | The request names AWS's xgboost image, which SageMaker treats as its built-in algorithm | Launch through `tune`, which uses our copy of the image (`/anomalypulse/ml/training-image-uri`). |
| Every tuning child fails on an unknown setting | SageMaker adds `_tuning_objective_metric` to every tuning job | Read settings with `train.read_settings`, which skips it. |
| `--script` is refused | The file isn't a `.py` in `ml/job/`; or its name isn't lowercase letters, digits and `_`; or it's named after a library | Name it `ml/job/train_<model>.py`. |
| `say what to tune … with --range` | `tune --script` without a `--range` | Only `train.py` has a default range (C); give yours. |
| `promote` FAILs `libraries` | A listed version isn't the pin; a listed library isn't one the Lambda installs; or one of the four is missing | List exactly what your model uses, at the versions in `ml/requirements-image.txt`. |
| `promote` FAILs `runs` | The model wasn't trained on v1's 560 / 140 split | Train through the launcher. |
| `promote` FAILs `kind` or `task` | A detector without a numeric `threshold`; or a task name with capitals, dashes or slashes | Fix `model.json`. |
| Every call fails with `BadModel` | The promoted package fails a check | Promote the previous job again; the message says what's wrong. |
| `BadWindow: features must be int or float: … is NoneType` | A window with empty features, such as a run's first | Don't send it (§7). |
| Replies still name the old model after `promote` | Warm copies keep what they loaded | Wait, or check `model.job` on each reply. |
| The first call after a deploy takes 10–40 s | The new image isn't cached yet | Normal after a libraries change; the 60 s timeout covers it. |
| `ImportError` loading scipy in a Mac venv | scipy 1.15's macOS wheels on macOS 27 | Use the Docker environment (§3). |
| `ExpiredToken` / `security token is invalid` | SSO session expired | `aws sso login --profile <your-profile>`; the `ml` profile renews from it. |

**Golden rule:** train locally first (§3), then dry-run (`--dry-run`) before anything that bills.

---

## 9. Your M5 checklist

- [ ] Pulled `main`; created your ticket branch `m5-<n>-<name>`; read `ml/job/train.py` and `Docs/ML_INFERENCE.md`
- [ ] Ran `train.py` locally in Docker and saw `validation:macro_f1=0.988426` (§3)
- [ ] Your script writes `model.joblib` + `model.json` (format 1, your `task`, every library you use at the pinned version)
- [ ] It trains only on the `train` channel, and uses `validation` only to score and choose (settings, a cut-off)
- [ ] Your package loads and answers in the local Lambda (§3)
- [ ] `train --script ml/job/train_<model>.py --dry-run` runs, then the real job trains in SageMaker and its checks all PASS
- [ ] (Rachel) `classes` written in `LabelEncoder` order; Model 0 scored on the same validation windows; your pick promoted as `incident_type` and checked with a real call
- [ ] (Melisa) Isolation Forest packaged as a detector with its `threshold`; the binary detector packaged as a classifier, under its own `task`
- [ ] PR opened to `main`, with your validation metrics beside the baselines

---

*Questions → ping Jake. The reference of record is [`Docs/ML_INFERENCE.md`](../Docs/ML_INFERENCE.md); the code that enforces it is `AnomalyPulse/ml/launch.py` (the checks) and `AnomalyPulse/ml/diagnosis/handler.py` (the Lambda).*

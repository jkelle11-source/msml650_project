# Model Packages and the Diagnosis Lambda (M5-1)

**Owner:** Jake · **Launcher:** `AnomalyPulse/ml/launch.py` · **Training script:** `AnomalyPulse/ml/job/train.py` ·
**Lambda:** `AnomalyPulse/ml/diagnosis/` (`handler.py`, `Dockerfile`) · **Stack:** `anomalypulse-ml`
(`AnomalyPulse/infra/ml/template.yaml`)

How a trained model gets from a SageMaker job into the Diagnosis Lambda, what a package must hold for the
Lambda to load it, how to call the Lambda, and how to measure what each call costs (M9-4).

---

## The path, end to end

1. `python -m ml.launch train` (or `tune`) trains in SageMaker. SageMaker uploads the job's `/opt/ml/model`
   to `s3://anomalypulse-ml-<account>-us-east-2/training/<job>/output/model.tar.gz`. The bucket is
   versioned, so every upload keeps its own version ID.
2. `python -m ml.launch promote <job>` checks that package and writes its address and version to the SSM
   parameter `/anomalypulse/ml/models/incident_type` (the **pointer**).
3. Each cold start of the `anomalypulse-diagnosis` Lambda reads the pointer, fetches exactly that version
   into `/tmp/model` and loads it. Warm calls reuse the loaded model.

There is no SageMaker endpoint. SageMaker is used for training only, and between calls nothing runs and
nothing is billed except storage.

## Package format 1

`model.tar.gz` holds two files at its root. The Lambda unpacks only these two.

| File | What it is |
|---|---|
| `model.joblib` | The fitted model, saved with `joblib.dump`: a scikit-learn-style estimator, or a `Pipeline` ending in one. Today's is `StandardScaler` → `LogisticRegression`. |
| `model.json` | What using the model takes, and what it was trained on. |

A package is one of two **kinds**:

- A **classifier** (the default) answers with a probability per class. Its model must have `predict_proba`
  and `classes_`: logistic regression, random forest, XGBoost's `XGBClassifier`, the supervised binary
  detector.
- A **detector** answers with a score and whether that score is anomalous. Its model must have
  `score_samples` (Isolation Forest has it, and so does a `Pipeline` ending in one), and its `model.json`
  records a `threshold`: a window is anomalous when its `score_samples` score is below it.

`model.json`'s fields:

| Field | The promoted model's value | Checked by |
|---|---|---|
| `format` | `1` | Lambda and `promote`: anything else is refused |
| `kind` | absent, which means `"classifier"` | Lambda and `promote`: `"classifier"` or `"detector"` |
| `threshold` | (detectors only) | Lambda and `promote`: a detector must have a finite number |
| `task` | `"incident_type"` | `promote`: lowercase letters, digits and `_`; names the pointer |
| `estimator` | `"logistic_regression"` | (shown in the Lambda's reply) |
| `columns` | the 106 feature names, sorted | Lambda: the order it builds each window's vector in |
| `classes` | the 6 incident classes, in `classes_` order (classifiers only) | Lambda: the model's `classes_` must be these names, or the numbers `0..5` standing for them in order (XGBoost trains on numbers) |
| `settings` | `{"C": 25.818801896635723, "max_iter": 1000}` | (record) |
| `converged` | `true` (only for a model with an iterative solver) | `promote`: `false` is refused |
| `rows` | `{"train": 3553, "validation": 885}` | (record) |
| `runs` | `{"train": 560, "validation": 140}` | `promote`: must equal v1's split today |
| `metrics` | validation `macro_f1` 0.9923, `accuracy` 0.9898 (and the same for train) | `promote` copies the validation metrics into the pointer |
| `libraries` | `python` 3.10.22, `scikit-learn` 1.5.2, `numpy` 2.1.0, `scipy` 1.15.0, `joblib` 1.6.0 | Lambda and `promote`: see below |

**Why the library versions must match exactly.** `model.joblib` is a pickle: it stores Python objects by
their internal layout, and a different library version can load it wrongly or not at all. So a package
lists every library its model uses, always including scikit-learn, numpy, scipy and joblib, and every
version it lists must be the one in `ml/requirements-image.txt` (the training image's versions, which the
Lambda installs). A library the Lambda doesn't install is refused. The Lambda runs Python 3.13 rather than
training's 3.10; on 2026-10-28 and 10-29, the promoted model, an XGBoost classifier and an Isolation Forest
each gave identical outputs under both on all 885 validation windows.

**Adding your model (M5-2, M5-3).** Train it through the launcher (the training image has every pinned
library) and write the same two files, with its own `task`:

- **An XGBoost classifier** lists `xgboost` 3.0.5 in `libraries`. The Lambda installs it, and accepts its
  numbered classes as described above.
- **An Isolation Forest** is a detector. Choose its threshold however you like (scikit-learn's own
  `predict` uses `offset_`, which the default `contamination="auto"` sets to −0.5), and record it in
  `model.json`; the Lambda never picks one. `handler.detect(pipeline, meta, vectors)` returns each window's
  `(score, anomalous)`. With `threshold` set to the model's `offset_`, it flags exactly what `predict` flags.
- **Serving more than one model is M8-2's.** The Lambda loads one model at startup, the one its
  `ANOMALYPULSE_MODEL_POINTER` names, and refuses a detector there. M8-2 adds the detector's pointer, its
  read permission in the stack, and the window → detector → classifier path that uses `detect`.

## Promoting a model

```bash
cd AnomalyPulse
python -m ml.launch promote <training-job-name> --dry-run   # every check; writes nothing
python -m ml.launch promote <training-job-name>             # as the `ml` profile
```

For a tuned model, promote the tuning job's best training job; `tune` prints its name.

`promote` refuses unless the job completed, its package has a version ID, and `model.json` passes the
checks in the table above. It then prints the current pointer and writes the new one to
`/anomalypulse/ml/models/<task>`. For the promoted model, `/anomalypulse/ml/models/incident_type`:

```json
{"job": "anomalypulse-hpo-261028173159-003-28af89d1",
 "uri": "s3://anomalypulse-ml-<account>-us-east-2/training/anomalypulse-hpo-261028173159-003-28af89d1/output/model.tar.gz",
 "version_id": "kNUehxgFp8NvWKB4nurs3JgoXXPOT_yY",
 "validation": {"macro_f1": 0.9923470358173319, "accuracy": 0.9898305084745763}}
```

One parameter holds all four fields, so a job and its version always change together. SSM keeps every
earlier value (`aws ssm get-parameter-history --name /anomalypulse/ml/models/incident_type`). The Lambda
reads only `uri` and `version_id`.

- **To undo a promotion,** promote the previous job again. `promote` prints it before replacing it.
- **A promotion takes effect at each copy's next cold start.** Copies of the Lambda that are already warm
  keep the model they loaded until Lambda retires them, so for a while after a promotion either model may
  answer. Every reply names its model (`model.job`, `model.version_id`). Switching every copy at once is
  not automated yet.

## Calling the Lambda

`anomalypulse-diagnosis` (us-east-2): an arm64 container image with 1024 MB and a 60 s timeout. It has no
API or trigger yet (M8 wires it in), so it is invoked directly:

```bash
aws lambda invoke --function-name anomalypulse-diagnosis --cli-binary-format raw-in-base64-out \
  --payload file://event.json reply.json
```

**Request:** `{"windows": [window, ...]}`. A window is one line of
`tier1/features/experiment_id=<id>/features.jsonl`, unchanged: `window_start` plus exactly the model's
`columns`, each an int or a float. One window is about 4.5 KB, so a call can carry about 1,300 windows
before Lambda's 6 MB request limit.

**Reply** for the validation window of slot 561 at 11:11 (probabilities rounded here):

```json
{"model": {"task": "incident_type", "estimator": "logistic_regression",
           "job": "anomalypulse-hpo-261028173159-003-28af89d1",
           "version_id": "kNUehxgFp8NvWKB4nurs3JgoXXPOT_yY"},
 "predictions": [{"window_start": "2026-10-24T11:11:00.000Z", "label": "NORMAL",
                  "probabilities": {"CASCADING_FAILURE": 5.4e-13, "DATABASE_THROTTLING": 2.2e-07,
                                    "DEPENDENCY_FAILURE": 5.4e-09, "LAMBDA_DEGRADATION": 0.0006,
                                    "NORMAL": 0.9952, "TRAFFIC_SPIKE": 0.0042}}],
 "inference_ms": 0.595}
```

Predictions come back in request order. `label` is the class with the highest probability.
`inference_ms` times only building the vectors and `predict_proba`, not the whole call.

**Errors.** The Lambda refuses a bad window rather than guessing a value for it. In an M5-1 test on the
375 NORMAL validation windows, setting any one of 31 of the 106 features to 0 changed some predictions;
for `cart_db_latency_ms_mean`, 29 of them, mostly to TRAFFIC_SPIKE. A refused call comes back with
`FunctionError: Unhandled` and a payload holding `errorType` and `errorMessage`:

| `errorType` | When | Effect |
|---|---|---|
| `BadWindow` | The event has no windows, or a window has a missing or extra feature, or a value that isn't a number | The whole call is refused, even when only one window is bad |
| `BadModel` | At startup: the package fails a check in the `model.json` table (format, kind, threshold, libraries, classes), or the served model is a detector | Every call fails until a good model is promoted |

A startup that fails for AWS reasons (a missing pointer, no access) fails every call the same way, with
botocore's error type.

**Running it locally.** The AWS base image includes Lambda's local emulator. Point it at an unpacked package
instead of the pointer:

```bash
cd AnomalyPulse/ml
docker build --platform linux/arm64 -t anomalypulse-diagnosis:local -f diagnosis/Dockerfile .
docker run --rm -p 9000:8080 -e ANOMALYPULSE_MODEL_DIR=/model -v <unpacked package>:/model:ro anomalypulse-diagnosis:local
curl -s -XPOST http://localhost:9000/2015-03-31/functions/function/invocations -d @event.json
```

## Cold starts (measured 2026-10-29)

| | Startup (`Init Duration`) | Call (`Duration`) | Billed |
|---|---|---|---|
| Warm call | none | 2–3 ms | 3 ms |
| Cold start | 1.5–5.0 s (4 seen) | 2–3 ms | startup + call, e.g. 1,484 ms |
| First cold start after a deploy that changed the libraries (2 seen) | ran past 10 s both times, and was retried inside the call | 29.3 s and 9.5 s | 29,303 and 9,509 ms |
| First cold start after a deploy that changed only `handler.py` (1 seen) | 3.7 s | 3 ms | 3,693 ms |

Memory used: 232–235 MB of 1024 MB.

A cold start imports scikit-learn, scipy and numpy, reads the pointer, fetches the package (8.7 KB) and
loads it. Lambda allows startup 10 s; when startup runs over, Lambda retries it inside the first call,
under the function's timeout.

The first start after a deploy reads the parts of the new image that Lambda hasn't cached yet. That is an
inference, but it fits every case seen on 2026-10-29:
- the two slow starts came after deploys that changed the image's large library layer (the first image,
  then adding xgboost);
- a deploy that changed only `handler.py`, the image's small top layer, started in 3.7 s;
- a deploy that changed only the timeout (the same image) started in 1.5 s;
- later starts took 1.5–5 s.

So expect one slow first start after any change to `ml/requirements-image.txt`. It is why the timeout is
60 s rather than 30 s.

## Cost per inference (the M9-4 hook)

Every call writes a `REPORT` line to the log group `/aws/lambda/anomalypulse-diagnosis` (kept 90 days) with
what it was billed. This Logs Insights query totals them for any time range:

```text
filter @type = "REPORT"
| stats count(*) as calls, sum(ispresent(@initDuration)) as cold_starts,
        sum(@billedDuration) as billed_ms, max(@maxMemoryUsed) / 1000000 as max_mb
```

Lambda's arm64 prices in us-east-2 (AWS price list, 2026-10-29) turn that into dollars:

```text
cost = billed_ms / 1000 × (1024 / 1024) × $0.0000133334   per GB-second, at 1024 MB = 1 GB
     + calls × $0.0000002                                  $0.20 per million requests
```

The GB-second price drops only above 7.5 billion GB-seconds a month.

| Measured on 2026-10-29 | Billed | Cost |
|---|---|---|
| One warm call | 3 ms | ≈ $2.4 × 10⁻⁷ ($4.0 × 10⁻⁸ compute + $2.0 × 10⁻⁷ request) |
| One cold start | 1,484 ms | ≈ $2.0 × 10⁻⁵ |
| The first 19 test calls | 38,861 ms | ≈ $0.00052 |

- **A warm call's request fee is five times its compute,** so sending many windows per call is what lowers
  the cost per window.
- **Idle cost is storage only:** the image in ECR at $0.10 per GB-month, plus the model packages in S3 (a
  few KB each). SAM keeps every image it pushes, so each deploy that changes the image adds one: two on
  2026-10-29 (282 MB, then 287 MB with xgboost), about $0.06 a month together. No lifecycle rule prunes
  them yet. Each cold start also makes one SSM read (free for standard parameters) and one S3 GET ($0.0004
  per 1,000).
- **A startup that timed out has no `Init Duration`** (its `INIT_REPORT` line says `Status: timeout`), so
  `cold_starts` undercounts it. Its `REPORT` line bills only the retry inside the call; AWS's docs don't
  say whether the timed-out 10 s is billed too. If it is, that adds at most 10 GB-seconds (about
  $0.00013) per such start, which so far has happened once after each deploy that changed the libraries.
- **The reply's `inference_ms` is not billed time.** Bill from the `REPORT` lines.

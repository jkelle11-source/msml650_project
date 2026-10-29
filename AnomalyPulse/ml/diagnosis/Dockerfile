# The Diagnosis Lambda (M5-1). Built from AnomalyPulse/ml/:
#   docker build --platform linux/arm64 -t anomalypulse-diagnosis:local -f diagnosis/Dockerfile .
# AWS's Python 3.13 base, as the other Lambdas run; the training image's library
# versions, which the model was saved with (2026-10-28: identical predictions).
FROM public.ecr.aws/lambda/python:3.13

# The libraries first: editing handler.py then reuses this layer instead of reinstalling.
# Wheels only: a pinned version missing for this Python fails the build, never compiles.
COPY requirements-image.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir --only-binary=:all: -r ${LAMBDA_TASK_ROOT}/requirements-image.txt

COPY diagnosis/handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]

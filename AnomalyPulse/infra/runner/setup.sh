#!/bin/bash
# Prepare the M4 runner host (M4-1) to run batches. Run it as ec2-user:
#
#     bash setup.sh <git ref>        # a commit, branch or tag of this repo
#
# It clones the repo (or fetches), checks out <ref> detached, so the batch runs
# from one fixed commit, installs the simulator into ~/venv, and writes the
# `sim` profile: the simulator role, assumed with the instance's own role.
# Rerun it to move to another commit. Docs/DATASET.md has the whole runbook.
set -euo pipefail

REF="${1:?usage: setup.sh <commit, branch or tag to run>}"
REPO="${REPO:-https://github.com/jkelle11-source/msml650_project.git}"
CHECKOUT="$HOME/msml650_project"

PYTHON="$(command -v python3.13 || command -v python3.12 || true)"
if [ -z "$PYTHON" ]; then
  echo "error: no python3.13 or python3.12; the instance's user data installs one" >&2
  exit 1
fi

[ -d "$CHECKOUT/.git" ] || git clone --quiet "$REPO" "$CHECKOUT"
cd "$CHECKOUT"
git fetch --quiet --prune origin
# A branch name means the remote's branch; anything else is taken as given.
git checkout --quiet --detach "origin/$REF" 2>/dev/null || git checkout --quiet --detach "$REF"

[ -x "$HOME/venv/bin/python" ] || "$PYTHON" -m venv "$HOME/venv"
"$HOME/venv/bin/pip" install --quiet --upgrade pip
"$HOME/venv/bin/pip" install --quiet -r AnomalyPulse/simulator/requirements.txt

# The role name is fixed by infra/shared/template.yaml; the account is this one.
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
mkdir -p "$HOME/.aws" "$HOME/logs"
cat > "$HOME/.aws/config" <<EOF
[profile sim]
role_arn = arn:aws:iam::${ACCOUNT}:role/anomalypulse-simulator
credential_source = Ec2InstanceMetadata
region = us-east-2
EOF

echo "checkout: $(git rev-parse HEAD) ($REF)"
echo "python:   $("$HOME/venv/bin/python" --version)"
echo "sim:      $(aws sts get-caller-identity --profile sim --query Arn --output text | sed -E 's/[0-9]{12}/<account>/')"

# sam build for IncidentsFunction (infra/incidents/template.yaml, BuildMethod: makefile).
# Bundles incidents/ with the three genai modules it imports (contract.py, affected.py, and
# self_check.py for its finding kinds) and
# ml/diagnosis/incident.py, which goes in beside the others as incidents/incident.py (core.py
# imports it from there). No pip install: boto3 is already in the Lambda runtime.
# ROOT is found through git, so this works from sam's copy of the folder as well as from here.
ROOT := $(shell git rev-parse --show-toplevel)/AnomalyPulse

build-IncidentsFunction: ; \
  mkdir -p "$(ARTIFACTS_DIR)/incidents" "$(ARTIFACTS_DIR)/genai" && \
  cp "$(ROOT)/incidents/__init__.py" "$(ROOT)/incidents/builder.py" "$(ROOT)/incidents/record.py" "$(ROOT)/incidents/core.py" "$(ROOT)/incidents/handler.py" "$(ARTIFACTS_DIR)/incidents/" && \
  cp "$(ROOT)/ml/diagnosis/incident.py" "$(ARTIFACTS_DIR)/incidents/incident.py" && \
  cp "$(ROOT)/genai/__init__.py" "$(ROOT)/genai/contract.py" "$(ROOT)/genai/affected.py" "$(ROOT)/genai/self_check.py" "$(ARTIFACTS_DIR)/genai/"
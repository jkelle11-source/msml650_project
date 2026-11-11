# sam build for DashboardFunction (infra/dashboard/template.yaml, BuildMethod: makefile).
# Bundles the read API (handler.py, builder.py, mock_source.py, live_source.py) at the bundle's top level, where
# handler.handler and its flat imports find them, and the incident store's record module with the modules
# it imports (incidents/record.py and builder.py; genai/contract.py, affected.py, self_check.py): M8-5's
# live source reads stored records through incidents.record.to_incident (Docs/INCIDENT_FEED.md).
# incidents/test_bundle.py checks that this copies every module the bundle's code imports.
# No pip install: the read API needs only the standard library (and boto3, in the Lambda runtime, for M8-5).
# ROOT is found through git, so the build runs in the source folder (samconfig.toml: build_in_source).
ROOT := $(shell git rev-parse --show-toplevel)/AnomalyPulse

build-DashboardFunction: ; \
  mkdir -p "$(ARTIFACTS_DIR)/incidents" "$(ARTIFACTS_DIR)/genai" && \
  cp "$(ROOT)/dashboard/handler.py" "$(ROOT)/dashboard/builder.py" "$(ROOT)/dashboard/mock_source.py" "$(ROOT)/dashboard/live_source.py" "$(ARTIFACTS_DIR)/" && \
  cp "$(ROOT)/incidents/__init__.py" "$(ROOT)/incidents/builder.py" "$(ROOT)/incidents/record.py" "$(ARTIFACTS_DIR)/incidents/" && \
  cp "$(ROOT)/genai/__init__.py" "$(ROOT)/genai/contract.py" "$(ROOT)/genai/affected.py" "$(ROOT)/genai/self_check.py" "$(ARTIFACTS_DIR)/genai/"

# sam build for ExplainFunction (infra/genai/template.yaml, BuildMethod: makefile).
# The package goes in as genai/ so its imports (from genai import ...) work in Lambda;
# tests, samples and the sample runner stay out. Dependencies are installed as
# arm64 Linux wheels: a plain pip install on a Mac would bundle macOS builds of
# pydantic-core and the like, which fail to import in Lambda.
build-ExplainFunction:
	mkdir -p "$(ARTIFACTS_DIR)/genai"
	cp __init__.py contract.py prompt.py self_check.py explain.py handler.py "$(ARTIFACTS_DIR)/genai/"
	python3 -m pip install -r requirements.txt \
	  --platform manylinux2014_aarch64 --python-version 3.13 \
	  --implementation cp --only-binary=:all: \
	  -t "$(ARTIFACTS_DIR)"

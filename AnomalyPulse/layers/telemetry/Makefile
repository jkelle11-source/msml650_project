build-TelemetryLayer:
	mkdir -p "$(ARTIFACTS_DIR)/python"
	cp -r python/. "$(ARTIFACTS_DIR)/python/"
	pip install -r requirements.txt \
	  --platform manylinux2014_aarch64 --python-version 3.13 \
	  --implementation cp --only-binary=:all: \
	  -t "$(ARTIFACTS_DIR)/python"
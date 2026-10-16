# Every build of the layer has the same bytes, so a deploy that doesn't change
# it doesn't publish a new layer version, which would update every function
# (M4-1). So: pinned requirements; no bytecode, neither pip's nor what local
# test runs leave in python/__pycache__ (Lambda's 3.13 ignores other versions'
# bytecode anyway); no bin/, whose scripts name the builder's Python on their
# first line; and no dist-info RECORD files, which hash those scripts. Lambda
# uses none of them.
build-TelemetryLayer:
	mkdir -p "$(ARTIFACTS_DIR)/python"
	cp -r python/. "$(ARTIFACTS_DIR)/python/"
	python3 -m pip install -r requirements.txt \
	  --platform manylinux2014_aarch64 --python-version 3.13 \
	  --implementation cp --only-binary=:all: --no-compile \
	  -t "$(ARTIFACTS_DIR)/python"
	rm -rf "$(ARTIFACTS_DIR)/python/bin"
	find "$(ARTIFACTS_DIR)/python" -type d -name __pycache__ -prune -exec rm -rf {} +
	find "$(ARTIFACTS_DIR)/python" -path '*.dist-info/RECORD' -delete

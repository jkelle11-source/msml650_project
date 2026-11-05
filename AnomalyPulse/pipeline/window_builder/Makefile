# The window builder's Lambda package. sam build runs this target here, in the
# source folder (build_in_source in infra/pipeline/samconfig.toml), so the paths
# below reach the repository's own features/ and simulator/.
#
# build_matrix needs features/, and its Feature/Label Boundary guard imports
# simulator/ (the injector registry and the manifest schema) and reads two files
# by path from the package root. jsonschema's rpds-py is compiled, so pip fetches
# the wheels built for Lambda's Linux on arm64, never the ones for this machine.

ROOT := ../..

build-WindowBuilderFunction:
	cp handler.py $(ARTIFACTS_DIR)/
	rsync -a --exclude '__pycache__' --exclude 'test_*' $(ROOT)/features $(ARTIFACTS_DIR)/
	mkdir -p $(ARTIFACTS_DIR)/simulator $(ARTIFACTS_DIR)/infra/shared
	cp $(ROOT)/simulator/__init__.py $(ROOT)/simulator/manifest.py $(ROOT)/simulator/manifest_schema.json \
	   $(ROOT)/simulator/control_plane.py $(ARTIFACTS_DIR)/simulator/
	rsync -a --exclude '__pycache__' --exclude 'test_*' $(ROOT)/simulator/injectors $(ARTIFACTS_DIR)/simulator/
	cp $(ROOT)/infra/shared/field_catalog.json $(ARTIFACTS_DIR)/infra/shared/
	python3 -m pip install --quiet --no-compile --target $(ARTIFACTS_DIR) --platform manylinux2014_aarch64 \
	   --implementation cp --python-version 3.13 --only-binary=:all: -r requirements.txt
	rm -rf $(ARTIFACTS_DIR)/bin

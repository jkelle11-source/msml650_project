"""Write forbidden_names.json: the M4-5 guard's list, for the Diagnosis Lambda (M8-2).

    cd AnomalyPulse && python ml/diagnosis/write_forbidden_names.py

The Lambda's image is built from ml/ alone, so it can't import features.guard, which reads the
field catalog, the manifest schema and the injector registry. This copies the guard's list
into a file the image does hold. ml/diagnosis/test_handler.py fails while the file differs from
features.guard.forbidden_names(), so rerun this whenever the catalog, the schema or an injector
changes, and commit the file.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUT = HERE / "forbidden_names.json"


def main():
    sys.path.insert(0, str(ROOT))
    from features import guard
    names = sorted(guard.forbidden_names())
    OUT.write_text(json.dumps({"source": "features.guard.forbidden_names()", "names": names}, indent=1) + "\n")
    print(f"wrote {len(names)} names to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
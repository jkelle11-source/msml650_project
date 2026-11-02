"""Run the sample inputs through Bedrock and self-check each diagnosis (M6-1's Definition of Done).

    cd AnomalyPulse
    python -m genai.run_samples --profile <your-profile>                       # the default model
    python -m genai.run_samples --profile <your-profile> --only dependency_failure
    python -m genai.run_samples --profile <your-profile> \\
        --model <model id> --model <another model id>                         # compare models

Each call costs a little (a few thousand tokens). Results are written to
genai/samples/results/<model>/<sample>.json; with more than one --model, a
table compares the models at the end.
"""
import argparse
import json
import os
import sys
from pathlib import Path

SAMPLES = Path(__file__).parent / "samples"
RESULTS = SAMPLES / "results"


def _print_result(name, evidence, result):
    d, check = result["diagnosis"], result["self_check"]
    print(f"{'PASS' if check['passed'] else 'FLAG'}  {name}  ({evidence['incident_type']}, "
          f"{result['latency_ms']} ms, {result['usage']['input_tokens']} in / "
          f"{result['usage']['output_tokens']} out tokens)")
    print(f"      severity: {d['severity']}")
    print(f"      summary: {d['summary']}")
    print(f"      root cause: {d['root_cause']}")
    for bullet, refs in zip(d["evidence"], result["evidence_metrics"]):
        print(f"      - {bullet}  [{', '.join(refs)}]")
    for n, action in enumerate(d["recommended_actions"], 1):
        print(f"      {n}. {action}")
    for finding in check["findings"]:
        print(f"      ! {finding}")


def _run_model(explain, model, names):
    """{sample: result or None} for one model; prints each diagnosis."""
    out_dir = RESULTS / model
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    print(f"===== {model}")
    for name in names:
        evidence = json.loads((SAMPLES / f"{name}.json").read_text())
        try:
            result = explain.diagnose(evidence, model=model)
        except Exception as e:
            print(f"ERROR {name}: {type(e).__name__}: {e}")
            results[name] = None
            continue
        (out_dir / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        _print_result(name, evidence, result)
        results[name] = result
    return results


def _compare(by_model, names):
    print("===== comparison")
    print(f"{'model':<40}{'passed':>8}{'findings':>10}{'median ms':>11}{'in tok':>9}{'out tok':>9}")
    for model, results in by_model.items():
        done = [r for r in results.values() if r]
        if not done:
            print(f"{model:<40}{'errors':>8}")
            continue
        latencies = sorted(r["latency_ms"] for r in done)
        print(f"{model:<40}{sum(r['self_check']['passed'] for r in done):>5}/{len(names):<2}"
              f"{sum(len(r['self_check']['findings']) for r in done):>10}"
              f"{latencies[len(latencies) // 2]:>11}"
              f"{sum(r['usage']['input_tokens'] for r in done):>9}"
              f"{sum(r['usage']['output_tokens'] for r in done):>9}")
    print(f"\n{'sample':<22}" + "".join(f"{m.split('.')[-1][:24]:>26}" for m in by_model))
    for name in names:
        cells = []
        for results in by_model.values():
            r = results.get(name)
            cells.append("error" if not r else
                         f"{r['diagnosis']['severity']}, {len(r['self_check']['findings'])} findings")
        print(f"{name:<22}" + "".join(f"{c:>26}" for c in cells))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m genai.run_samples", description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="AWS profile allowed to call Bedrock")
    p.add_argument("--only", action="append", help="sample name (repeatable); default: all")
    p.add_argument("--model", action="append", help="Bedrock model id (repeatable); default: explain.MODEL_ID")
    args = p.parse_args(argv)

    # The Bedrock client reads the standard AWS chain, so the profile goes in before it's created.
    os.environ["AWS_PROFILE"] = args.profile
    from genai import explain

    names = args.only or sorted(f.stem for f in SAMPLES.glob("*.json"))
    models = args.model or [explain.MODEL_ID]
    by_model = {model: _run_model(explain, model, names) for model in models}
    if len(models) > 1:
        _compare(by_model, names)

    ok = all(r and r["self_check"]["passed"] for results in by_model.values() for r in results.values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    raise SystemExit(main())

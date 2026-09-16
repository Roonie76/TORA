import argparse
import json
import sys
from pathlib import Path

from .harness import EvalHarness, load_scenarios, run_suite, to_markdown


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the TORA evaluation benchmark.")
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--model", help="Ollama model for live mode (defaults to OLLAMA_MODEL)")
    parser.add_argument("--category", help="Only run one category")
    parser.add_argument("--scenarios", type=Path, help="Alternative scenario file")
    parser.add_argument("--real-web", action="store_true", help="Live mode: use real search/fetch/research")
    parser.add_argument("--grounding", choices=["off", "annotate", "regenerate"], default="regenerate")
    parser.add_argument("--out", type=Path, help="Write JSON report here")
    parser.add_argument("--md", type=Path, help="Write Markdown summary here")
    parser.add_argument("--min-pass-rate", type=float, default=None,
                        help="Exit non-zero if the scenario pass rate is below this (0-1)")
    args = parser.parse_args(argv)

    provider = None
    if args.mode == "live":
        from ..llm import OllamaProvider
        provider = OllamaProvider(default_model=args.model) if args.model else OllamaProvider()
    harness = EvalHarness(mode=args.mode, provider=provider, real_web=args.real_web, grounding_mode=args.grounding)
    report = run_suite(harness, load_scenarios(args.scenarios, args.category))
    md = to_markdown(report)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.md:
        args.md.write_text(md, encoding="utf-8")
    print(md)
    if args.min_pass_rate is not None and (report["scenario_pass_rate"] or 0) < args.min_pass_rate:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

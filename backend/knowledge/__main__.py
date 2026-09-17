"""python -m backend.knowledge check | search "<query>" [tax_year]"""
import json
import sys

from .library import RulesLibrary


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    lib = RulesLibrary.load()
    if not argv or argv[0] == "check":
        problems = lib.check()
        print(f"Rules library {lib.version}: {len(lib.rules)} rules")
        for p in problems:
            print(" -", p)
        print("OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if argv[0] == "search" and len(argv) >= 2:
        print(json.dumps(lib.search(argv[1], argv[2] if len(argv) > 2 else None), indent=1, ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Release gate: python -m backend.check  (tests + offline benchmark + rules library)."""
import subprocess
import sys


def main() -> int:
    steps = [
        ("unit tests", [sys.executable, "-m", "pytest", "-q", "backend/tests"]),
        ("offline benchmark", [sys.executable, "-m", "backend.evals", "--mode", "offline", "--min-pass-rate", "1.0"]),
        ("rules library", [sys.executable, "-m", "backend.knowledge", "check"]),
    ]
    failed = []
    for name, cmd in steps:
        print(f"== {name}: {' '.join(cmd[2:])}", flush=True)
        if subprocess.call(cmd) != 0:
            failed.append(name)
    print("RELEASE GATE:", "PASSED" if not failed else "FAILED — " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

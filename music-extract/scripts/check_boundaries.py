import re
import sys
from pathlib import Path

MUSICTOOLS_DIR = Path(__file__).resolve().parent.parent / "musictools"

FORBIDDEN_IMPORT = re.compile(r"^\s*(from|import)\s+(app|config|core|infrastructure|api)(\.|\s)")
FORBIDDEN_FILE_USAGE = re.compile(r"__file__")
FORBIDDEN_ENV_USAGE = re.compile(r"load_dotenv|os\.getenv")


def check() -> list[str]:
    violations = []

    for path in MUSICTOOLS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")

        for lineno, line in enumerate(text.splitlines(), 1):
            if FORBIDDEN_IMPORT.match(line):
                violations.append(f"{path}:{lineno}: musictools no puede importar de app/ -> {line.strip()}")

            if FORBIDDEN_FILE_USAGE.search(line):
                violations.append(f"{path}:{lineno}: musictools no resuelve rutas desde __file__ -> {line.strip()}")

            if FORBIDDEN_ENV_USAGE.search(line):
                violations.append(f"{path}:{lineno}: musictools no lee .env directamente -> {line.strip()}")

    return violations


def main() -> None:
    violations = check()

    if violations:
        print("\n".join(violations))
        sys.exit(1)

    print("OK: musictools respeta sus limites (sin imports de app/, sin __file__, sin .env).")


if __name__ == "__main__":
    main()

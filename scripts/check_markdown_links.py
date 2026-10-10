"""Check that local Markdown links resolve inside the repository."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for markdown in sorted(root.rglob("*.md")):
        if any(part in {".git", ".venv", "work", "outputs"} for part in markdown.parts):
            continue
        text = markdown.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = raw_target.strip().split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            target_path = (markdown.parent / unquote(target)).resolve()
            try:
                target_path.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{markdown.relative_to(root)}: link leaves repository: {raw_target}")
                continue
            if not target_path.exists():
                errors.append(f"{markdown.relative_to(root)}: missing target: {raw_target}")
    return errors


def main() -> None:
    root = Path.cwd()
    errors = check(root)
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print("All local Markdown links resolve.")


if __name__ == "__main__":
    main()

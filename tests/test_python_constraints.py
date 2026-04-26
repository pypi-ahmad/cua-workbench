from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONSTRAINTS = ROOT / "constraints.txt"
REQUIREMENTS = [ROOT / "requirements.txt", ROOT / "requirements-dev.txt"]


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _iter_requirement_lines(path: Path):
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-r"):
            continue
        yield line


def _requirement_name(line: str) -> str:
    return _normalize(re.split(r"[<>=!~\[]", line, maxsplit=1)[0])


class TestPythonConstraints(unittest.TestCase):
    def test_all_requirements_have_upper_bounds(self):
        for path in REQUIREMENTS:
            for line in _iter_requirement_lines(path):
                self.assertIn("<", line, f"{path.name} requirement missing upper bound: {line}")

    def test_constraints_pin_every_top_level_requirement(self):
        constraints = {
            _normalize(line.split("==", 1)[0])
            for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "==" in line
        }

        for path in REQUIREMENTS:
            for line in _iter_requirement_lines(path):
                self.assertIn(
                    _requirement_name(line),
                    constraints,
                    f"constraints.txt missing pin for {line}",
                )


if __name__ == "__main__":
    unittest.main()
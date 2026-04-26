"""Lock-in test for backend.tools.action_aliases (F-007).

Python silently keeps the last value for duplicate dict keys, which
hid that ``activate_window`` had two conflicting mappings. This test
parses the source AST and asserts no key appears twice in the
``ACTION_ALIASES`` dict literal.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


_ALIASES_PATH = Path(__file__).resolve().parents[1] / "backend" / "tools" / "action_aliases.py"


def _collect_alias_keys() -> list[str]:
    src = _ALIASES_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    keys: list[str] = []
    for node in ast.walk(tree):
        target_name: str | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value = node.value
        if target_name == "ACTION_ALIASES" and isinstance(value, ast.Dict):
            for k in value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.append(k.value)
    return keys


def test_aliases_dict_has_no_duplicate_keys() -> None:
    keys = _collect_alias_keys()
    assert keys, "ACTION_ALIASES dict not found in action_aliases.py"
    counts = Counter(keys)
    duplicates = {k: c for k, c in counts.items() if c > 1}
    assert not duplicates, f"duplicate alias keys: {duplicates}"


def test_activate_window_maps_to_focus_window() -> None:
    from backend.tools.action_aliases import ACTION_ALIASES

    assert ACTION_ALIASES["activate_window"] == "focus_window"

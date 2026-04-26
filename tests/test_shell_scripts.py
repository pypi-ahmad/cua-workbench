from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_setup_sh_documents_and_handles_check_mode():
    content = (ROOT / "setup.sh").read_text(encoding="utf-8")
    assert "bash setup.sh --check" in content
    assert 'if [[ "${1:-}" == "--check" ]]; then' in content
    assert 'info "Check mode complete."' in content


def test_start_sh_documents_and_handles_check_mode():
    content = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "./start.sh --check" in content
    assert 'if [[ "${1:-}" == "--check" ]]; then' in content
    assert 'info "Check mode complete."' in content
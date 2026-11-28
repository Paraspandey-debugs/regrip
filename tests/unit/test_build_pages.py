"""Unit tests for the static GitHub Pages site builder."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_build_pages() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "build_pages.py"
    spec = importlib.util.spec_from_file_location("build_pages", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_pages_outputs_docs_assets_and_demo(tmp_path: Path) -> None:
    mod = _load_build_pages()
    out_dir = mod.build_pages(tmp_path / "site")

    index_html = (out_dir / "index.html").read_text(encoding="utf-8")
    demo_html = (out_dir / "demo.html").read_text(encoding="utf-8")

    assert (out_dir / ".nojekyll").is_file()
    assert (out_dir / "site.css").is_file()
    assert (out_dir / "assets" / "tokens.css").is_file()
    assert (out_dir / "assets" / "regrip-demo.gif").is_file()

    assert "Catch breaking MCP tool changes" in index_html
    assert 'href="demo.html"' in index_html
    assert 'href="assets/regrip-04-favicon-transparent.png"' in demo_html
    assert "window.fetch" in demo_html
    assert "commerce-mcp" in demo_html
    assert "/assets/" not in demo_html

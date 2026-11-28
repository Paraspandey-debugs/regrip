"""Build the static GitHub Pages bundle from docs/, assets/, and dashboard assets."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
from pathlib import Path
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS_DIR = _REPO_ROOT / "docs"
_ASSETS_DIR = _REPO_ROOT / "assets"
_TOKENS_CSS = _REPO_ROOT / "src" / "regrip" / "adapters" / "static" / "tokens.css"
_DASHBOARD_HTML_MOD = _REPO_ROOT / "src" / "regrip" / "adapters" / "dashboard_html.py"


def _load_dashboard_html() -> str:
    """Load `dashboard_html.load_dashboard_html()` without importing package dependencies."""
    spec = importlib.util.spec_from_file_location("_dashboard_html", _DASHBOARD_HTML_MOD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return cast(str, mod.load_dashboard_html())


_DEMO_BANNER_CSS = (
    ".demo-banner{background:#151517;border-bottom:1px solid #26262A;color:#A1A1AA;"
    "font-family:var(--font-mono);font-size:12px;padding:8px 24px;"
    "display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;}"
    ".demo-banner a{color:#FF2468;font-weight:600;}"
)

_DEMO_BANNER_HTML = (
    '<div class="demo-banner">'
    "<span>Static Preview — live dashboard backed by exported "
    "<code>commerce-mcp@v1.0.0 → mcp_v2.py</code> fixtures</span>"
    '<a href="index.html">← Back to Regrip Docs</a>'
    "</div>"
)


def _build_fetch_shim_script(fixtures: dict[str, object]) -> str:
    """Return an inline JS fetch interceptor for static GitHub Pages preview."""
    payload = json.dumps(fixtures, separators=(",", ":"))
    return (
        "<script>(() => {"
        f"const D = {payload};"
        "const ok = (body) => Promise.resolve(new Response(JSON.stringify(body), "
        "{status: 200, headers: {'Content-Type': 'application/json'}}));"
        "const origFetch = window.fetch.bind(window);"
        "window.fetch = (input, init) => {"
        "const url = typeof input === 'string' ? input : (input && input.url) || '';"
        "if (url === '/api/v1/health') return ok(D.health);"
        "if (url === '/api/v1/projects/default/baselines') return ok(D.baselines);"
        "if (url === '/api/v1/projects/default/runs') return ok(D.runs);"
        "const rep = url.match(/^\\/api\\/v1\\/projects\\/default\\/runs\\/([^/]+)\\/report$/);"
        "if (rep && D.reports[rep[1]]) return ok(D.reports[rep[1]]);"
        "const hist = url.match(/^\\/api\\/v1\\/projects\\/default\\/tools\\/([^/]+)\\/history$/);"
        "if (hist) { const k = decodeURIComponent(hist[1]); "
        "return ok(D.histories[k] || {tool_name:k,total_recorded_calls:0,error_rate:0,"
        "dependent_agents:[],timeline:[],top_parameter_combinations:[]}); }"
        "return origFetch(input, init);"
        "};"
        "})();</script>"
    )


def build_demo_html(fixtures: dict[str, object]) -> str:
    """Render standalone static demo.html reusing the live Regrip dashboard bundle."""
    html = _load_dashboard_html()
    html = html.replace(
        "/assets/regrip-04-favicon-transparent.png",
        "assets/regrip-04-favicon-transparent.png",
    )
    html = html.replace("</style>", f"\n{_DEMO_BANNER_CSS}\n</style>", 1)
    html = html.replace("<body>", f"<body>\n  {_DEMO_BANNER_HTML}", 1)
    shim = _build_fetch_shim_script(fixtures)
    return html.replace("<script>", f"{shim}\n  <script>", 1)


def build_pages(out_dir: Path) -> Path:
    """Assemble the static GitHub Pages site in out_dir."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(_DOCS_DIR / "index.html", out_dir / "index.html")
    shutil.copy2(_DOCS_DIR / "site.css", out_dir / "site.css")
    if (_DOCS_DIR / "architecture.md").is_file():
        shutil.copy2(_DOCS_DIR / "architecture.md", out_dir / "architecture.md")

    assets_out = out_dir / "assets"
    shutil.copytree(_ASSETS_DIR, assets_out, dirs_exist_ok=True)
    shutil.copy2(_TOKENS_CSS, assets_out / "tokens.css")

    fixtures_path = _DOCS_DIR / "data" / "demo_fixtures.json"
    fixtures: dict[str, object] = json.loads(fixtures_path.read_text(encoding="utf-8"))
    (out_dir / "demo.html").write_text(build_demo_html(fixtures), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Regrip static GitHub Pages bundle.")
    parser.add_argument("--out", default="_site", help="Output directory (default: _site)")
    args = parser.parse_args()
    dest = build_pages(Path(args.out))
    print(f"Built GitHub Pages site at {dest}")


if __name__ == "__main__":
    main()

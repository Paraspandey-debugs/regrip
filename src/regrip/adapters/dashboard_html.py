"""Loader for the self-contained local Web Dashboard HTML bundle."""

from __future__ import annotations

from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def load_dashboard_html() -> str:
    """Assemble self-contained dashboard HTML with inlined CSS and JS."""
    html = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
    tokens = (_STATIC_DIR / "tokens.css").read_text(encoding="utf-8")
    css = (_STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    views_js = (_STATIC_DIR / "dashboard_views.js").read_text(encoding="utf-8")
    js = (_STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    return (
        html.replace("/* __REGRIP_CSS__ */", f"{tokens}\n{css}")
        .replace("/* __REGRIP_VIEWS_JS__ */", views_js)
        .replace("/* __REGRIP_JS__ */", js)
    )


DASHBOARD_HTML = load_dashboard_html()

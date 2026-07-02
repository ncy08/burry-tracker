"""Render the Burry portfolio mirror dashboard from SQLite + the Jinja template.

Turns the JSON-serializable snapshot (``dashboard_data.read_snapshot``) and the
``burry_tracker.html.j2`` template into the written ``index.html``, atomically.

Independently runnable: ``python -m substack_trader.render_dashboard`` re-derives
the snapshot from the current ``signal_log.db`` and rewrites the dashboard. The
pipeline calls ``render(config)`` at the end of every cycle (guarded so a render
failure never crashes the cycle).

Templating choice: Jinja, because ``tojson``-based safe JSON inlining
(``{{ data | tojson | safe }}``) is cleaner and less error-prone than
manual string substitution for a large snapshot. ``jinja2`` is a
declared direct dependency (``pyproject.toml``); a bare ``Environment`` does NOT
ship the ``tojson`` filter (that is a Flask convention), so it is registered
explicitly below.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from substack_trader.config import Config
from substack_trader.dashboard_data import read_snapshot

# Resolved from this file: <repo>/substack_trader/render_dashboard.py -> parents[1] = <repo>.
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
OUTPUT = DASHBOARD_DIR / "index.html"

TEMPLATE_NAME = "burry_tracker.html.j2"


def render(config: Config) -> Path:
    """Render the dashboard to ``index.html`` and return its path.

    Atomic write: render fully to a sibling ``.tmp`` then ``os.replace`` it into
    place, so a reader never sees a half-written file. The ``.tmp`` is always
    cleaned up, even if the replace fails. Paths are read from the module globals
    at call time so tests can redirect ``TEMPLATES_DIR`` / ``DASHBOARD_DIR``.
    """
    data = read_snapshot(config)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["tojson"] = json.dumps  # REQUIRED — a bare Environment lacks tojson.
    html = env.get_template(TEMPLATE_NAME).render(data=data)

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    output = DASHBOARD_DIR / "index.html"
    tmp = output.with_name("index.html.tmp")
    try:
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, output)  # atomic on the same filesystem
    finally:
        tmp.unlink(missing_ok=True)
    return output


if __name__ == "__main__":
    print(render(Config.load()))

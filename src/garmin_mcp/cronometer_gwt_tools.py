"""
Macro-target tools for Cronometer using the GWT-RPC protocol.

This module adds ONLY the macro-target read/write tools that the stable
mobile-API library (cronometer_tools.py) cannot provide. Everything else
(food logging, diary, search, nutrition scores) stays on the mobile-API
library. Session cookies are persisted to CRONOMETER_DATA_DIR so Railway
redeploys don't force a fresh login every time.
"""

import os
import sys
from datetime import date as _date

from cronometer_mcp import CronometerClient

_client = None


def init_client():
    """Build a GWT CronometerClient from the same env vars as the mobile client.
    Points session-cookie storage at CRONOMETER_DATA_DIR so it survives
    Railway redeploys when a persistent Volume is mounted there."""
    username = os.environ.get("CRONOMETER_USERNAME")
    password = os.environ.get("CRONOMETER_PASSWORD")
    if not username or not password:
        print(
            "CRONOMETER_USERNAME/CRONOMETER_PASSWORD not set; GWT macro tools disabled.",
            file=sys.stderr,
        )
        return None

    # Tell the library where to store session cookies.
    # On Railway this points to the mounted Volume; locally it uses ~/.local/...
    data_dir = os.environ.get(
        "CRONOMETER_DATA_DIR",
        os.path.expanduser("~/.local/share/cronometer-mcp"),
    )
    os.makedirs(data_dir, exist_ok=True)
    os.environ["CRONOMETER_DATA_DIR"] = data_dir

    try:
        client = CronometerClient()
        client.authenticate()
        print("Cronometer GWT client (macro tools) initialized successfully.", file=sys.stderr)
        return client
    except Exception as e:
        print(f"Cronometer GWT login failed: {e}", file=sys.stderr)
        return None


def configure(client):
    """Receive the already-constructed GWT CronometerClient (or None)."""
    global _client
    _client = client


def _parse_day(day: str | None) -> _date:
    """Convert an optional 'YYYY-MM-DD' string into a date object."""
    return _date.fromisoformat(day) if day else _date.today()


def register_tools(app):
    if _client is None:
        return app  # GWT client unavailable -- skip silently

    # --- Reading -------------------------------------------------------

    @app.tool()
    def get_cronometer_daily_macro_targets(day: str | None = None) -> dict:
        """Get the macro targets (protein, fat, carbs, calories) for a specific
        date (YYYY-MM-DD, default today). Shows what template is applied."""
        return _client.get_daily_macro_targets(_parse_day(day))

    @app.tool()
    def get_cronometer_macro_target_templates_gwt() -> list:
        """List all saved macro target templates (name, protein, fat, carbs,
        calories, template_id). Use template_id with save_macro_schedule."""
        return _client.get_macro_target_templates()

    @app.tool()
    def get_cronometer_weekly_macro_schedule() -> list:
        """Get the full weekly macro schedule showing which template is assigned
        to each day of the week (0=Sun through 6=Sat)."""
        return _client.get_all_macro_schedules()

    # --- Writing -------------------------------------------------------

    @app.tool()
    def update_cronometer_daily_targets(
        day: str,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        calories: float,
        template_name: str = "Custom Targets",
    ) -> bool:
        """Edit macro targets for one specific date (YYYY-MM-DD) without
        affecting the weekly schedule or any templates. Use this for
        one-off adjustments when a training day changes unexpectedly.
        Returns True on success."""
        return _client.update_daily_targets(
            day=_parse_day(day),
            protein_g=protein_g,
            fat_g=fat_g,
            carbs_g=carbs_g,
            calories=calories,
            template_name=template_name,
        )

    @app.tool()
    def save_cronometer_macro_template(
        template_name: str,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        calories: float,
    ) -> int:
        """Create (or overwrite) a named macro target template such as
        'Hard Training Day' or 'Rest Day'. Returns the template_id, which
        you can pass to assign_cronometer_weekly_macro_schedule."""
        return _client.save_macro_target_template(
            template_name=template_name,
            protein_g=protein_g,
            fat_g=fat_g,
            carbs_g=carbs_g,
            calories=calories,
        )

    @app.tool()
    def assign_cronometer_weekly_macro_schedule(
        day_of_week: int,
        template_id: int,
    ) -> bool:
        """Assign a macro template to a recurring day of the week
        (0=Sunday, 1=Monday ... 6=Saturday). Use get_cronometer_weekly_macro_schedule
        to see current assignments and get_cronometer_macro_target_templates_gwt
        to find template_ids. Returns True on success."""
        return _client.save_macro_schedule(
            day_of_week_us=day_of_week,
            template_id=template_id,
        )

    @app.tool()
    def delete_cronometer_macro_template(template_id: int) -> bool:
        """Delete a saved macro target template by its template_id.
        Get template_ids from get_cronometer_macro_target_templates_gwt.
        Returns True on success."""
        return _client.delete_macro_target_template(template_id)

    return app
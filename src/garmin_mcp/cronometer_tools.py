"""
MCP tools for Cronometer nutrition data.

Wraps CronometerClient from the third-party `cronometer-api-mcp` package
(reverse-engineered from Cronometer's mobile API). Both read and write
operations are exposed -- Claude can log food, copy days, create custom
foods, delete entries, and mark days complete, in addition to reading.
"""

import os
import sys
from datetime import date as _date

from cronometer_api_mcp.client import CronometerClient

_client = None


def init_client():
    """Build a CronometerClient from CRONOMETER_USERNAME / CRONOMETER_PASSWORD.
    Returns None if credentials are missing or login fails, so the rest of
    the server (Garmin) keeps working even if Cronometer is unavailable."""
    username = os.environ.get("CRONOMETER_USERNAME")
    password = os.environ.get("CRONOMETER_PASSWORD")
    if not username or not password:
        print(
            "CRONOMETER_USERNAME/CRONOMETER_PASSWORD not set; Cronometer tools disabled.",
            file=sys.stderr,
        )
        return None
    try:
        client = CronometerClient()
        client.login()
        print("Cronometer client initialized successfully.", file=sys.stderr)
        return client
    except Exception as e:
        print(f"Cronometer login failed: {e}", file=sys.stderr)
        return None


def configure(client):
    """Receive the already-constructed CronometerClient (or None)."""
    global _client
    _client = client


def _parse_day(day: str | None):
    """Convert an optional 'YYYY-MM-DD' string into a date object."""
    return _date.fromisoformat(day) if day else None


def register_tools(app):
    if _client is None:
        return app  # Cronometer unavailable -- skip silently

    # --- Reading -------------------------------------------------------

    @app.tool()
    def get_cronometer_diary(day: str | None = None) -> dict:
        """Get Cronometer food diary entries for a date (YYYY-MM-DD, default today)."""
        return _client.get_diary(_parse_day(day))

    @app.tool()
    def get_cronometer_daily_nutrients(day: str | None = None) -> dict:
        """Get total nutrients consumed on a date (YYYY-MM-DD, default today)."""
        return _client.get_consumed_nutrients(_parse_day(day))

    @app.tool()
    def get_cronometer_nutrient_targets(day: str | None = None) -> dict:
        """Get nutrient targets vs. logged amounts for a date."""
        return _client.get_nutrients(_parse_day(day))

    @app.tool()
    def get_cronometer_nutrition_scores(day: str | None = None, include_supplements: bool = True) -> dict:
        """Get nutrition category scores (vitamins, minerals, etc.) for a date."""
        return _client.get_nutrition_scores(_parse_day(day), include_supplements=include_supplements)

    @app.tool()
    def search_cronometer_foods(query: str) -> list:
        """Search the Cronometer food database by name."""
        return _client.search_food(query)

    @app.tool()
    def get_cronometer_food_details(food_id: int) -> dict:
        """Get the full nutrition profile for a specific Cronometer food id."""
        return _client.get_food(food_id)

    @app.tool()
    def get_cronometer_macro_schedules() -> dict:
        """Get the weekly macro target schedule."""
        return _client.get_macro_schedules()

    @app.tool()
    def get_cronometer_macro_target_templates() -> dict:
        """Get saved macro target templates."""
        return _client.get_macro_target_templates()

    @app.tool()
    def get_cronometer_fasting_stats() -> dict:
        """Get aggregate fasting statistics."""
        return _client.get_fasting_stats()

    @app.tool()
    def get_cronometer_fasting_history(start: str | None = None, end: str | None = None) -> dict:
        """Get fasting history within a date range (YYYY-MM-DD)."""
        return _client.get_fasting_with_date_range(_parse_day(start), _parse_day(end))

    # --- Writing ---------------------------------------------------------

    @app.tool()
    def add_cronometer_serving(
        food_id: int,
        grams: float,
        measure_id: int | None = None,
        translation_id: int = 0,
        day: str | None = None,
        diary_group: int = 0,
    ) -> dict:
        """Log a food serving to the Cronometer diary (use search/food-details to find food_id)."""
        return _client.add_serving(
            food_id=food_id,
            measure_id=measure_id,
            grams=grams,
            translation_id=translation_id,
            day=_parse_day(day),
            diary_group=diary_group,
        )

    @app.tool()
    def copy_cronometer_day(from_day: str | None = None, to_day: str | None = None) -> dict:
        """Copy all diary entries from one day to another (defaults: yesterday -> today)."""
        return _client.copy_day(_parse_day(from_day), _parse_day(to_day))

    @app.tool()
    def create_cronometer_custom_food(
        name: str,
        calories: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        fiber_g: float = 0,
        sugar_g: float = 0,
        sodium_mg: float = 0,
        saturated_fat_g: float = 0,
        serving_name: str = "1 serving",
        serving_grams: float = 100.0,
    ) -> dict:
        """Create a custom food with specified nutrition values."""
        return _client.create_custom_food(
            name,
            calories=calories,
            protein_g=protein_g,
            fat_g=fat_g,
            carbs_g=carbs_g,
            fiber_g=fiber_g,
            sugar_g=sugar_g,
            sodium_mg=sodium_mg,
            saturated_fat_g=saturated_fat_g,
            serving_name=serving_name,
            serving_grams=serving_grams,
        )

    @app.tool()
    def delete_cronometer_entries(entry_ids: list[str], day: str | None = None) -> dict:
        """Delete one or more diary entries by id. Not reversible -- confirm with the user first."""
        return _client.delete_entries(entry_ids, _parse_day(day))

    @app.tool()
    def mark_cronometer_day_complete(day: str | None = None, complete: bool = True) -> dict:
        """Mark a diary day as complete or incomplete."""
        return _client.mark_day_complete(_parse_day(day), complete)

    return app
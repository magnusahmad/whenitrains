import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from whenitrains.cli import _discover_market
from whenitrains.storage import connect, migrate


class CliDiscoveryTests(unittest.TestCase):
    def test_discover_market_fetches_highest_and_lowest_temperature_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.db")
            migrate(db)
            requested_slugs = []

            def fake_fetch(slug):
                requested_slugs.append(slug)
                if slug not in {
                    "highest-temperature-in-hong-kong-on-may-7-2026",
                    "lowest-temperature-in-hong-kong-on-may-7-2026",
                }:
                    return None
                return {
                    "id": f"event-{slug}",
                    "slug": slug,
                    "title": slug,
                    "eventDate": "2026-05-07",
                    "markets": [
                        {
                            "id": f"market-{slug}",
                            "question": slug,
                            "groupItemTitle": "25°C",
                            "clobTokenIds": '["YES_TOKEN", "NO_TOKEN"]',
                        }
                    ],
                }

            with (
                patch("whenitrains.cli.fetch_hk_temperature_event", side_effect=fake_fetch),
                patch("whenitrains.cli.resolution_rules_warning", return_value=None),
            ):
                discovered = _discover_market(db, date(2026, 5, 7))

            self.assertTrue(discovered)
            self.assertEqual(
                requested_slugs,
                [
                    "highest-temperature-in-hong-kong-on-may-7-2026",
                    "lowest-temperature-in-hong-kong-on-may-7-2026",
                ],
            )
            slugs = [
                row["slug"]
                for row in db.execute("select slug from markets order by slug")
            ]
            self.assertEqual(
                slugs,
                [
                    "highest-temperature-in-hong-kong-on-may-7-2026",
                    "lowest-temperature-in-hong-kong-on-may-7-2026",
                ],
            )


if __name__ == "__main__":
    unittest.main()

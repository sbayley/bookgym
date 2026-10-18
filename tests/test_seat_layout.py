from __future__ import annotations

import json
from datetime import date
import unittest

from app.models import DiscoveredClass
from app.seat_layout import _normalize_booking_layout, discovered_class_seat_metadata


class SeatLayoutMetadataTests(unittest.TestCase):
    def test_discovered_class_seat_metadata_falls_back_to_api_payload(self) -> None:
        discovered_class = DiscoveredClass(
            source_key="calendar_event:6588690",
            calendar_event_id=6588690,
            title="Steppin' Up",
            class_date=date(2026, 7, 4),
            api_payload=json.dumps(
                {
                    "LayoutId": 1521,
                    "CanShowLayout": True,
                    "EquipmentName": "Spot",
                }
            ),
        )

        metadata = discovered_class_seat_metadata(discovered_class)

        self.assertEqual(1521, metadata["layout_id"])
        self.assertTrue(metadata["can_show_layout"])
        self.assertEqual("Spot", metadata["seat_title"])
        self.assertTrue(metadata["has_seat_map"])

    def test_normalize_booking_layout_classifies_cells_and_preferred_seats(self) -> None:
        normalized = _normalize_booking_layout(
            body={
                "rows": 2,
                "columns": 3,
                "data": [
                    {
                        "Row": 1,
                        "Column": 1,
                        "SeatId": "A_1",
                        "Title": "33",
                        "ImageUrlOfBooked": "https://example.com/dumbell.png",
                        "CanBook": True,
                        "IsBooked": False,
                        "IsUnavailable": False,
                        "MaxGuests": 1,
                    },
                    {
                        "Row": 1,
                        "Column": 2,
                        "SeatId": "A_2",
                        "Title": "31",
                        "ImageUrlOfBooked": "https://example.com/dumbell.png",
                        "CanBook": True,
                        "IsBooked": True,
                        "IsUnavailable": False,
                        "MaxGuests": 1,
                    },
                    {
                        "Row": 1,
                        "Column": 3,
                        "SeatId": "A_3",
                        "Title": "30",
                        "ImageUrlOfBooked": "https://example.com/dumbell.png",
                        "CanBook": True,
                        "IsBooked": False,
                        "IsUnavailable": True,
                        "MaxGuests": 1,
                    },
                    {
                        "Row": 2,
                        "Column": 1,
                        "SeatId": "B_1",
                        "Title": "B1",
                        "ImageUrlOfBooked": "https://example.com/door.png",
                        "CanBook": False,
                        "IsBooked": False,
                        "IsUnavailable": False,
                        "MaxGuests": 1,
                    },
                    {
                        "Row": 2,
                        "Column": 2,
                        "SeatId": "B_2",
                        "Title": "T",
                        "ImageUrlOfBooked": "https://example.com/trainer.png",
                        "CanBook": False,
                        "IsBooked": False,
                        "IsUnavailable": False,
                        "MaxGuests": 1,
                    },
                    {
                        "Row": 2,
                        "Column": 3,
                        "SeatId": "B_3",
                        "Title": "B3",
                        "ImageUrlOfBooked": None,
                        "CanBook": False,
                        "IsBooked": False,
                        "IsUnavailable": False,
                        "MaxGuests": 1,
                    },
                ],
            },
        )

        self.assertEqual(2, normalized["rows"])
        self.assertEqual(3, normalized["columns"])
        self.assertEqual(
            {
                "available": 1,
                "booked": 1,
                "unavailable": 1,
                "non_bookable": 3,
            },
            normalized["summary"],
        )
        self.assertEqual("available", normalized["cells"][0]["kind"])
        self.assertEqual("booked", normalized["cells"][1]["kind"])
        self.assertEqual("unavailable", normalized["cells"][2]["kind"])
        self.assertEqual("door", normalized["cells"][3]["kind"])
        self.assertEqual("trainer", normalized["cells"][4]["kind"])
        self.assertEqual("walkway", normalized["cells"][5]["kind"])


if __name__ == "__main__":
    unittest.main()

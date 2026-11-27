from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from destinations.models import Destination, Hotel, Place
from planner.services import generate_optimal_plan
from planner.travel_stay import day_windows, mode_estimates
from trips.models import Accommodation, TransportOption, Trip

DELHI = {"latitude": 28.61, "longitude": 77.21}


def aware(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


@patch("planner.travel_stay.get_coordinates", return_value=DELHI)
class TravelStayTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="ts", password="TestPassword123"
        )
        self.client.force_authenticate(self.user)
        self.dest = Destination.objects.create(name="Goa", state="Goa")
        for i in range(1, 25):
            Place.objects.create(
                destination=self.dest, name=f"Spot {i}", category="attraction",
                latitude=Decimal(f"15.{500 + i}"), longitude=Decimal("73.80"),
                time_required=60, estimated_cost=Decimal("0"),
            )
        Hotel.objects.create(
            destination=self.dest, name="Sea View Hotel", category="hotel",
            latitude=Decimal("15.51"), longitude=Decimal("73.80"),
        )
        self.trip = Trip.objects.create(
            user=self.user, source="Delhi", destination="Goa",
            start_date=date(2026, 12, 1), end_date=date(2026, 12, 4),
            number_of_people=2, estimated_budget=Decimal("100000"),
        )

    def test_generate_suggests_travel_both_ways_and_stays(self, _geo):
        result = generate_optimal_plan(self.trip)
        outbound = result["transport_options"]["outbound"]
        back = result["transport_options"]["return"]
        self.assertTrue(2 <= len(outbound) <= 3)
        self.assertTrue(2 <= len(back) <= 3)
        self.assertTrue(all(o["is_estimate"] for o in outbound))
        self.assertEqual(sum(o["in_plan"] for o in outbound), 1)
        self.assertIsNotNone(result["return_transport"])
        self.assertEqual(len(result["accommodation_options"]), 3)
        self.assertIsNotNone(result["accommodation"])
        self.assertNotIn("return transport", result["estimated_cost"]["unknown_components"])
        # Estimates are kept, not duplicated, on regenerate.
        generate_optimal_plan(self.trip)
        self.assertEqual(TransportOption.objects.filter(trip=self.trip).count(), len(outbound) + len(back))
        self.assertEqual(Accommodation.objects.filter(trip=self.trip).count(), 3)

    def test_user_selection_wins(self, _geo):
        generate_optimal_plan(self.trip)
        mine = TransportOption.objects.create(
            trip=self.trip, transport_type="train", direction="outbound",
            origin="Delhi", destination="Goa", provider="My train",
            departure_time=aware(date(2026, 11, 30), 6),
            arrival_time=aware(date(2026, 12, 1), 14),
            estimated_cost=Decimal("9999"),
        )
        response = self.client.patch(
            f"/api/transport-options/{mine.id}/", {"is_selected": True}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        result = generate_optimal_plan(self.trip)
        self.assertEqual(result["transport"]["provider"], "My train")
        # Day 1 starts after arrival (14:00) plus the buffer.
        day_one = [i for i in result["itinerary"] if i["day"] == 1]
        if day_one:
            self.assertGreaterEqual(day_one[0]["start_time"], "15:00")
        # Selecting another option unselects mine (one per direction).
        other = TransportOption.objects.filter(
            trip=self.trip, direction="outbound", is_estimate=True
        ).first()
        self.client.patch(
            f"/api/transport-options/{other.id}/", {"is_selected": True}, format="json"
        )
        mine.refresh_from_db()
        self.assertFalse(mine.is_selected)

    def test_editing_estimate_quote_makes_it_users(self, _geo):
        generate_optimal_plan(self.trip)
        stay = Accommodation.objects.filter(trip=self.trip).first()
        self.client.patch(
            f"/api/accommodations/{stay.id}/", {"price_per_night": "2100.00"}, format="json"
        )
        stay.refresh_from_db()
        self.assertFalse(stay.is_estimate)

    def test_latest_plan_is_stale_after_date_edit(self, _geo):
        self.client.post(f"/api/planner/trips/{self.trip.id}/generate/", {}, format="json")
        latest = self.client.get(f"/api/planner/trips/{self.trip.id}/latest/")
        self.assertEqual(latest.status_code, 200)
        self.assertFalse(latest.data["stale"])
        self.client.patch(
            f"/api/trips/{self.trip.id}/", {"end_date": "2026-12-06"}, format="json"
        )
        latest = self.client.get(f"/api/planner/trips/{self.trip.id}/latest/")
        self.assertTrue(latest.data["stale"])
        # Regenerating covers the new days and replaces date-bound estimates.
        result = generate_optimal_plan(Trip.objects.get(id=self.trip.id))
        self.assertEqual(len(result["daily_schedule"]), 6)
        back = result["return_transport"]
        self.assertTrue(back["departure_time"].startswith("2026-12-06"))

    def test_day_windows_follow_arrival_and_departure(self, _geo):
        outbound = {
            "transport_type": "flight",
            "departure_time": aware(date(2026, 12, 1), 7),
            "arrival_time": aware(date(2026, 12, 1), 20),
        }
        back = {
            "transport_type": "flight",
            "departure_time": aware(date(2026, 12, 4), 11),
            "arrival_time": aware(date(2026, 12, 4), 13),
        }
        windows = day_windows(self.trip, outbound, back)
        self.assertIsNone(windows[0])   # lands at night
        self.assertIsNotNone(windows[1])
        self.assertIsNone(windows[3])   # must leave for the airport at 08:00

    def test_local_trip_gets_no_intercity_travel(self, geo):
        geo.return_value = {"latitude": 15.52, "longitude": 73.81}
        result = generate_optimal_plan(self.trip)
        self.assertEqual(result["transport_options"]["outbound"], [])

    def test_long_self_drive_still_gets_a_car_option(self, _geo):
        [car] = mode_estimates(850, 2, "self_vehicle")
        self.assertEqual(car["mode"], "car")
        self.assertIn("overnight halt", car["label"])
        self.assertGreater(car["minutes"], 22 * 60)
        self.assertEqual(mode_estimates(3000, 2, "self_vehicle"), [])

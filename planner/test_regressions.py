"""Regression tests for the bugs fixed in the 2026-11-28 bug sweep."""
from datetime import date, time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APITestCase

from config.throttles import AuthThrottle
from destinations.models import Destination, Hotel, Place
from integrations.geocoding import canonical_city_name, same_city
from integrations.save_places import _save_place
from planner.models import ItineraryItem, Plan
from planner.services import (
    build_activity_suggestions,
    build_daily_timeline,
    calculate_attraction_cost,
)
from trips.models import Trip


def ns_place(place_id, name, cost="0", category="attraction", lat="15.50"):
    return SimpleNamespace(
        id=place_id,
        name=name,
        category=category,
        time_required=60,
        latitude=Decimal(lat),
        longitude=Decimal("73.80"),
        estimated_cost=Decimal(cost),
    )


def ns_trip(days=1, people=1, budget="1000"):
    return SimpleNamespace(
        start_date=date(2026, 12, 1),
        end_date=date(2026, 12, days),
        number_of_people=people,
        estimated_budget=Decimal(budget),
    )


class PlannerServiceRegressionTests(SimpleTestCase):
    def test_suggestions_spend_the_budget_once(self):
        trip = ns_trip(days=2, budget="100")
        places = [ns_place(1, "Fort", "80"), ns_place(2, "Museum", "80")]
        suggestions = build_activity_suggestions(places, [], trip, "100")
        flags = sorted(s["would_exceed_budget"] for s in suggestions)
        self.assertEqual(flags, [False, True])

    def test_attraction_cost_is_per_traveller(self):
        itinerary = [{"place": ns_place(1, "Fort", "50")}]
        self.assertEqual(
            calculate_attraction_cost(itinerary, 3), Decimal("150")
        )

    def test_timeline_ignores_activity_without_coordinates(self):
        itinerary = [{
            "id": 1, "day": 1, "place": "Somewhere", "category": "attraction",
            "start_time": "09:00", "end_time": "10:00",
            "latitude": None, "longitude": None,
        }]
        dining = [{"id": 9, "name": "Cafe", "category": "cafe",
                   "latitude": "15.5", "longitude": "73.8"}]
        schedule = build_daily_timeline(itinerary, dining, ns_trip())
        self.assertEqual(len(schedule), 1)

    def test_timeline_has_no_free_time_past_closing(self):
        itinerary = [{
            "id": 1, "day": 1, "place": "Night market",
            "category": "attraction", "start_time": "21:30",
            "end_time": "22:30", "latitude": "15.5", "longitude": "73.8",
        }]
        events = build_daily_timeline(itinerary, [], ns_trip())[0]["events"]
        for event in events:
            if event["type"] == "free_time":
                self.assertLessEqual(event["end_time"], "21:00")
        starts = [e["start_time"] for e in events]
        self.assertEqual(starts, sorted(starts))


class CityAliasTests(SimpleTestCase):
    def test_alias_matches_canonical_name(self):
        self.assertTrue(same_city("Bangalore", "bengaluru"))
        self.assertEqual(canonical_city_name("bangalore"), "Bengaluru")
        self.assertFalse(same_city("Goa", "Mumbai"))


class SavePlaceRegressionTests(TestCase):
    def test_same_numeric_id_with_other_type_does_not_overwrite(self):
        goa = Destination.objects.create(name="Goa", state="Goa")
        pune = Destination.objects.create(name="Pune", state="Maharashtra")
        node_place = Place.objects.create(
            destination=goa, name="Beach", category="attraction",
            latitude=Decimal("15.5"), longitude=Decimal("73.8"),
            osm_id="123", estimated_cost=Decimal("40.00"), time_required=90,
        )
        way_place, created = _save_place(pune, {
            "osm_id": 123, "osm_type": "way", "name": "Fort",
            "category": "attraction", "latitude": 18.5, "longitude": 73.9,
        })
        self.assertTrue(created)
        self.assertEqual(way_place.osm_id, "way/123")
        node_place.refresh_from_db()
        self.assertEqual(node_place.destination, goa)
        self.assertEqual(node_place.name, "Beach")

    def test_update_keeps_curated_cost_and_duration(self):
        goa = Destination.objects.create(name="Goa", state="Goa")
        place = Place.objects.create(
            destination=goa, name="Beach", category="attraction",
            latitude=Decimal("15.5"), longitude=Decimal("73.8"),
            osm_id="77", estimated_cost=Decimal("40.00"), time_required=90,
        )
        _save_place(goa, {
            "osm_id": 77, "osm_type": "node", "name": "Beach",
            "category": "viewpoint", "latitude": 15.6, "longitude": 73.8,
        })
        place.refresh_from_db()
        self.assertEqual(place.category, "viewpoint")
        self.assertEqual(place.estimated_cost, Decimal("40.00"))
        self.assertEqual(place.time_required, 90)


class ItineraryEditRegressionTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="regress", password="TestPassword123"
        )
        self.destination = Destination.objects.create(name="Goa", state="Goa")
        self.places = [
            Place.objects.create(
                destination=self.destination, name=f"Spot {i}",
                category="attraction", latitude=Decimal("15.50") + i,
                longitude=Decimal("73.80"), time_required=60,
                estimated_cost=Decimal("100.00"),
            )
            for i in range(3)
        ]
        self.trip = Trip.objects.create(
            user=self.user, source="Pune", destination="Goa",
            start_date=date(2026, 12, 1), end_date=date(2026, 12, 2),
            number_of_people=2, estimated_budget=Decimal("5000.00"),
        )
        self.plan = Plan.objects.create(
            trip=self.trip,
            response_data={
                "trip": {"budget": "1.00"},
                "estimated_cost": {"unknown_components": []},
                "dining_recommendations": [],
            },
        )
        self.items = [
            ItineraryItem.objects.create(
                plan=self.plan, day=1, place=self.places[i],
                start_time=time(9 + 2 * i, 0), end_time=time(10 + 2 * i, 0),
                order=i + 1,
            )
            for i in range(2)
        ]
        self.base = f"/api/planner/trips/{self.trip.id}"
        self.client.force_authenticate(user=self.user)

    def test_reorder_reflows_times_without_overlap(self):
        first, second = self.items
        response = self.client.patch(
            f"{self.base}/itinerary/reorder/",
            {"day": 1, "item_ids": [second.id, first.id]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        second.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(second.start_time, time(9, 0))
        self.assertGreater(first.start_time, second.end_time)
        itinerary = response.data["plan"]["itinerary"]
        self.assertEqual(itinerary[0]["travel_minutes_before"], 0)
        self.assertGreater(itinerary[1]["travel_minutes_before"], 0)

    def test_move_without_times_does_not_overlap_target_day(self):
        other = ItineraryItem.objects.create(
            plan=self.plan, day=2, place=self.places[2],
            start_time=time(9, 0), end_time=time(10, 0), order=1,
        )
        moved = self.items[0]
        response = self.client.patch(
            f"{self.base}/itinerary/items/{moved.id}/move/",
            {"day": 2, "order": 2},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        moved.refresh_from_db()
        other.refresh_from_db()
        self.assertGreater(moved.start_time, other.end_time)

    def test_add_rejects_activity_past_midnight(self):
        response = self.client.post(
            f"{self.base}/itinerary/items/",
            {"day": 1, "place_id": self.places[2].id, "start_time": "23:30"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_add_rejects_bad_bodies(self):
        url = f"{self.base}/itinerary/items/"
        self.assertEqual(
            self.client.post(url, [1, 2], format="json").status_code, 400
        )
        self.assertEqual(
            self.client.post(
                url, {"day": 1, "place_id": "abc"}, format="json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.patch(
                f"{self.base}/itinerary/reorder/", "x", format="json"
            ).status_code,
            400,
        )

    def test_refresh_uses_live_budget_and_per_person_cost(self):
        response = self.client.patch(
            f"{self.base}/itinerary/reorder/",
            {"day": 1, "item_ids": [i.id for i in self.items]},
            format="json",
        )
        cost = response.data["plan"]["estimated_cost"]
        # 2 places x 100 x 2 travellers, against the live 5000 budget.
        self.assertEqual(Decimal(cost["attractions"]), Decimal("400.00"))
        self.assertEqual(
            Decimal(cost["remaining_known_budget"]), Decimal("4600.00")
        )


class TripValidationRegressionTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="tripcheck", password="TestPassword123"
        )
        self.client.force_authenticate(user=self.user)

    def post_trip(self, **overrides):
        data = {
            "source": "Pune", "destination": "Goa",
            "start_date": "2026-12-01", "end_date": "2026-12-03",
            "number_of_people": 2, "estimated_budget": "5000.00",
        }
        data.update(overrides)
        return self.client.post("/api/trips/", data, format="json")

    def test_valid_trip_is_created(self):
        self.assertEqual(self.post_trip().status_code, 201)

    def test_invalid_trips_are_rejected(self):
        for overrides in (
            {"end_date": "2026-11-30"},
            {"end_date": "2027-03-01"},
            {"number_of_people": 0},
            {"estimated_budget": "-1"},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.post_trip(**overrides).status_code, 400)


class AccommodationRegressionTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="hotelcheck", password="TestPassword123"
        )
        self.client.force_authenticate(user=self.user)
        destination = Destination.objects.create(
            name="Bengaluru", state="Karnataka"
        )
        self.hotel = Hotel.objects.create(
            destination=destination, name="Garden Inn", osm_id="5",
            osm_type="node", category="hotel",
            latitude=Decimal("12.97"), longitude=Decimal("77.59"),
            website="not a url",
        )
        self.trip = Trip.objects.create(
            user=self.user, source="Pune", destination="Bangalore",
            start_date=date(2026, 12, 1), end_date=date(2026, 12, 3),
            number_of_people=1, estimated_budget=Decimal("5000.00"),
        )

    def test_alias_city_hotel_is_accepted_and_bad_website_skipped(self):
        response = self.client.post(
            "/api/accommodations/",
            {"trip": self.trip.id, "hotel": self.hotel.id,
             "price_per_night": "1500.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["booking_url"], "")


class InputAndThrottleRegressionTests(APITestCase):
    def test_search_destination_rejects_non_text(self):
        response = self.client.post(
            "/api/destinations/search/",
            {"city": 5, "state": ["Goa"]},
            format="json",
        )
        self.assertIn(response.status_code, (400, 404))
        self.assertNotEqual(response.status_code, 500)

    def test_login_is_rate_limited(self):
        cache.clear()
        with patch.object(AuthThrottle, "THROTTLE_RATES", {"auth": "2/min"}):
            codes = [
                self.client.post(
                    "/api/token/",
                    {"username": "nobody", "password": "wrong"},
                    format="json",
                ).status_code
                for _ in range(3)
            ]
        cache.clear()
        self.assertEqual(codes[-1], 429)


CF = dict(
    CLOUDFLARE_ACCOUNT_ID="a", CLOUDFLARE_API_TOKEN="t",
    CLOUDFLARE_AI_MODEL="@cf/test",
)


class GenerateTimeBudgetTests(SimpleTestCase):
    """A slow place lookup must never push generate past gunicorn's 60s."""

    def trip_and_places(self):
        trip = SimpleNamespace(
            start_date=date(2026, 12, 1), end_date=date(2026, 12, 2),
            number_of_people=1, estimated_budget=Decimal("1000"),
            source="Delhi", destination="Uttaluru", travel_mode="any",
        )
        place = ns_place(1, "Temple")
        place.description = ""
        return trip, [place]

    def test_ai_is_skipped_when_little_time_is_left(self):
        from planner.ai import build_ai_schedule
        trip, places = self.trip_and_places()
        with self.settings(**CF), patch("planner.ai.requests.post") as post:
            schedule, provider, warning = build_ai_schedule(
                trip, places, time_budget=4
            )
        post.assert_not_called()
        self.assertIsNone(schedule)
        self.assertIn("AI was skipped", warning)

    def test_ai_read_timeout_is_capped_to_the_budget(self):
        import requests
        from planner.ai import build_ai_schedule
        trip, places = self.trip_and_places()
        with self.settings(**CF), patch(
            "planner.ai.requests.post", side_effect=requests.Timeout
        ) as post:
            build_ai_schedule(trip, places, time_budget=20)
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 15))


class OverpassCooldownTests(SimpleTestCase):
    def setUp(self):
        import integrations.places as places
        self.places = places
        places._overpass_down_until = 0.0
        self.addCleanup(setattr, places, "_overpass_down_until", 0.0)

    def test_second_lookup_skips_overpass_after_outage(self):
        import requests
        p = self.places
        with self.settings(GEOAPIFY_API_KEY="k"), patch.object(
            p, "_get_places_from_overpass", side_effect=requests.Timeout
        ) as overpass, patch.object(
            p, "get_places_from_geoapify", return_value=[]
        ) as geo:
            p.get_places_from_osm(15.0, 80.0)
            p.get_places_from_osm(15.0, 80.0, radius=25000)
        self.assertEqual(overpass.call_count, 1)
        self.assertEqual(geo.call_count, 2)


class FullDayPlanningTests(TestCase):
    """A thin city must give full days, not 1-2 stops on every day."""

    def test_few_places_fill_days_instead_of_spreading(self):
        from planner.services import build_daily_schedule
        places = [
            ns_place(i, f"Spot {i}", lat=f"15.{500 + i}") for i in range(1, 13)
        ]
        itinerary, unscheduled = build_daily_schedule(places, ns_trip(days=10))
        per_day = {}
        for item in itinerary:
            per_day[item["day"]] = per_day.get(item["day"], 0) + 1
        self.assertEqual(unscheduled, [])
        self.assertEqual(len(per_day), 3)          # 720 min / 330 per day
        self.assertGreaterEqual(min(per_day.values()), 4)

    def test_many_places_fill_every_day(self):
        from planner.services import build_daily_schedule, select_sightseeing_pool
        places = [
            ns_place(i, f"Spot {i}", lat=f"15.{500 + i}") for i in range(1, 200)
        ]
        pool = select_sightseeing_pool(places, 5)
        self.assertEqual(len(pool), 35)
        itinerary, _ = build_daily_schedule(pool, ns_trip(days=5))
        per_day = {}
        for item in itinerary:
            per_day[item["day"]] = per_day.get(item["day"], 0) + 1
        self.assertEqual(len(per_day), 5)
        self.assertGreaterEqual(min(per_day.values()), 5)
        last_end = max(i["end_time"] for i in itinerary if i["day"] == 1)
        self.assertGreaterEqual(last_end, time(15, 0))

    def test_pool_prefers_higher_priority(self):
        from planner.services import select_sightseeing_pool
        places = [ns_place(i, f"Park {i}", category="park") for i in range(1, 20)]
        places.append(ns_place(99, "Fort", category="historic"))
        pool = select_sightseeing_pool(places, 1)
        self.assertIn(99, [p.id for p in pool])

    def test_sparse_ai_assignment_is_replaced(self):
        from planner.services import generate_optimal_plan
        dest = Destination.objects.create(name="Testpur", state="Goa")
        places = [
            Place.objects.create(
                destination=dest, name=f"Spot {i}", category="attraction",
                latitude=Decimal(f"15.{500 + i}"), longitude=Decimal("73.80"),
                time_required=60, estimated_cost=Decimal("0"),
            )
            for i in range(1, 13)
        ]
        user = get_user_model().objects.create_user("fd", password="pw-12345678")
        trip = Trip.objects.create(
            user=user, source="Delhi", destination="Testpur",
            start_date=date(2026, 12, 1), end_date=date(2026, 12, 6),
            number_of_people=1, estimated_budget=Decimal("50000"),
        )
        # AI spreads 2 per day over 6 days; the packed plan uses 3 days.
        ai_days = {d: [places[2 * d - 2].id, places[2 * d - 1].id] for d in range(1, 7)}
        with patch(
            "planner.services.build_ai_schedule",
            return_value=(ai_days, "fake-ai", None),
        ):
            result = generate_optimal_plan(trip, use_ai=True)
        self.assertEqual(result["planner"]["mode"], "rules")
        days = {item["day"] for item in result["itinerary"]}
        self.assertEqual(len(days), 3)
        self.assertTrue(any("left free" in w for w in result["warnings"]))


class PlaceTopUpTests(TestCase):
    def setUp(self):
        cache.clear()
        self.dest = Destination.objects.create(name="Smallcity", state="Goa")
        Place.objects.create(
            destination=self.dest, name="Old Fort", category="attraction",
            latitude=Decimal("15.5"), longitude=Decimal("73.8"),
            time_required=60, estimated_cost=Decimal("0"), osm_id="1",
        )

    def fetched(self, n):
        return [
            {"osm_id": 1000 + i, "osm_type": "node", "name": f"Museum {i}",
             "category": "museum", "latitude": 15.5 + i / 1000,
             "longitude": 73.8, "description": ""}
            for i in range(n)
        ]

    def test_thin_city_is_topped_up_once(self):
        from integrations.save_places import ensure_destination_places
        with patch(
            "integrations.save_places.get_places_from_osm",
            return_value=self.fetched(30),
        ) as osm:
            result = ensure_destination_places("Smallcity", min_sightseeing=20)
            ensure_destination_places("Smallcity", min_sightseeing=20)
        self.assertEqual(result["places_added"], 30)
        self.assertEqual(osm.call_count, 1)          # second call: throttled
        self.assertFalse(osm.call_args.kwargs["include_dining"])
        museum = Place.objects.get(name="Museum 0")
        self.assertEqual(museum.time_required, 90)

    def test_top_up_failure_keeps_existing_places(self):
        import requests
        from integrations.save_places import ensure_destination_places
        with patch(
            "integrations.save_places.get_places_from_osm",
            side_effect=requests.Timeout(),
        ):
            result = ensure_destination_places("Smallcity", min_sightseeing=20)
        self.assertEqual(result["places_added"], 0)

    def test_enough_places_skips_top_up(self):
        from integrations.save_places import ensure_destination_places
        with patch("integrations.save_places.get_places_from_osm") as osm:
            ensure_destination_places("Smallcity", min_sightseeing=1)
        osm.assert_not_called()


class BroaderPlaceQueryTests(SimpleTestCase):
    def test_overpass_query_covers_museums_and_skips_dining(self):
        from integrations import places as p
        with patch.object(p, "fetch_overpass_data", return_value={"elements": [
            {"type": "node", "id": 5, "lat": 1, "lon": 2,
             "tags": {"name": "Fort", "historic": "fort"}},
        ]}) as fetch:
            result = p._get_places_from_overpass(1, 2, 5000, 10, 1, include_dining=False)
        query = fetch.call_args.args[0]
        self.assertIn("museum", query)
        self.assertIn("historic", query)
        self.assertNotIn("restaurant", query)
        self.assertEqual(result[0]["category"], "historic")

    def test_geoapify_sights_and_dining_are_separate_requests(self):
        from integrations import places as p
        response = SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"features": []}
        )
        with patch.object(p.requests, "get", return_value=response) as get, \
                self.settings(GEOAPIFY_API_KEY="k"):
            p.get_places_from_geoapify(1, 2)
        cats = [c.kwargs["params"]["categories"] for c in get.call_args_list]
        self.assertEqual(len(cats), 2)
        self.assertNotIn("catering", cats[0])
        self.assertIn("catering", cats[1])

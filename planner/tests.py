import json
from datetime import date, time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIRequestFactory, APITestCase, force_authenticate

from planner.ai import (
    AIResponseError,
    build_ai_schedule,
    validate_ai_schedule,
)
from destinations.models import Destination, Place
from integrations.save_places import (
    DestinationNotFoundError,
    ensure_destination_places,
)
from planner.models import ItineraryItem, Plan
from planner.services import (
    build_daily_schedule,
    build_activity_suggestions,
    choose_optimal_combination,
    nearest_sightseeing_distance,
    rank_accommodation_candidates,
    rank_transport_candidates,
    generate_optimal_plan,
)
from planner.views import generate_plan
from trips.models import Trip


def make_trip():
    return SimpleNamespace(
        source="Delhi",
        destination="Jaipur",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 2),
        number_of_people=2,
        estimated_budget=Decimal("10000"),
        travel_mode="any",
    )


def make_place(place_id, name, latitude):
    return SimpleNamespace(
        id=place_id,
        name=name,
        category="attraction",
        time_required=60,
        latitude=Decimal(latitude),
        longitude=Decimal("75.8000"),
        description="",
        estimated_cost=Decimal("0.00"),
    )


class AIScheduleValidationTests(SimpleTestCase):
    def test_accepts_complete_saved_place_assignments(self):
        response = {
            "days": [
                {"day": 1, "place_ids": ["12"]},
                {"day": 2, "place_ids": [34]},
            ],
        }

        self.assertEqual(
            validate_ai_schedule(response, [12, 34], 2),
            {1: [12], 2: [34]},
        )

    def test_rejects_unknown_duplicate_missing_and_out_of_range_ids(self):
        invalid_responses = [
            {"days": [{"day": 1, "place_ids": [99]}, {"day": 2, "place_ids": [34]}]},
            {"days": [{"day": 1, "place_ids": [12, 12]}, {"day": 2, "place_ids": [34]}]},
            {"days": [{"day": 1, "place_ids": [12]}]},
            {"days": [{"day": 1, "place_ids": [12]}, {"day": 3, "place_ids": [34]}]},
        ]

        for response in invalid_responses:
            with self.subTest(response=response), self.assertRaises(AIResponseError):
                validate_ai_schedule(response, [12, 34], 2)

    def test_rejects_fractional_day_and_place_ids(self):
        for response in (
            {"days": [{"day": 1.5, "place_ids": [12]}, {"day": 2, "place_ids": [34]}]},
            {"days": [{"day": 1, "place_ids": [12.5]}, {"day": 2, "place_ids": [34]}]},
        ):
            with self.subTest(response=response), self.assertRaises(AIResponseError):
                validate_ai_schedule(response, [12, 34], 2)

    @patch("planner.ai.requests.post")
    def test_missing_either_credential_falls_back_without_provider_call(self, post):
        incomplete_credentials = (
            ("", "test-token"),
            ("test-account", ""),
        )
        for account_id, api_token in incomplete_credentials:
            with self.subTest(account_id=account_id, api_token=bool(api_token)):
                with override_settings(
                    CLOUDFLARE_ACCOUNT_ID=account_id,
                    CLOUDFLARE_API_TOKEN=api_token,
                ):
                    schedule, provider, warning = build_ai_schedule(
                        make_trip(),
                        [make_place(12, "Amber Fort", "26.9855")],
                    )

                self.assertIsNone(schedule)
                self.assertIsNone(provider)
                self.assertIn("Cloudflare Workers AI is not configured", warning)

        post.assert_not_called()

    @override_settings(
        CLOUDFLARE_ACCOUNT_ID="test-account",
        CLOUDFLARE_API_TOKEN="test-token",
        CLOUDFLARE_AI_MODEL="@cf/qwen/qwen3.8-27b",
    )
    @patch("planner.ai.requests.post")
    def test_calls_cloudflare_and_validates_its_response(self, post):
        response = Mock()
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "days": [
                            {"day": 1, "place_ids": [12]},
                            {"day": 2, "place_ids": [34]},
                        ],
                    }),
                },
            }],
        }
        post.return_value = response

        schedule, provider, warning = build_ai_schedule(
            make_trip(),
            [
                make_place(12, "Amber Fort", "26.9855"),
                make_place(34, "City Palace", "26.9250"),
            ],
        )

        self.assertEqual(schedule, {1: [12], 2: [34]})
        self.assertEqual(
            provider,
            "cloudflare-workers-ai:@cf/qwen/qwen3.8-27b",
        )
        self.assertIsNone(warning)
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            "https://api.cloudflare.com/client/v4/accounts/"
            "test-account/ai/v1/chat/completions",
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer test-token",
        )
        self.assertEqual(kwargs["json"]["model"], "@cf/qwen/qwen3.8-27b")
        self.assertFalse(kwargs["json"]["store"])
        self.assertEqual(kwargs["timeout"], (5, 60))
        prompt = json.loads(kwargs["json"]["messages"][1]["content"])
        self.assertEqual(prompt["trip"]["budget"], "10000")
        self.assertEqual(prompt["trip"]["travel_mode"], "any")
        self.assertIn("description", prompt["places"][0])
        self.assertIn("estimated_cost", prompt["places"][0])

    @override_settings(
        CLOUDFLARE_ACCOUNT_ID="test-account",
        CLOUDFLARE_API_TOKEN="test-token",
        CLOUDFLARE_AI_MODEL="@cf/qwen/qwen3.8-27b",
    )
    @patch("planner.ai.requests.post")
    def test_invalid_provider_response_uses_fallback_warning(self, post):
        response = Mock()
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "days": [
                            {"day": 1, "place_ids": [999]},
                            {"day": 2, "place_ids": [34]},
                        ],
                    }),
                },
            }],
        }
        post.return_value = response

        schedule, provider, warning = build_ai_schedule(
            make_trip(),
            [
                make_place(12, "Amber Fort", "26.9855"),
                make_place(34, "City Palace", "26.9250"),
            ],
        )

        self.assertIsNone(schedule)
        self.assertIsNone(provider)
        self.assertIn("invalid itinerary", warning)
        post.assert_called_once()

    @override_settings(
        CLOUDFLARE_ACCOUNT_ID="test-account",
        CLOUDFLARE_API_TOKEN="test-token",
        CLOUDFLARE_AI_MODEL="@cf/qwen/qwen3.8-27b",
    )
    @patch("planner.ai.requests.post", side_effect=requests.Timeout)
    def test_provider_timeout_uses_rule_based_fallback(self, post):
        schedule, provider, warning = build_ai_schedule(
            make_trip(),
            [make_place(12, "Amber Fort", "26.9855")],
        )

        self.assertIsNone(schedule)
        self.assertIsNone(provider)
        self.assertIn("Cloudflare Workers AI was unavailable", warning)
        post.assert_called_once()


class AIScheduleIntegrationTests(SimpleTestCase):
    def test_local_scheduler_respects_ai_day_assignments(self):
        itinerary, unscheduled = build_daily_schedule(
            [
                make_place(12, "Amber Fort", "26.9855"),
                make_place(34, "City Palace", "26.9250"),
            ],
            make_trip(),
            ai_days={1: [34], 2: [12]},
        )

        self.assertEqual(
            [(item["day"], item["place"].id) for item in itinerary],
            [(1, 34), (2, 12)],
        )
        self.assertEqual(unscheduled, [])


class ActivitySuggestionTests(SimpleTestCase):
    def test_suggests_an_unplanned_place_for_an_empty_day_without_adding_it(self):
        trip = make_trip()
        scheduled_place = make_place(12, "Amber Fort", "26.9855")
        suggested_place = make_place(34, "City Palace", "26.9250")
        itinerary = [{
            "place": scheduled_place,
            "day": 1,
            "start_time": "09:00",
            "end_time": "10:00",
            "visit_minutes": 60,
            "travel_minutes_before": 0,
            "latitude": str(scheduled_place.latitude),
            "longitude": str(scheduled_place.longitude),
        }]

        suggestions = build_activity_suggestions(
            [scheduled_place, suggested_place],
            itinerary,
            trip,
            Decimal("1000"),
        )

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["id"], suggested_place.id)
        self.assertEqual(suggestions[0]["day"], 2)
        self.assertEqual(suggestions[0]["status"], "pending_approval")
        self.assertEqual(len(itinerary), 1)

    def test_over_budget_places_remain_suggested_with_clear_cost_impact(self):
        trip = make_trip()
        expensive_place = make_place(34, "City Palace", "26.9250")
        expensive_place.estimated_cost = Decimal("1500.00")

        suggestions = build_activity_suggestions(
            [expensive_place],
            [],
            trip,
            Decimal("500.00"),
        )

        self.assertEqual(len(suggestions), 1)
        self.assertTrue(suggestions[0]["would_exceed_budget"])
        self.assertEqual(suggestions[0]["budget_overage"], "1000.00")


class ActivitySuggestionApprovalTests(APITestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="suggestiontester",
            password="TestPassword123",
        )
        self.destination = Destination.objects.create(
            name="Mumbai",
            state="Maharashtra",
        )
        self.place = Place.objects.create(
            destination=self.destination,
            name="Harbour Cafe",
            category="cafe",
            latitude=Decimal("18.9432"),
            longitude=Decimal("72.8235"),
            time_required=60,
            estimated_cost=Decimal("250.00"),
        )
        self.trip = Trip.objects.create(
            user=self.user,
            source="Goa",
            destination="Mumbai",
            start_date=date(2026, 12, 16),
            end_date=date(2026, 12, 17),
            number_of_people=1,
            estimated_budget=Decimal("5000.00"),
        )
        self.plan = Plan.objects.create(
            trip=self.trip,
            response_data={
                "trip": {"budget": "5000.00"},
                "estimated_cost": {
                    "transport": None,
                    "accommodation": None,
                    "known_total": "0.00",
                    "remaining_known_budget": "5000.00",
                    "unknown_components": [],
                },
                "dining_recommendations": [],
            },
        )
        self.client.force_authenticate(user=self.user)

    def test_approval_adds_a_cafe_to_the_saved_itinerary(self):
        response = self.client.post(
            f"/api/planner/trips/{self.trip.id}/itinerary/items/",
            {
                "day": 1,
                "place_id": self.place.id,
                "start_time": "09:00",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            ItineraryItem.objects.filter(
                plan=self.plan,
                place=self.place,
                day=1,
            ).exists()
        )
        self.assertEqual(response.data["plan"]["activity_suggestions"], [])

    def test_manual_add_uses_the_next_open_time_instead_of_overlapping(self):
        previous_place = Place.objects.create(
            destination=self.destination,
            name="Gateway of India",
            category="attraction",
            latitude=Decimal("18.9220"),
            longitude=Decimal("72.8347"),
            time_required=60,
            estimated_cost=Decimal("0.00"),
        )
        ItineraryItem.objects.create(
            plan=self.plan,
            day=1,
            place=previous_place,
            start_time=time(9, 0),
            end_time=time(10, 0),
            order=1,
        )

        response = self.client.post(
            f"/api/planner/trips/{self.trip.id}/itinerary/items/",
            {"day": 1, "place_id": self.place.id},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        added = ItineraryItem.objects.get(plan=self.plan, place=self.place)
        self.assertGreaterEqual(added.start_time, time(10, 5))

    def test_existing_plan_offers_suggestions_without_adding_them(self):
        response = self.client.get(
            f"/api/planner/trips/{self.trip.id}/latest/",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["activity_suggestions"][0]["id"],
            self.place.id,
        )
        self.assertFalse(ItineraryItem.objects.filter(plan=self.plan).exists())
        self.assertNotIn(
            "activity_suggestions",
            self.plan.__class__.objects.get(id=self.plan.id).response_data,
        )

    def test_not_now_dismisses_a_suggestion_for_the_current_plan(self):
        response = self.client.post(
            f"/api/planner/trips/{self.trip.id}/activity-suggestions/"
            f"{self.place.id}/dismiss/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["plan"]["activity_suggestions"], [])
        self.assertEqual(
            self.plan.__class__.objects.get(id=self.plan.id)
            .response_data["dismissed_activity_ids"],
            [self.place.id],
        )


class DestinationPreparationTests(TestCase):
    @patch("integrations.save_places.get_places_from_osm")
    @patch("integrations.save_places.get_coordinates")
    def test_new_city_and_sightseeing_places_are_saved_before_planning(
        self,
        get_coordinates_mock,
        get_places_mock,
    ):
        get_coordinates_mock.return_value = {
            "latitude": 29.3803,
            "longitude": 79.4636,
            "state": "Uttarakhand",
        }
        get_places_mock.return_value = [{
            "osm_id": "12345",
            "name": "Naini Lake",
            "category": "attraction",
            "latitude": 29.3803,
            "longitude": 79.4636,
            "description": "A lake in the city.",
        }]

        result = ensure_destination_places("Nainital")

        destination = Destination.objects.get(name="Nainital")
        place = Place.objects.get(destination=destination)
        self.assertEqual(destination.state, "Uttarakhand")
        self.assertEqual(place.name, "Naini Lake")
        self.assertEqual(place.description, "A lake in the city.")
        self.assertEqual(result["places_added"], 1)
        self.assertEqual(result["places_available"], 1)
        get_coordinates_mock.assert_called_once_with(
            "Nainital",
            None,
            timeout=10,
        )
        get_places_mock.assert_called_once_with(
            29.3803,
            79.4636,
            request_timeout=12,
            attempts=2,
        )

    @patch("integrations.save_places.get_places_from_osm")
    @patch("integrations.save_places.get_coordinates")
    def test_existing_city_alias_and_places_avoid_external_lookups(
        self,
        get_coordinates_mock,
        get_places_mock,
    ):
        destination = Destination.objects.create(
            name="Bengaluru",
            state="Karnataka",
        )
        Place.objects.create(
            destination=destination,
            name="Lalbagh Botanical Garden",
            category="attraction",
            latitude=Decimal("12.9507"),
            longitude=Decimal("77.5848"),
            time_required=60,
            estimated_cost=Decimal("0.00"),
        )

        result = ensure_destination_places("Bangalore")

        self.assertEqual(result["destination"], "Bengaluru")
        self.assertEqual(result["places_available"], 1)
        get_coordinates_mock.assert_not_called()
        get_places_mock.assert_not_called()

    @patch("integrations.save_places.get_coordinates", return_value=None)
    def test_unresolved_city_is_not_added_to_the_database(self, get_coordinates_mock):
        with self.assertRaises(DestinationNotFoundError):
            ensure_destination_places("Not A Real City")

        self.assertFalse(Destination.objects.exists())

    @patch("integrations.save_places.get_places_from_osm", return_value=[])
    @patch(
        "integrations.save_places.get_coordinates",
        return_value={
            "latitude": 29.3803,
            "longitude": 79.4636,
            "state": "Uttarakhand",
        },
    )
    def test_resolved_city_is_saved_even_when_osm_has_no_sightseeing_places(
        self,
        get_coordinates_mock,
        get_places_mock,
    ):
        with self.assertRaises(DestinationNotFoundError):
            ensure_destination_places("Nainital")

        destination = Destination.objects.get(name="Nainital")
        self.assertEqual(destination.state, "Uttarakhand")
        get_coordinates_mock.assert_called_once()
        # 5 km first, then one wider search before giving up.
        self.assertEqual(get_places_mock.call_count, 2)
        self.assertEqual(
            get_places_mock.call_args.kwargs["radius"], 25000
        )


class AIWithoutSavedPlacesTests(TestCase):
    @patch("planner.services.build_ai_schedule")
    def test_requested_ai_is_skipped_with_actionable_warning_when_no_places_exist(
        self,
        build_ai_schedule_mock,
    ):
        user = get_user_model().objects.create_user(
            username="planner-ai-test",
            password="test-password",
        )
        trip = Trip.objects.create(
            user=user,
            source="Delhi",
            destination="Unknown City",
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 2),
            number_of_people=1,
            estimated_budget=Decimal("10000"),
        )

        result = generate_optimal_plan(trip, use_ai=True)

        self.assertEqual(result["planner"]["mode"], "rules")
        self.assertTrue(
            any(
                "AI day assignment was skipped" in warning
                for warning in result["warnings"]
            )
        )
        build_ai_schedule_mock.assert_not_called()
        self.assertTrue(Plan.objects.filter(trip=trip).exists())


class GeneratePlanInputTests(SimpleTestCase):
    def test_rejects_non_boolean_ai_opt_in(self):
        request = APIRequestFactory().post(
            "/api/planner/trips/1/generate/",
            {"use_ai": "true"},
            format="json",
        )
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

        with (
            patch("planner.views.get_object_or_404", return_value=make_trip()),
            patch("planner.views.generate_optimal_plan") as generate,
        ):
            response = generate_plan(request, trip_id=1)

        self.assertEqual(response.status_code, 400)
        generate.assert_not_called()


class GeneratePlanPreparationTests(TestCase):
    def make_request(self, use_ai=True):
        request = APIRequestFactory().post(
            "/api/planner/trips/1/generate/",
            {"use_ai": use_ai},
            format="json",
        )
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        return request

    @patch("planner.views.ensure_destination_places")
    @patch("planner.views.generate_optimal_plan")
    @patch("planner.views.get_object_or_404")
    def test_prepares_city_places_before_generating_the_plan(
        self,
        get_object_mock,
        generate_mock,
        prepare_mock,
    ):
        trip = make_trip()
        plan = SimpleNamespace(response_data={}, save=Mock())
        preparation = {
            "destination": "Jaipur",
            "destination_created": True,
            "places_added": 4,
            "places_available": 4,
        }
        get_object_mock.side_effect = [trip, plan]
        prepare_mock.return_value = preparation
        generate_mock.return_value = {"plan_id": 1, "warnings": []}

        response = generate_plan(self.make_request(), trip_id=1)

        self.assertEqual(response.status_code, 201)
        prepare_mock.assert_called_once_with("Jaipur", min_sightseeing=10)
        generate_mock.assert_called_once()
        args, kwargs = generate_mock.call_args
        self.assertEqual(args, (trip,))
        self.assertTrue(kwargs["use_ai"])
        self.assertTrue(0 < kwargs["ai_time_budget"] <= 50)
        self.assertEqual(response.data["data_preparation"], preparation)

    @patch("planner.views.ensure_destination_places")
    @patch("planner.views.generate_optimal_plan")
    @patch("planner.views.get_object_or_404", return_value=make_trip())
    def test_does_not_generate_an_empty_plan_when_city_cannot_be_resolved(
        self,
        get_object_mock,
        generate_mock,
        prepare_mock,
    ):
        prepare_mock.side_effect = DestinationNotFoundError(
            "Could not locate the city."
        )

        response = generate_plan(self.make_request(), trip_id=1)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"], "Could not locate the city.")
        generate_mock.assert_not_called()


class BalancedOptionRankingTests(SimpleTestCase):
    def test_transport_ranking_balances_quote_and_duration(self):
        ranked = rank_transport_candidates([
            {
                "model_id": 1,
                "estimated_cost": Decimal("1000"),
                "duration_seconds": 36000,
            },
            {
                "model_id": 2,
                "estimated_cost": Decimal("1200"),
                "duration_seconds": 3600,
            },
        ])

        self.assertEqual([item["model_id"] for item in ranked], [1, 2])
        self.assertLess(
            ranked[0]["balanced_score"],
            ranked[1]["balanced_score"],
        )

    def test_hotel_ranking_uses_rating_and_straight_line_proximity(self):
        sightseeing = make_place(5, "City Palace", "26.9250")
        hotels = [
            SimpleNamespace(
                id=1,
                hotel_id=1,
                hotel=SimpleNamespace(
                    latitude=Decimal("27.0000"),
                    longitude=Decimal("75.8000"),
                ),
                price_per_night=Decimal("2500"),
                rating=Decimal("3.0"),
            ),
            SimpleNamespace(
                id=2,
                hotel_id=2,
                hotel=SimpleNamespace(
                    latitude=Decimal("26.9250"),
                    longitude=Decimal("75.8000"),
                ),
                price_per_night=Decimal("2500"),
                rating=Decimal("4.8"),
            ),
        ]

        ranked = rank_accommodation_candidates(
            hotels,
            [{"place": sightseeing}],
        )

        self.assertEqual(ranked[0]["option"].id, 2)
        self.assertEqual(
            ranked[0]["nearest_sightseeing_distance_km"],
            0.0,
        )

    def test_budget_feasibility_precedes_a_better_balanced_transport_score(self):
        transport = [
            {
                "model_id": 1,
                "estimated_cost": Decimal("1500"),
                "duration_seconds": 3600,
                "balanced_score": 0.0,
            },
            {
                "model_id": 2,
                "estimated_cost": Decimal("800"),
                "duration_seconds": 7200,
                "balanced_score": 0.8,
            },
        ]

        selected, _ = choose_optimal_combination(
            transport_candidates=transport,
            accommodation_candidates=[],
            nights=1,
            attraction_cost=Decimal("0"),
            budget=Decimal("1000"),
        )

        self.assertEqual(selected["transport"]["model_id"], 2)

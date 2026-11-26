import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from planner.ai import AIResponseError, build_ai_schedule, validate_ai_schedule
from planner.services import build_daily_schedule
from planner.views import generate_plan


def make_trip():
    return SimpleNamespace(
        source="Delhi",
        destination="Jaipur",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 2),
        number_of_people=2,
    )


def make_place(place_id, name, latitude):
    return SimpleNamespace(
        id=place_id,
        name=name,
        category="attraction",
        time_required=60,
        latitude=Decimal(latitude),
        longitude=Decimal("75.8000"),
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
        self.assertEqual(kwargs["timeout"], (5, 30))

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

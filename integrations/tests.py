from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from rest_framework.test import (
    APIRequestFactory,
    APITestCase,
    force_authenticate,
)

from integrations.views import hotel_search


class FlightSearchAPITests(APITestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="flighttester",
            email="flighttester@example.com",
            password="TestPassword123",
        )
        self.client.force_authenticate(user=self.user)
        self.url = "/api/flights/search/"
        self.params = {
            "origin": "DEL",
            "destination": "BOM",
            "date_from": date.today().isoformat(),
            "adults": 1,
        }

    def test_missing_parameters(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)

    def test_invalid_airport_code(self):
        response = self.client.get(
            self.url,
            {**self.params, "origin": "DELHI"},
        )
        self.assertEqual(response.status_code, 400)

    def test_same_origin_and_destination(self):
        response = self.client.get(
            self.url,
            {**self.params, "destination": "DEL"},
        )
        self.assertEqual(response.status_code, 400)

    def test_past_travel_date(self):
        response = self.client.get(
            self.url,
            {**self.params, "date_from": "2020-01-01"},
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_passenger_count(self):
        response = self.client.get(self.url, {**self.params, "adults": 15})
        self.assertEqual(response.status_code, 400)

    @patch("planner.ai.requests.post")
    def test_returns_free_external_flight_search_link_without_api_calls(
        self,
        ai_request,
    ):
        response = self.client.get(self.url, self.params)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["source"], "Google Flights")
        self.assertIn("google.com/travel/flights", response.data["search_url"])
        self.assertFalse(response.data["live_offers_in_app"])
        ai_request.assert_not_called()


class GeoapifyCitySuggestionTests(SimpleTestCase):
    @patch("integrations.geocoding.requests.get")
    def test_maps_and_deduplicates_city_suggestions(self, mock_get):
        response = mock_get.return_value
        response.json.return_value = {
            "results": [
                {
                    "city": "Jaipur",
                    "state": "Rajasthan",
                    "country": "India",
                    "country_code": "in",
                },
                {
                    "city": "Jaipur",
                    "state": "Rajasthan",
                    "country": "India",
                    "country_code": "in",
                },
                {
                    "city": "Jaipur",
                    "state": "Queensland",
                    "country": "Australia",
                    "country_code": "au",
                },
            ]
        }

        from integrations.geocoding import search_city_suggestions

        suggestions = search_city_suggestions("jai", "test-geoapify-key")

        self.assertEqual(
            suggestions,
            [{
                "value": "Jaipur",
                "label": "Jaipur, Rajasthan",
                "state": "Rajasthan",
                "country": "India",
            }],
        )
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["type"], "city")
        self.assertEqual(params["filter"], "countrycode:in")
        self.assertEqual(params["apiKey"], "test-geoapify-key")


class FreeFlightSearchTests(SimpleTestCase):
    @patch("planner.ai.requests.post")
    def test_builds_google_flights_link_without_network_ai_or_provider_calls(self, ai_request):
        request = APIRequestFactory().post(
            "/api/flights/search/",
            {
                "origin": "DEL",
                "destination": "BOM",
                "date_from": date.today().isoformat(),
                "adults": 2,
            },
            format="json",
        )
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

        from integrations.views import flight_search

        response = flight_search(request)

        self.assertEqual(response.status_code, 200)
        self.assertIn("google.com/travel/flights", response.data["search_url"])
        self.assertIn("adults=2", response.data["search_url"])
        self.assertFalse(response.data["live_offers_in_app"])
        ai_request.assert_not_called()


class FreeHotelSearchTests(SimpleTestCase):
    @patch("integrations.views.Hotel.objects.filter")
    @patch("planner.ai.requests.post")
    def test_returns_osm_hotels_and_google_search_without_paid_api_calls(
        self,
        ai_request,
        hotel_filter,
    ):
        hotel_filter.return_value.order_by.return_value = []
        request = APIRequestFactory().get(
            "/api/hotels/search/",
            {"city_name": "Mumbai"},
        )
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

        response = hotel_search(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["source"], "OpenStreetMap")
        self.assertFalse(response.data["live_search_available"])
        self.assertIn("google.com/search", response.data["search_url"])
        self.assertIn("availability or prices", response.data["message"])
        ai_request.assert_not_called()

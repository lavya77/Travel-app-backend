from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
import requests
from rest_framework.test import APIRequestFactory

from .views import city_suggestions


class CitySuggestionsAPITests(SimpleTestCase):

    url = "/api/destinations/suggestions/"

    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()

    @override_settings(GEOAPIFY_API_KEY="test-geoapify-key")
    @patch("destinations.views.search_city_suggestions")
    def test_returns_city_suggestions(self, mock_search):
        mock_search.return_value = [
            {
                "value": "Jaipur",
                "label": "Jaipur, Rajasthan",
                "state": "Rajasthan",
                "country": "India",
            }
        ]

        response = city_suggestions(self.factory.get(self.url, {"q": "jai"}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["suggestions"][0]["label"],
            "Jaipur, Rajasthan",
        )
        mock_search.assert_called_once_with("jai", "test-geoapify-key")

    @override_settings(GEOAPIFY_API_KEY="")
    @patch("destinations.views.search_city_suggestions")
    def test_missing_api_key_returns_service_unavailable(self, mock_search):
        response = city_suggestions(self.factory.get(self.url, {"q": "jai"}))

        self.assertEqual(response.status_code, 503)
        mock_search.assert_not_called()

    @override_settings(GEOAPIFY_API_KEY="test-geoapify-key")
    @patch("destinations.views.search_city_suggestions")
    def test_short_query_does_not_call_provider(self, mock_search):
        response = city_suggestions(self.factory.get(self.url, {"q": "j"}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"suggestions": []})
        mock_search.assert_not_called()

    @override_settings(GEOAPIFY_API_KEY="test-geoapify-key")
    @patch("destinations.views.search_city_suggestions")
    def test_provider_failure_is_reported(self, mock_search):
        mock_search.side_effect = requests.Timeout()

        response = city_suggestions(self.factory.get(self.url, {"q": "jai"}))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.data["error"],
            "City suggestions are temporarily unavailable.",
        )

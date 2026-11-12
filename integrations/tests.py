
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from unittest.mock import patch


class FlightSearchAPITests(APITestCase):

    def setUp(self):
        User = get_user_model()

        self.user = User.objects.create_user(
            username="flighttester",
            email="flighttester@example.com",
            password="TestPassword123"
        )

        self.client.force_authenticate(user=self.user)

        self.url = "/api/flights/search/"

        self.params = {
            "origin": "DEL",
            "destination": "BOM",
            "date_from": "2027-01-15",
            "adults": 1
        }

    def test_missing_parameters(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 400)

    def test_invalid_airport_code(self):
        params = self.params.copy()
        params["origin"] = "DELHI"

        response = self.client.get(self.url, params)

        self.assertEqual(response.status_code, 400)

    def test_same_origin_and_destination(self):
        params = self.params.copy()
        params["destination"] = "DEL"

        response = self.client.get(self.url, params)

        self.assertEqual(response.status_code, 400)

    def test_past_travel_date(self):
        params = self.params.copy()
        params["date_from"] = "2020-01-01"

        response = self.client.get(self.url, params)

        self.assertEqual(response.status_code, 400)

    def test_invalid_passenger_count(self):
        params = self.params.copy()
        params["adults"] = 15

        response = self.client.get(self.url, params)

        self.assertEqual(response.status_code, 400)

    @patch("integrations.views.search_flights")
    def test_successful_flight_search(self, mock_search):

        mock_search.return_value = {
            "offers": [
                {
                    "airline": "IndiGo",
                    "price": 6500,
                    "currency": "INR"
                }
            ]
        }

        response = self.client.get(self.url, self.params)

        self.assertEqual(response.status_code, 200)
        self.assertIn("offers", response.data)

        mock_search.assert_called_once()

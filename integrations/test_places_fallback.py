"""Overpass mirror failover and the Geoapify backup for place search."""
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from integrations import places


def ok(payload):
    response = MagicMock(status_code=200)
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def http_error(code):
    response = MagicMock(status_code=code)
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    return response


OVERPASS_PAYLOAD = {"elements": [{
    "type": "node", "id": 1, "lat": 15.5, "lon": 73.8,
    "tags": {"name": "Fort", "tourism": "attraction"},
}]}

GEOAPIFY_PAYLOAD = {"features": [
    {"properties": {
        "name": "Temple", "lat": 15.1, "lon": 73.1,
        "categories": ["tourism", "tourism.sights"],
        "datasource": {"raw": {"osm_type": "w", "osm_id": 42}},
    }},
    {"properties": {
        "name": "Cafe Day", "lat": 15.2, "lon": 73.2,
        "categories": ["catering", "catering.cafe"],
        "datasource": {"raw": {"osm_type": "n", "osm_id": 7}},
    }},
    {"properties": {"name": "", "lat": 1, "lon": 1, "categories": []}},
]}


class OverpassMirrorTests(SimpleTestCase):
    @patch("integrations.places.requests.post")
    def test_moves_to_next_mirror_on_timeout_and_5xx(self, post):
        post.side_effect = [requests.Timeout(), http_error(504), ok(OVERPASS_PAYLOAD)]
        data = places.fetch_overpass_data("q", attempts=3)
        self.assertEqual(data, OVERPASS_PAYLOAD)
        used = [call.args[0] for call in post.call_args_list]
        self.assertEqual(used, list(places.OVERPASS_URLS[:3]))

    @patch("integrations.places.requests.post")
    def test_non_retryable_error_is_raised_at_once(self, post):
        post.return_value = http_error(400)
        with self.assertRaises(requests.HTTPError):
            places.fetch_overpass_data("q", attempts=3)
        self.assertEqual(post.call_count, 1)


@override_settings(GEOAPIFY_API_KEY="test-key")
class GeoapifyFallbackTests(SimpleTestCase):
    @patch("integrations.places.requests.get")
    @patch("integrations.places.requests.post")
    def test_falls_back_to_geoapify_when_overpass_is_down(self, post, get):
        post.side_effect = requests.Timeout()
        get.return_value = ok(GEOAPIFY_PAYLOAD)
        result = places.get_places_from_osm(15.0, 73.0, attempts=2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            [(p["osm_type"], p["osm_id"], p["category"]) for p in result],
            [("way", 42, "attraction"), ("node", 7, "cafe")],
        )

    @patch("integrations.places.requests.get")
    @patch("integrations.places.requests.post")
    def test_overpass_success_skips_geoapify(self, post, get):
        post.return_value = ok(OVERPASS_PAYLOAD)
        result = places.get_places_from_osm(15.0, 73.0)
        self.assertEqual(result[0]["name"], "Fort")
        get.assert_not_called()

    @override_settings(GEOAPIFY_API_KEY="")
    @patch("integrations.places.requests.post")
    def test_without_key_the_overpass_error_is_raised(self, post):
        post.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            places.get_places_from_osm(15.0, 73.0, attempts=1)

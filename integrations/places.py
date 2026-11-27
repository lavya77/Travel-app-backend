
import logging
import time

import requests
from django.conf import settings

from integrations.geocoding import get_coordinates


logger = logging.getLogger(__name__)

# Public Overpass servers, tried in order. Any one of them is often slow or
# down, so a failure moves on to the next instead of failing the request.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_URL = OVERPASS_URLS[0]

GEOAPIFY_PLACES_URL = "https://api.geoapify.com/v2/places"
GEOAPIFY_SIGHT_CATEGORIES = (
    "tourism.sights,tourism.attraction,entertainment.museum,"
    "entertainment.zoo,entertainment.aquarium,entertainment.theme_park"
)
GEOAPIFY_DINING_CATEGORIES = "catering.restaurant,catering.cafe"

# Sightseeing kinds fetched from OpenStreetMap. Temples and parks are only
# taken when they have a Wikidata entry, so neighbourhood shrines and tiny
# parks do not flood the itinerary.
OVERPASS_SIGHT_FILTERS = (
    '["tourism"~"^(attraction|viewpoint|museum|gallery|zoo|theme_park|aquarium)$"]["name"]',
    '["historic"~"^(monument|memorial|fort|castle|palace|ruins|archaeological_site|city_gate)$"]["name"]',
    '["leisure"~"^(park|garden|nature_reserve)$"]["name"]["wikidata"]',
    '["amenity"="place_of_worship"]["name"]["wikidata"]',
)
OVERPASS_DINING_FILTERS = ('["amenity"~"^(restaurant|cafe)$"]["name"]',)

HEADERS = {
    "User-Agent": "SmartTravelPlanner/1.0"
}


RETRYABLE_STATUS_CODES = {429, 502, 503, 504}

OVERPASS_ATTEMPTS = 3

# After every mirror fails, skip Overpass for a while and go straight to
# Geoapify, so one outage does not cost each request 30+ seconds of timeouts.
OVERPASS_COOLDOWN_SECONDS = 120
_overpass_down_until = 0.0


def overpass_is_down():
    return time.monotonic() < _overpass_down_until


def _mark_overpass_down():
    global _overpass_down_until
    _overpass_down_until = time.monotonic() + OVERPASS_COOLDOWN_SECONDS


def fetch_overpass_data(
    query,
    request_timeout=60,
    attempts=OVERPASS_ATTEMPTS,
    urls=OVERPASS_URLS,
):
    """
    Execute an Overpass API query, trying mirror servers in turn.

    Each attempt goes to the next mirror, so `attempts` is the total number
    of requests. Timeouts, connection errors and 429/502/503/504 move on to
    the next mirror; the last error is raised when every attempt fails.
    """

    last_error = None

    for attempt in range(attempts):
        url = urls[attempt % len(urls)]
        if attempt >= len(urls):
            time.sleep(2)

        try:
            response = requests.post(
                url,
                data={"data": query},
                headers=HEADERS,
                timeout=request_timeout
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as error:
            if error.response.status_code not in RETRYABLE_STATUS_CODES:
                raise
            last_error = error
        except (requests.ConnectionError, requests.Timeout) as error:
            last_error = error
        except ValueError as error:  # not JSON (e.g. an HTML error page)
            last_error = requests.RequestException(str(error))

        logger.warning(
            "Overpass mirror %s failed (attempt %s/%s): %s",
            url, attempt + 1, attempts, last_error,
        )

    raise last_error


# ---------------- TOURIST PLACES SEARCH ----------------

def get_places_from_osm(
    latitude,
    longitude,
    radius=5000,
    request_timeout=60,
    attempts=OVERPASS_ATTEMPTS,
    include_dining=True,
):
    """
    Fetch attractions, viewpoints, restaurants and cafes.

    Overpass (with mirrors) is the main source. When every mirror fails,
    Geoapify Places, which serves the same OpenStreetMap data, is the
    backup, so places keep the same OSM ids either way.
    """

    has_backup = bool(getattr(settings, "GEOAPIFY_API_KEY", "").strip())
    if has_backup and overpass_is_down():
        return get_places_from_geoapify(
            latitude, longitude, radius, request_timeout=min(request_timeout, 15),
            include_dining=include_dining,
        )
    try:
        return _get_places_from_overpass(
            latitude, longitude, radius, request_timeout, attempts,
            include_dining=include_dining,
        )
    except requests.RequestException as error:
        _mark_overpass_down()
        if not has_backup:
            raise
        logger.warning(
            "Overpass unavailable (%s); falling back to Geoapify Places",
            error,
        )
        return get_places_from_geoapify(
            latitude, longitude, radius, request_timeout=min(request_timeout, 15),
            include_dining=include_dining,
        )


_GEOAPIFY_OSM_TYPES = {"n": "node", "w": "way", "r": "relation"}


def _geoapify_category(categories):
    if any(c.startswith("catering.cafe") for c in categories):
        return "cafe"
    if any(c.startswith("catering") for c in categories):
        return "restaurant"
    if any("viewpoint" in c for c in categories):
        return "viewpoint"
    if any(c.startswith("entertainment.museum") for c in categories):
        return "museum"
    if any("place_of_worship" in c for c in categories):
        return "place_of_worship"
    if any(c.startswith("leisure.park") for c in categories):
        return "park"
    return "attraction"


def get_places_from_geoapify(
    latitude,
    longitude,
    radius=5000,
    request_timeout=15,
    include_dining=True,
):
    """Fetch places from Geoapify in the same shape as get_places_from_osm."""

    # Sights and dining are separate requests: each is capped at 200
    # results, and in a big city restaurants would crowd out the sights.
    groups = [GEOAPIFY_SIGHT_CATEGORIES]
    if include_dining:
        groups.append(GEOAPIFY_DINING_CATEGORIES)

    features = []
    for categories in groups:
        response = requests.get(
            GEOAPIFY_PLACES_URL,
            params={
                "categories": categories,
                "filter": f"circle:{longitude},{latitude},{radius}",
                "bias": f"proximity:{longitude},{latitude}",
                "limit": 200,
                "apiKey": settings.GEOAPIFY_API_KEY.strip(),
            },
            headers=HEADERS,
            timeout=request_timeout,
        )
        response.raise_for_status()
        try:
            features.extend(response.json().get("features") or [])
        except (ValueError, AttributeError) as error:
            raise requests.RequestException("Invalid Geoapify response") from error

    places = []
    seen_ids = set()
    for feature in features:
        props = (feature or {}).get("properties") or {}
        name = props.get("name")
        lat, lon = props.get("lat"), props.get("lon")
        if not isinstance(name, str) or not name.strip() or lat is None or lon is None:
            continue

        raw = (props.get("datasource") or {}).get("raw") or {}
        osm_type = _GEOAPIFY_OSM_TYPES.get(raw.get("osm_type"))
        if osm_type and raw.get("osm_id") is not None:
            osm_id = abs(int(raw["osm_id"]))
        elif props.get("place_id"):
            osm_type, osm_id = "geoapify", props["place_id"]
        else:
            continue

        key = f"{osm_type}/{osm_id}"
        if key in seen_ids:
            continue
        seen_ids.add(key)

        places.append({
            "osm_id": osm_id,
            "osm_type": osm_type,
            "name": name.strip(),
            "category": _geoapify_category(props.get("categories") or []),
            "latitude": lat,
            "longitude": lon,
            "description": raw.get("description", "") if isinstance(raw.get("description"), str) else "",
        })

    return places


def _get_places_from_overpass(
    latitude,
    longitude,
    radius,
    request_timeout,
    attempts,
    include_dining=True,
):

    filters = OVERPASS_SIGHT_FILTERS + (
        OVERPASS_DINING_FILTERS if include_dining else ()
    )
    around = f"nwr(around:{radius},{latitude},{longitude})"
    statements = "\n".join(f"        {around}{f};" for f in filters)
    query = f"""
    [out:json][timeout:25];

    (
{statements}
    );

    out center;
    """

    data = fetch_overpass_data(
        query,
        request_timeout=request_timeout,
        attempts=attempts,
    )

    places = []
    seen_ids = set()

    for element in data.get("elements", []):

        tags = element.get("tags", {})
        name = tags.get("name")

        if not name:
            continue

        osm_id = f'{element["type"]}/{element["id"]}'

        if osm_id in seen_ids:
            continue

        if element["type"] == "node":
            lat = element.get("lat")
            lon = element.get("lon")
        else:
            center = element.get("center", {})
            lat = center.get("lat")
            lon = center.get("lon")

        if lat is None or lon is None:
            continue

        if "tourism" in tags:
            category = tags["tourism"]
        elif "historic" in tags:
            category = "historic"
        elif "leisure" in tags:
            category = "park"
        elif tags.get("amenity") == "place_of_worship":
            category = "place_of_worship"
        elif tags.get("amenity") == "restaurant":
            category = "restaurant"
        elif tags.get("amenity") == "cafe":
            category = "cafe"
        else:
            category = "other"

        places.append({
            "osm_id": element["id"],
            "osm_type": element["type"],
            "name": name,
            "category": category,
            "latitude": lat,
            "longitude": lon,
            "description": tags.get("description", ""),
        })

        seen_ids.add(osm_id)

    return places


# Discover runs inside one web request, so keep its worst case well under a
# minute: 3 mirrors x 12 s, then the Geoapify fallback.
DISCOVER_TIMEOUT = 12


def search_places_by_city(city_name, state=None, radius=5000):
    """Geocode a city (within India) and retrieve its nearby places."""

    location = get_coordinates(city_name, state, timeout=10)

    if not location:
        return []

    return get_places_from_osm(
        location["latitude"],
        location["longitude"],
        radius,
        request_timeout=DISCOVER_TIMEOUT,
    )


# ---------------- HOTEL DISCOVERY ----------------

def get_hotels_from_osm(
    latitude,
    longitude,
    radius=5000,
    request_timeout=60,
    attempts=OVERPASS_ATTEMPTS,
):
    """Fetch hotels and accommodation from OpenStreetMap."""

    query = f"""
    [out:json][timeout:25];

    (
        nwr(around:{radius},{latitude},{longitude})
        ["tourism"~"hotel|hostel|guest_house|motel|apartment|chalet"]
        ["name"];
    );

    out center;
    """

    data = fetch_overpass_data(
        query, request_timeout=request_timeout, attempts=attempts
    )

    hotels = []
    seen_ids = set()

    for element in data.get("elements", []):

        tags = element.get("tags", {})
        name = tags.get("name")

        if not name:
            continue

        osm_id = str(element["id"])
        osm_type = element["type"]

        unique_key = (osm_id, osm_type)

        if unique_key in seen_ids:
            continue

        if osm_type == "node":
            lat = element.get("lat")
            lon = element.get("lon")
        else:
            center = element.get("center", {})
            lat = center.get("lat")
            lon = center.get("lon")

        if lat is None or lon is None:
            continue

        hotels.append({
            "osm_id": element["id"],
            "osm_type": osm_type,
            "name": name,
            "category": tags.get("tourism", "accommodation"),
            "latitude": lat,
            "longitude": lon,
            "address": {
                "street": tags.get("addr:street", ""),
                "city": tags.get("addr:city", ""),
                "postcode": tags.get("addr:postcode", ""),
            },
            "website": tags.get("website", ""),
            "phone": tags.get("phone", ""),
        })

        seen_ids.add(unique_key)

    return hotels


def search_hotels_by_city(city_name, state=None, radius=5000):
    """Geocode a city (within India) and retrieve nearby accommodation."""

    location = get_coordinates(city_name, state, timeout=10)

    if not location:
        return []

    # Places are already saved when this runs, so hotels get one short try
    # rather than holding the Discover request open for minutes.
    return get_hotels_from_osm(
        location["latitude"],
        location["longitude"],
        radius,
        request_timeout=DISCOVER_TIMEOUT,
        attempts=1,
    )

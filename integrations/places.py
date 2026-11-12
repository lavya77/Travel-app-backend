
import logging
import requests

from integrations.geocoding import get_coordinates


logger = logging.getLogger(__name__)

# Restoring the previously used Overpass server.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

HEADERS = {
    "User-Agent": "SmartTravelPlanner/1.0"
}


def fetch_overpass_data(query):
    """Execute an Overpass API query."""

    response = requests.post(
        OVERPASS_URL,
        data={"data": query},
        headers=HEADERS,
        timeout=60
    )

    response.raise_for_status()
    return response.json()


# ---------------- TOURIST PLACES SEARCH ----------------

def get_places_from_osm(latitude, longitude, radius=5000):
    """
    Fetch attractions, viewpoints, restaurants and cafes.
    Restored to the previously working lightweight query.
    """

    query = f"""
    [out:json][timeout:25];

    (
        nwr(around:{radius},{latitude},{longitude})
        ["tourism"~"attraction|viewpoint"]
        ["name"];

        nwr(around:{radius},{latitude},{longitude})
        ["amenity"~"restaurant|cafe"]
        ["name"];
    );

    out center;
    """

    data = fetch_overpass_data(query)

    places = []
    seen_ids = set()

    for element in data.get("elements", []):

        tags = element.get("tags", {})
        name = tags.get("name")

        if not name:
            continue

        osm_id = str(element["id"])

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
        elif tags.get("amenity") == "restaurant":
            category = "restaurant"
        elif tags.get("amenity") == "cafe":
            category = "cafe"
        else:
            category = "other"

        places.append({
            "osm_id": element["id"],
            "name": name,
            "category": category,
            "latitude": lat,
            "longitude": lon,
        })

        seen_ids.add(osm_id)

    return places


def search_places_by_city(city_name, radius=5000):
    """Geocode a city and retrieve its nearby places."""

    location = get_coordinates(city_name)

    if not location:
        return []

    return get_places_from_osm(
        location["latitude"],
        location["longitude"],
        radius
    )


# ---------------- HOTEL DISCOVERY ----------------

def get_hotels_from_osm(latitude, longitude, radius=5000):
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

    data = fetch_overpass_data(query)

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


def search_hotels_by_city(city_name, radius=5000):
    """Geocode a city and retrieve nearby accommodation."""

    location = get_coordinates(city_name)

    if not location:
        return []

    return get_hotels_from_osm(
        location["latitude"],
        location["longitude"],
        radius
    )

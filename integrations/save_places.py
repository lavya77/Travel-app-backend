
import logging
from decimal import Decimal
import requests

from django.core.cache import cache
from django.db import transaction

from destinations.models import Destination, Place, Hotel
from integrations.geocoding import (
    canonical_city_name,
    city_lookup_names,
    get_coordinates,
)
from integrations.places import (
    get_places_from_osm,
    search_places_by_city,
    search_hotels_by_city,
)

NON_SIGHTSEEING_CATEGORIES = {
    "restaurant",
    "cafe",
    "hotel",
    "hostel",
    "guest_house",
    "guesthouse",
    "motel",
    "apartment",
    "information",
    "fuel",
    "parking",
    "toilets",
    "bus_station",
    "railway_station",
}

WIDER_SEARCH_RADIUS = 25000

logger = logging.getLogger(__name__)

# Typical visit length by category, used for newly saved places.
VISIT_MINUTES_BY_CATEGORY = {
    "viewpoint": 30,
    "place_of_worship": 45,
    "park": 60,
    "historic": 60,
    "gallery": 60,
    "museum": 90,
    "aquarium": 90,
    "zoo": 120,
    "theme_park": 180,
}

# A city is topped up with more places at most this often (seconds).
TOP_UP_INTERVAL = 6 * 60 * 60


class DestinationNotFoundError(ValueError):
    pass


class DestinationDataUnavailableError(RuntimeError):
    pass


def find_destination_record(city_name):
    for name in city_lookup_names(city_name):
        destination = (
            Destination.objects
            .filter(name__iexact=name)
            .order_by("id")
            .first()
        )
        if destination:
            return destination
    return None


def _sightseeing_places(destination):
    return (
        Place.objects
        .filter(destination=destination)
        .exclude(category__in=NON_SIGHTSEEING_CATEGORIES)
    )


def _has_sightseeing_places(destination):
    return _sightseeing_places(destination).exists()


def _top_up_places(destination, min_sightseeing):
    """Fetch more sightseeing places around a city that has too few.

    Best effort: a failed lookup keeps the places already saved. The city
    centre is the average of its saved places, so no geocoding is needed.
    """
    key = f"places-top-up:{destination.id}"
    if not cache.add(key, True, TOP_UP_INTERVAL):
        return 0

    coords = list(_sightseeing_places(destination).values_list("latitude", "longitude"))
    latitude = sum(float(lat) for lat, _ in coords) / len(coords)
    longitude = sum(float(lon) for _, lon in coords) / len(coords)

    added = 0
    try:
        for radius in (5000, WIDER_SEARCH_RADIUS):
            fetched = get_places_from_osm(
                latitude,
                longitude,
                radius=radius,
                request_timeout=12,
                attempts=1,
                include_dining=False,
            )
            added += save_places_to_database(
                destination.name, destination.state, fetched
            )["new_places_saved"]
            if _sightseeing_places(destination).count() >= min_sightseeing:
                break
    except requests.RequestException as error:
        logger.warning(
            "Place top-up for %s failed (%s)", destination.name, type(error).__name__
        )
    return added


def ensure_destination_places(city_name, min_sightseeing=0):
    """Resolve and save a city's OSM places before generating its plan.

    `min_sightseeing` is how many sightseeing places the trip can use; a
    saved city with fewer is topped up with a wider search.
    """
    city_name = city_name.strip()
    if not city_name:
        raise DestinationNotFoundError("Enter a destination city before generating a plan.")

    destination = find_destination_record(city_name)
    if destination and _has_sightseeing_places(destination):
        places_added = 0
        if _sightseeing_places(destination).count() < min_sightseeing:
            places_added = _top_up_places(destination, min_sightseeing)
        return {
            "destination": destination.name,
            "destination_created": False,
            "places_added": places_added,
            "places_available": destination.places.count(),
        }

    try:
        location = get_coordinates(
            city_name,
            destination.state if destination and destination.state else None,
            timeout=10,
        )
    except requests.RequestException as error:
        raise DestinationDataUnavailableError(
            "City lookup is temporarily unavailable. Please try again."
        ) from error

    if not location:
        raise DestinationNotFoundError(
            f"Could not locate “{city_name}” in India. Check the spelling and try again."
        )

    state = (
        (destination.state if destination else "")
        or location.get("state")
        or ""
    )
    destination_created = False
    if not destination:
        destination, destination_created = get_or_create_destination_case_insensitive(
            canonical_city_name(city_name),
            state,
        )
    elif not destination.state and state:
        destination.state = state
        destination.save(update_fields=["state"])

    try:
        fetched_places = get_places_from_osm(
            location["latitude"],
            location["longitude"],
            request_timeout=12,
            attempts=2,
        )
        if not any(
            p["category"] not in NON_SIGHTSEEING_CATEGORIES
            for p in fetched_places
        ):
            # Small towns and villages often have nothing mapped within
            # 5 km, so look once more across the surrounding area.
            fetched_places += get_places_from_osm(
                location["latitude"],
                location["longitude"],
                radius=WIDER_SEARCH_RADIUS,
                request_timeout=12,
                attempts=1,
            )
    except requests.RequestException as error:
        raise DestinationDataUnavailableError(
            "Place search is temporarily unavailable. Please retry in a minute."
        ) from error

    places_added = 0
    if fetched_places:
        result = save_places_to_database(
            destination.name,
            state,
            fetched_places,
        )
        places_added = result["new_places_saved"]

    if not _has_sightseeing_places(destination):
        raise DestinationNotFoundError(
            f"No sightseeing places were found for “{city_name}”. "
            "Try another spelling or use Discover to check the city."
        )

    return {
        "destination": destination.name,
        "destination_created": destination_created,
        "places_added": places_added,
        "places_available": destination.places.count(),
    }


@transaction.atomic
def get_or_create_destination_case_insensitive(
    destination_name,
    state
):
    """
    Find a destination case-insensitively.

    If duplicate destination records already exist, preserve the
    most populated record, move related places and hotels to it,
    and remove the duplicate destination records.
    """

    destination_name = canonical_city_name(destination_name)

    matches = []
    for name in city_lookup_names(destination_name):
        matches.extend(
            Destination.objects.select_for_update()
            .filter(name__iexact=name)
            .exclude(id__in=[m.id for m in matches])
            .order_by("id")
        )

    if not matches:
        destination = Destination.objects.create(
            name=destination_name,
            state=state,
            country="India",
        )

        return destination, True

    # Prefer the destination containing the most existing data.
    destination = max(
        matches,
        key=lambda item: (
            item.places.count() + item.hotels.count(),
            item.places.count(),
            item.hotels.count(),
            -item.id,
        )
    )

    # Merge any duplicate records into the chosen destination.
    for duplicate in matches:

        if duplicate.id == destination.id:
            continue

        Place.objects.filter(
            destination=duplicate
        ).update(
            destination=destination
        )

        Hotel.objects.filter(
            destination=duplicate
        ).update(
            destination=destination
        )

        duplicate.delete()

    # Fill the state only when it is currently missing.
    if not destination.state and state:
        destination.state = state
        destination.save(update_fields=["state"])

    return destination, False


# ---------------- SAVE TOURIST PLACES ----------------

def _place_keys(item):
    """Return (typed_key, legacy_key) for an OSM element.

    Nodes, ways and relations share a numeric id space, so new rows are
    keyed "way/123". Older rows were keyed by the bare id; they are reused
    only when they clearly describe the same place.
    """
    numeric_id = str(item["osm_id"])
    osm_type = item.get("osm_type")
    if not osm_type or osm_type == "node":
        return numeric_id, None
    return f"{osm_type}/{numeric_id}", numeric_id


def _save_place(destination, item):
    typed_key, legacy_key = _place_keys(item)
    place = Place.objects.filter(osm_id=typed_key).first()
    if place is None and legacy_key:
        legacy = Place.objects.filter(osm_id=legacy_key).first()
        if (
            legacy
            and legacy.destination_id == destination.id
            and legacy.name.casefold() == item["name"].casefold()
        ):
            place = legacy

    if place is not None and place.destination_id != destination.id:
        # osm_id is unique, so the element already belongs to another city
        # (e.g. a place near a border). Never move or overwrite it.
        return place, False

    fields = {
        "name": item["name"],
        "category": item["category"],
        "latitude": Decimal(str(item["latitude"])),
        "longitude": Decimal(str(item["longitude"])),
    }
    if item.get("description"):
        fields["description"] = item["description"]

    if place is not None:
        # Refresh OSM-owned fields only; keep curated cost and duration.
        for key, value in fields.items():
            setattr(place, key, value)
        place.save(update_fields=list(fields))
        return place, False

    place = Place.objects.create(
        destination=destination,
        osm_id=typed_key,
        time_required=VISIT_MINUTES_BY_CATEGORY.get(item["category"], 60),
        estimated_cost=Decimal("0.00"),
        description=item.get("description") or "",
        **{k: v for k, v in fields.items() if k != "description"},
    )
    return place, True


@transaction.atomic
def save_places_to_database(
    destination_name,
    state,
    places
):
    destination, destination_created = (
        get_or_create_destination_case_insensitive(
            destination_name,
            state
        )
    )

    saved_count = 0

    for item in places:

        place, created = _save_place(destination, item)

        if created:
            saved_count += 1

    return {
        "destination": destination.name,
        "new_places_saved": saved_count,
        "total_places_received": len(places),
        "destination_created": destination_created,
    }


def fetch_and_save_city_places(city_name, state):

    places = search_places_by_city(city_name, state)

    if not places:
        return {
            "destination": city_name,
            "message": "No places found",
            "new_places_saved": 0,
            "total_places_received": 0,
        }

    return save_places_to_database(
        destination_name=city_name,
        state=state,
        places=places,
    )


# ---------------- SAVE HOTELS ----------------

@transaction.atomic
def save_hotels_to_database(
    destination_name,
    state,
    hotels
):
    destination, destination_created = (
        get_or_create_destination_case_insensitive(
            destination_name,
            state
        )
    )

    saved_count = 0

    for item in hotels:

        address = item.get("address") or {}

        hotel, created = Hotel.objects.update_or_create(
            osm_id=str(item["osm_id"]),
            osm_type=item["osm_type"],
            defaults={
                "destination": destination,
                "name": item["name"],
                "category": item["category"],
                "latitude": Decimal(str(item["latitude"])),
                "longitude": Decimal(str(item["longitude"])),
                "street": address.get("street") or "",
                "city": address.get("city") or destination.name,
                "postcode": address.get("postcode") or "",
                "website": item.get("website") or "",
                "phone": item.get("phone") or "",
            }
        )

        if created:
            saved_count += 1

    return {
        "destination": destination.name,
        "new_hotels_saved": saved_count,
        "total_hotels_received": len(hotels),
        "destination_created": destination_created,
    }


def fetch_and_save_city_hotels(city_name, state):

    hotels = search_hotels_by_city(city_name, state)

    if not hotels:
        return {
            "destination": city_name,
            "message": "No hotels found",
            "new_hotels_saved": 0,
            "total_hotels_received": 0,
        }

    return save_hotels_to_database(
        destination_name=city_name,
        state=state,
        hotels=hotels,
    )

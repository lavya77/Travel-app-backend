
from decimal import Decimal

from django.db import transaction

from destinations.models import Destination, Place, Hotel
from integrations.places import (
    search_places_by_city,
    search_hotels_by_city,
)


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

    destination_name = destination_name.strip()

    matches = list(
        Destination.objects.select_for_update()
        .filter(name__iexact=destination_name)
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

        place, created = Place.objects.update_or_create(
            osm_id=str(item["osm_id"]),
            defaults={
                "destination": destination,
                "name": item["name"],
                "category": item["category"],
                "latitude": Decimal(str(item["latitude"])),
                "longitude": Decimal(str(item["longitude"])),
                "time_required": 60,
                "estimated_cost": Decimal("0.00"),
            }
        )

        if created:
            saved_count += 1

    return {
        "destination": destination.name,
        "new_places_saved": saved_count,
        "total_places_received": len(places),
        "destination_created": destination_created,
    }


def fetch_and_save_city_places(city_name, state):

    places = search_places_by_city(city_name)

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

    hotels = search_hotels_by_city(city_name)

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

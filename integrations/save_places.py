
from decimal import Decimal

from destinations.models import Destination, Place, Hotel

from integrations.places import (
    search_places_by_city,
    search_hotels_by_city
)


# ---------------- SAVE TOURIST PLACES ----------------

def save_places_to_database(destination_name, state, places):

    destination, created = Destination.objects.get_or_create(
        name=destination_name,
        defaults={
            "state": state,
            "country": "India"
        }
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
        "total_places_received": len(places)
    }


def fetch_and_save_city_places(city_name, state):

    places = search_places_by_city(city_name)

    if not places:
        return {
            "destination": city_name,
            "message": "No places found",
            "new_places_saved": 0,
            "total_places_received": 0
        }

    result = save_places_to_database(
        destination_name=city_name,
        state=state,
        places=places
    )

    return result


# ---------------- SAVE HOTELS ----------------

def save_hotels_to_database(destination_name, state, hotels):

    destination, created = Destination.objects.get_or_create(
        name=destination_name,
        defaults={
            "state": state,
            "country": "India"
        }
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
                "city": address.get("city") or destination_name,
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
        "total_hotels_received": len(hotels)
    }


def fetch_and_save_city_hotels(city_name, state):

    hotels = search_hotels_by_city(city_name)

    if not hotels:
        return {
            "destination": city_name,
            "message": "No hotels found",
            "new_hotels_saved": 0,
            "total_hotels_received": 0
        }

    result = save_hotels_to_database(
        destination_name=city_name,
        state=state,
        hotels=hotels
    )

    return result

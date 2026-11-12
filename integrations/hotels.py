
from letsfg import LetsFG


def get_hotel_destinations(city_name):
    """Find hotel-provider destinations for a city."""

    client = LetsFG()
    return client.hotel_destinations(text=city_name)


def search_hotels(
    city_name,
    check_in,
    check_out,
    adults=2,
    children=0,
    child_ages=None,
    nationality="IN",
    limit=40,
    currency="INR",
):
    """
    Resolve the city and search available hotels.
    """

    client = LetsFG()

    destinations = client.hotel_destinations(text=city_name)

    if not destinations:
        raise ValueError("No hotel destination found for this city.")

    # Use the best matching destination returned by the provider.
    selected_city = destinations[0]

    city_id = selected_city["Id"]
    resolved_city_name = selected_city["Name"]

    result = client.search_hotels(
        city_id=int(city_id),
        city_name=resolved_city_name,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children=children,
        child_ages=child_ages,
        nationality=nationality,
        limit=limit,
        with_images=True,
        currency=currency,
    )

    return result

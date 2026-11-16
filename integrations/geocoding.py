
import requests


def get_coordinates(city_name, state=None):

    url = "https://nominatim.openstreetmap.org/search"

    # Include the state and country so that same-named places abroad
    # (e.g. Vasco da Gama in Brazil) are not picked instead.
    query = (
        f"{city_name}, {state}, India"
        if state
        else f"{city_name}, India"
    )

    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "in",  # only search inside India
    }

    headers = {
        "User-Agent": "SmartTravelPlanner/1.0"
    }

    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if not data:
        return None

    return {
        "latitude": float(data[0]["lat"]),
        "longitude": float(data[0]["lon"]),
        "display_name": data[0]["display_name"]
    }

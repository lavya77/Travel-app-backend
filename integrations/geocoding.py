
import requests

GEOAPIFY_AUTOCOMPLETE_URL = "https://api.geoapify.com/v1/geocode/autocomplete"
CITY_ALIASES = {
    "bangalore": "Bengaluru",
    "banglore": "Bengaluru",
}


def canonical_city_name(city_name):
    name = city_name.strip()
    return CITY_ALIASES.get(name.casefold(), name)


def city_name_variants(city_name):
    name = city_name.strip()
    canonical_name = canonical_city_name(name)
    if canonical_name.casefold() == name.casefold():
        return (name,)
    return (name, canonical_name)


def same_city(first, second):
    """True when two city names refer to the same city (alias-aware)."""
    return (
        canonical_city_name(first or "").casefold()
        == canonical_city_name(second or "").casefold()
    )


def city_lookup_names(city_name):
    """Every spelling that should match this city, canonical first."""
    canonical = canonical_city_name(city_name)
    names = [canonical]
    for alias, target in CITY_ALIASES.items():
        if target.casefold() == canonical.casefold():
            names.append(alias)
    if city_name.strip().casefold() not in {n.casefold() for n in names}:
        names.append(city_name.strip())
    return tuple(names)


def get_coordinates(city_name, state=None, timeout=30):
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
        "addressdetails": 1,
    }

    headers = {
        "User-Agent": "SmartTravelPlanner/1.0"
    }

    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=timeout
    )

    response.raise_for_status()

    data = response.json()

    if not data:
        return None

    address = data[0].get("address") or {}
    return {
        "latitude": float(data[0]["lat"]),
        "longitude": float(data[0]["lon"]),
        "display_name": data[0]["display_name"],
        "state": address.get("state") or "",
    }


def search_city_suggestions(query, api_key, limit=6):
    response = requests.get(
        GEOAPIFY_AUTOCOMPLETE_URL,
        params={
            "text": query,
            "type": "city",
            "filter": "countrycode:in",
            "format": "json",
            "limit": limit,
            "apiKey": api_key,
        },
        headers={"User-Agent": "SmartTravelPlanner/1.0"},
        timeout=8,
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("Geoapify returned an invalid city suggestion response.")

    suggestions = []
    seen = set()
    for result in payload["results"]:
        if not isinstance(result, dict):
            continue

        name_value = result.get("city") or result.get("name")
        if not isinstance(name_value, str):
            continue
        name = name_value.strip()
        state_value = result.get("state")
        state = state_value.strip() if isinstance(state_value, str) else ""
        country_code_value = result.get("country_code")
        country_code = country_code_value.lower() if isinstance(country_code_value, str) else ""
        if not name or country_code != "in":
            continue

        key = (name.casefold(), state.casefold())
        if key in seen:
            continue
        seen.add(key)

        suggestions.append({
            "value": name,
            "label": f"{name}, {state}" if state else name,
            "state": state,
            "country": (
                result.get("country").strip()
                if isinstance(result.get("country"), str)
                else "India"
            ),
        })

    return suggestions

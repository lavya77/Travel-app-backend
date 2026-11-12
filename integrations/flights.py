
import logging

from letsfg import LetsFG


logger = logging.getLogger(__name__)


def serialize_result(result):
    """
    Convert LetsFG SDK responses into JSON-compatible Python data.
    """

    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")

    if hasattr(result, "dict"):
        return result.dict()

    if isinstance(result, dict):
        return result

    if isinstance(result, list):
        return [serialize_result(item) for item in result]

    if isinstance(result, tuple):
        return [serialize_result(item) for item in result]

    if hasattr(result, "__dict__"):
        return {
            key: serialize_result(value)
            for key, value in vars(result).items()
            if not key.startswith("_")
        }

    return result


def search_flights(
    origin,
    destination,
    date_from,
    adults=1,
    currency="INR",
    limit=20,
    sort="price",
):
    """
    Search flights through the LetsFG Python SDK.

    Authentication is handled by the SDK using
    LETSFG_BEARER_TOKEN or its saved local configuration.
    """

    try:
        client = LetsFG()

        result = client.search(
            origin,
            destination,
            date_from,
            adults=int(adults),
            currency=currency,
            limit=int(limit),
            sort=sort,
        )

        return serialize_result(result)

    except Exception:
        logger.exception(
            "LetsFG flight search failed: %s -> %s on %s",
            origin,
            destination,
            date_from,
        )
        raise

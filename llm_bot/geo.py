"""Time zone from a shared location."""
import httpx
from zoneinfo import ZoneInfo

# Free, keyless; coordinates are rounded to ~10 km before they leave the server.
LOOKUP = "https://api.open-meteo.com/v1/forecast"


def by_longitude(lon: float) -> str:
    """Rough fallback: solar offset; Etc/GMT signs are inverted (Etc/GMT-3 is UTC+3)."""
    offset = max(-12, min(14, round(lon / 15)))
    return "UTC" if offset == 0 else f"Etc/GMT{-offset:+d}"


async def timezone_at(client: httpx.AsyncClient | None, lat: float, lon: float) -> tuple[str, bool]:
    """Returns (IANA zone, exact). exact=False means the longitude fallback was used."""
    if client is not None:
        try:
            response = await client.get(LOOKUP, params={"latitude": round(lat, 1), "longitude": round(lon, 1),
                                                        "timezone": "auto", "forecast_days": 1}, timeout=10)
            name = response.json()["timezone"]
            ZoneInfo(name)
            return name, True
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
    return by_longitude(lon), False

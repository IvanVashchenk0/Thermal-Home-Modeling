"""Read real EPW LOCATION headers; filenames are never used as city metadata."""

from collections import Counter
import csv
from dataclasses import asdict, dataclass, replace
import logging
import math
from pathlib import Path
import re
from typing import Any

from thermal_home_modeling.errors import ConfigurationError

logger = logging.getLogger(__name__)


def friendly_city(station_name: str) -> str:
    """Remove common airport suffixes while retaining unfamiliar station wording."""
    city = " ".join(station_name.replace("_", " ").split())
    city = re.split(r"\s+(?:O['’]?Hare|Ohare|International|Intl|Airport)\b", city, maxsplit=1, flags=re.I)[0]
    if city.isupper() or city.islower():
        city = city.title()
    return re.sub(r"\bDc\b", "DC", city)


@dataclass(frozen=True)
class EPWLocation:
    """Header-derived location, plus a friendly selector label."""

    path: Path
    city: str
    station_name: str
    region: str
    country: str
    source: str
    station_id: str
    latitude: float
    longitude: float
    time_zone: float
    elevation: float
    label: str

    def to_dict(self) -> dict[str, Any]:
        """Keep exact header provenance and the resolved file path together."""
        return {**asdict(self), "path": str(self.path)}

    def site_location(self) -> dict[str, float]:
        """Return the documented epJSON Site:Location numeric fields."""
        return {name: getattr(self, name) for name in ("latitude", "longitude", "time_zone", "elevation")}


def read_epw_location(path: Path) -> EPWLocation:
    """Validate a LOCATION header, including finite coordinates and time zone."""
    path = Path(path).expanduser().resolve()
    try:
        if path.suffix.lower() != ".epw":
            raise ValueError("expected an .epw file")
        with path.open(encoding="utf-8-sig") as handle:
            header = [value.strip() for value in next(csv.reader(handle))]
        if len(header) < 10 or header[0].upper() != "LOCATION" or not header[1]:
            raise ValueError("expected LOCATION,city,region,country,source,station,latitude,longitude,time_zone,elevation")
        latitude, longitude, time_zone, elevation = map(float, header[6:10])
        if not all(math.isfinite(value) for value in (latitude, longitude, time_zone, elevation)):
            raise ValueError("location coordinates must be finite")
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and -12 <= time_zone <= 14):
            raise ValueError("latitude, longitude, or time zone is out of range")
        city = friendly_city(header[1])
        region = header[2] if header[2] not in ("", "-") else ""
        country = header[3]
        parts = [city, region]
        if country not in ("", "USA", "US") or not region:
            parts.append(country)
        label = ", ".join(part for part in parts if part)
        return EPWLocation(path, city, header[1], region, country, header[4], header[5],
                           latitude, longitude, time_zone, elevation, label)
    except (OSError, ValueError, StopIteration, csv.Error) as exc:
        raise ConfigurationError(f"Cannot read EPW location from {path}: {exc}") from exc


def discover_weather(directory: Path) -> list[EPWLocation]:
    """Discover valid installed EPWs and disambiguate cities with station/source."""
    if not directory.is_dir():
        raise ConfigurationError(f"WeatherData directory does not exist: {directory}")
    locations = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".epw":
            try:
                locations.append(read_epw_location(path))
            except ConfigurationError as exc:
                logger.warning("Skipping invalid installed weather file: %s", exc)
    if not locations:
        raise ConfigurationError(f"No valid EPW LOCATION headers found under {directory}")
    counts = Counter(location.label.casefold() for location in locations)
    labels: Counter[str] = Counter()
    result = []
    for location in locations:
        label = location.label
        if counts[label.casefold()] > 1:
            station = " · ".join(filter(None, (location.station_name, location.source, location.station_id)))
            label += f" — {station}"
        labels[label] += 1
        if labels[label] > 1:
            label += f" · file {labels[label]}"
        result.append(replace(location, label=label))
    return sorted(result, key=lambda location: (location.label.casefold(), str(location.path)))

"""SQLite extraction with explicit units, weather periods, and metric scope."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3
from typing import Iterator, Protocol

from thermal_home_modeling.config.output_config import OutputConfig, PeakVariable
from thermal_home_modeling.errors import ResultParsingError

JOULES_PER_KWH = 3_600_000
ENERGY_TO_KWH = {"J": 1 / JOULES_PER_KWH, "kJ": 1 / 3600, "MJ": 1 / 3.6, "GJ": 1000 / 3.6, "Wh": 0.001, "kWh": 1.0}
POWER_TO_KW = {"W": 0.001, "kW": 1.0}
WEATHER_RUN_PERIOD = 3


@dataclass(frozen=True)
class ParsedMetrics:
    """Metrics only; identity, execution diagnostics, and provenance live elsewhere."""

    annual_heating_kwh: float
    annual_cooling_kwh: float
    annual_total_hvac_kwh: float | None = None
    peak_heating_kw: float | None = None
    peak_cooling_kw: float | None = None


class ResultParser(Protocol):
    """Future CSV/ESO adapters must return the same metrics or a parsing error."""

    def parse(self, output_directory: Path) -> ParsedMetrics:
        """Extract explicitly configured metrics from one simulation directory."""
        ...


class SQLiteResultParser:
    """Parse requested RunPeriod meters and optional exact-key rate variables."""

    def __init__(self, outputs: OutputConfig):
        self.outputs = outputs

    def parse(self, output_directory: Path) -> ParsedMetrics:
        """Require one full weather year; design-day/warmup rows never contribute."""
        with self._weather_database(output_directory) as (connection, environment):
            annual = {name: self._annual_meter(connection, environment, name) for name in self.outputs.all_meters()}
            return ParsedMetrics(
                annual_heating_kwh=sum(annual[name] for name in self.outputs.heating_meters),
                annual_cooling_kwh=sum(annual[name] for name in self.outputs.cooling_meters),
                annual_total_hvac_kwh=sum(annual[name] for name in self.outputs.total_hvac_meters) if self.outputs.total_hvac_meters else None,
                peak_heating_kw=self._peak(connection, environment, self.outputs.peak_heating),
                peak_cooling_kw=self._peak(connection, environment, self.outputs.peak_cooling),
            )

    def parse_annual_meters(self, output_directory: Path, *, required: tuple[str, ...],
                            optional: tuple[str, ...] = ()) -> dict[str, float]:
        """Read named annual meters; omit absent optional meters, never replace with zero.

        A present but corrupt, ambiguous, or partial optional output still fails.
        This lets a demonstration model report its actual fuel mix while keeping
        the existing research parser's strict required metrics unchanged.
        """
        with self._weather_database(output_directory) as (connection, environment):
            available = {row[0].casefold() for row in connection.execute(
                "SELECT Name FROM ReportDataDictionary WHERE IsMeter = 1 AND ReportingFrequency = 'Run Period' COLLATE NOCASE"
            )}
            names = list(required) + [name for name in optional if name.casefold() in available]
            return {name: self._annual_meter(connection, environment, name) for name in dict.fromkeys(names)}

    def parse_raw_annual_variables(self, output_directory: Path, variables: dict[str, str], *, key: str) -> dict:
        """Direct annual variable values, retaining original units; no energy/fuel conversion."""
        with self._weather_database(output_directory) as (connection, environment):
            outputs = {}
            for field, name in variables.items():
                dictionary = self._dictionary(connection, name, "Run Period", False, key)
                rows = self._values(connection, dictionary["ReportDataDictionaryIndex"], environment, annual_summary=True)
                if dictionary["Type"].casefold() != "sum" or dictionary["Units"] != "J":
                    raise ResultParsingError(f"Unexpected type/units for residential load {name}")
                if len(rows) != 1 or rows[0]["SimulationDays"] not in (365, 366):
                    raise ResultParsingError(f"Expected one full annual weather-period value for {name}")
                val = rows[0]["Value"]
                if val is None or not math.isfinite(val) or val < 0:
                    raise ResultParsingError(f"Invalid residential load value for {name}")
                outputs[field] = {"value": val, "units": dictionary["Units"], "frequency": "Run Period",
                                  "variable": name, "key": key, "simulation_days": rows[0]["SimulationDays"]}
            return outputs

    @staticmethod
    @contextmanager
    def _weather_database(output_directory: Path) -> Iterator[tuple[sqlite3.Connection, int]]:
        """Open one weather-period database read-only and normalize SQL errors."""
        path = output_directory / "eplusout.sql"
        if not path.is_file():
            raise ResultParsingError(f"Missing {path}; enable Output:SQLite")
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                environments = connection.execute("SELECT EnvironmentPeriodIndex FROM EnvironmentPeriods WHERE EnvironmentType = ?", (WEATHER_RUN_PERIOD,)).fetchall()
                if len(environments) != 1:
                    raise ResultParsingError(f"Expected one weather run period, found {len(environments)}; do not combine multiple years/environments")
                environment = environments[0][0]
                yield connection, environment
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise ResultParsingError(f"Cannot parse {path}: {exc}") from exc

    @staticmethod
    def _dictionary(connection: sqlite3.Connection, name: str, frequency: str, is_meter: bool, key: str | None = None) -> sqlite3.Row:
        query = """SELECT ReportDataDictionaryIndex, Units, Type FROM ReportDataDictionary
                   WHERE Name = ? COLLATE NOCASE AND ReportingFrequency = ? COLLATE NOCASE AND IsMeter = ?"""
        parameters: list[object] = [name, frequency, int(is_meter)]
        if key is not None:
            query += " AND KeyValue = ? COLLATE NOCASE"
            parameters.append(key)
        matches = connection.execute(query, parameters).fetchall()
        if len(matches) != 1:
            raise ResultParsingError(f"Expected exactly one {frequency} output for {name!r} key={key!r}, found {len(matches)}; verify output_config.py against .mdd/.rdd")
        return matches[0]

    @staticmethod
    def _values(connection: sqlite3.Connection, dictionary_index: int, environment: int,
                *, annual_summary: bool = False) -> list[sqlite3.Row]:
        # Real RunPeriod summary records can have NULL WarmupFlag (observed in
        # 26.2). They are aggregate records, not warmup timesteps. Only the annual
        # meter path permits this; peaks still require explicit non-warmup rows.
        warmup_filter = "(t.WarmupFlag = 0 OR t.WarmupFlag IS NULL)" if annual_summary else "t.WarmupFlag = 0"
        return connection.execute('''SELECT r.Value, t.SimulationDays, t.TimeIndex FROM ReportData r
            JOIN "Time" t ON r.TimeIndex = t.TimeIndex
            WHERE r.ReportDataDictionaryIndex = ? AND t.EnvironmentPeriodIndex = ?
              AND ''' + warmup_filter + ' ORDER BY t.TimeIndex', (dictionary_index, environment)).fetchall()

    def _annual_meter(self, connection: sqlite3.Connection, environment: int, name: str) -> float:
        dictionary = self._dictionary(connection, name, "Run Period", True)
        if dictionary["Type"].casefold() != "sum":
            raise ResultParsingError(f"Annual meter {name!r} must report summed energy")
        values = self._values(connection, dictionary["ReportDataDictionaryIndex"], environment, annual_summary=True)
        if len(values) != 1 or values[0]["SimulationDays"] not in (365, 366):
            raise ResultParsingError(f"{name!r} requires exactly one full-year RunPeriod value (365/366 days); partial runs are not annual results")
        return self._convert(values[0]["Value"], dictionary["Units"], ENERGY_TO_KWH, name)

    def _peak(self, connection: sqlite3.Connection, environment: int, variable: PeakVariable | None) -> float | None:
        if variable is None:
            return None
        frequency = {"Hourly": "Hourly", "Timestep": "Zone Timestep"}[variable.frequency]
        dictionary = self._dictionary(connection, variable.name, frequency, False, variable.key)
        if dictionary["Type"].casefold() != "avg":
            raise ResultParsingError(f"Peak variable {variable.name!r} must be an average rate")
        values = self._values(connection, dictionary["ReportDataDictionaryIndex"], environment)
        if not values:
            raise ResultParsingError(f"No weather-period values for {variable.name!r}")
        if len({row["TimeIndex"] for row in values}) != len(values):
            raise ResultParsingError(f"Duplicate time values for {variable.name!r}")
        return max(self._convert(row["Value"], dictionary["Units"], POWER_TO_KW, variable.name) for row in values)

    @staticmethod
    def _convert(value: float, unit: str, factors: dict[str, float], name: str) -> float:
        if unit not in factors:
            raise ResultParsingError(f"Unsupported units {unit!r} for {name!r}; expected {list(factors)}")
        if value is None or not math.isfinite(value) or value < 0:
            raise ResultParsingError(f"Invalid negative/nonfinite/missing value for {name!r}: {value!r}")
        return value * factors[unit]

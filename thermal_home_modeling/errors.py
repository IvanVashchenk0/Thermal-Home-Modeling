"""Errors classified by the stage that needs attention."""


class ThermalModelingError(Exception):
    """Base for expected pipeline failures."""

    category = "internal"


class ConfigurationError(ThermalModelingError):
    """Missing or invalid settings or input assets."""

    category = "configuration"


class ModelGenerationError(ThermalModelingError):
    """Invalid model or a requested change that is not implemented."""

    category = "model_generation"


class EnergyPlusExecutionError(ThermalModelingError):
    """The process could not launch, timed out, or exited unsuccessfully."""

    category = "execution"


class EnergyPlusSimulationError(ThermalModelingError):
    """EnergyPlus reported an invalid or incomplete simulation."""

    category = "simulation"


class ResultParsingError(ThermalModelingError):
    """Required output is missing, ambiguous, or cannot be interpreted."""

    category = "result_parsing"


class StorageError(ThermalModelingError):
    """Checkpoints cannot be safely persisted or read; stop the batch."""

    category = "storage"

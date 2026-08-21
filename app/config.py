"""Typed YAML configuration with explicit environment-variable overrides."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
VersionIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=115,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
_ENV_PATTERN = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


class SettingsError(ValueError):
    """Raised when the configuration source cannot be resolved safely."""


class SettingsModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class PathsSettings(SettingsModel):
    input: Path = Path("/data/input")
    output: Path = Path("/data/output")
    state: Path = Path("/data/state")
    state_database_name: NonEmpty = "state.sqlite"
    temp: Path = Path("/tmp/reolink-analyzer")

    @field_validator("state_database_name")
    @classmethod
    def database_name_only(cls, value: str) -> str:
        candidate = Path(value)
        if (
            candidate.name != value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
        ):
            raise ValueError("state_database_name must be a filename, not a path")
        return value

    @property
    def state_database(self) -> Path:
        return self.state / self.state_database_name


class ScheduleSettings(SettingsModel):
    target: Literal["previous_day"] = "previous_day"


class InputSettings(SettingsModel):
    stable_seconds: int = Field(default=120, ge=0)
    stability_recheck_sec: float = Field(default=2.0, ge=0, le=30)
    ffprobe_timeout_sec: float = Field(default=60.0, gt=0)
    extensions: tuple[NonEmpty, ...] = (".mp4",)

    @field_validator("extensions")
    @classmethod
    def normalize_extensions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("at least one input extension is required")
        normalized = tuple(
            extension.lower() if extension.startswith(".") else f".{extension.lower()}"
            for extension in values
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("input extensions must be unique")
        return normalized


class ResolutionReductionSettings(SettingsModel):
    enabled: bool = True
    daytime_max_long_edge_px: int = Field(default=768, gt=0)
    sample_frames: int = Field(default=5, gt=0, le=30)
    interval_sec: float = Field(default=1.0, gt=0, le=10)
    sample_width: int = Field(default=160, gt=0, le=1920)
    sample_height: int = Field(default=90, gt=0, le=1080)
    saturation_threshold: float = Field(default=20.0, ge=0, le=255)
    dark_luminance_threshold: int = Field(default=40, ge=0, le=255)
    dark_ratio_threshold: float = Field(default=0.20, ge=0, le=1)


class FrameSettings(SettingsModel):
    interval_sec: float = Field(default=1.0, gt=0)
    max_long_edge_px: int = Field(default=1280, gt=0)
    jpeg_quality: int = Field(default=85, ge=1, le=100)
    ffmpeg_timeout_sec: float = Field(default=900.0, gt=0)
    resolution_reduction: ResolutionReductionSettings = Field(
        default_factory=ResolutionReductionSettings
    )

    @model_validator(mode="after")
    def daytime_resolution_cannot_exceed_default(self) -> FrameSettings:
        if (
            self.resolution_reduction.daytime_max_long_edge_px
            > self.max_long_edge_px
        ):
            raise ValueError(
                "daytime_max_long_edge_px cannot exceed max_long_edge_px"
            )
        return self


class RetrySettings(SettingsModel):
    max_attempts: int = Field(default=4, ge=1)


class PricingSettings(SettingsModel):
    input_per_million_tokens: float | None = Field(default=None, ge=0)
    output_per_million_tokens: float | None = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"


class GenAISettings(SettingsModel):
    provider: NonEmpty = "openai"
    model: NonEmpty = "gpt-5.6-luna"
    api_key: SecretStr | None = Field(default=None, repr=False)
    max_images_per_request: Literal["auto"] | int = "auto"
    max_inline_image_bytes: int = Field(default=13_000_000, gt=0)
    chunk_overlap_frames: int = Field(default=2, ge=0)
    request_timeout_sec: float = Field(default=180.0, gt=0)
    max_output_tokens: int = Field(default=8192, gt=0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)

    @field_validator("max_images_per_request", mode="before")
    @classmethod
    def validate_image_limit(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip().lower()
            if stripped == "auto":
                return "auto"
            if stripped.isdecimal():
                value = int(stripped)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("max_images_per_request must be 'auto' or a positive integer")
        return value

    @property
    def timeout_sec(self) -> float:
        """Compatibility name used by provider adapters."""

        return self.request_timeout_sec


class PromptSettings(SettingsModel):
    event: Path = Path("prompts/event_observation_v1.txt")
    event_synthesis: Path = Path("prompts/event_synthesis_v1.txt")
    daily_report: Path = Path("prompts/daily_report_v1.txt")
    event_version: VersionIdentifier = "event_observation_v1"
    event_synthesis_version: VersionIdentifier = "event_synthesis_v1"
    daily_report_version: VersionIdentifier = "daily_report_v1"


class ReportSettings(SettingsModel):
    # The PoC contract requires all three canonical renderings. Keeping JSON
    # mandatory also guarantees the daily narrative cache artifact exists.
    json_output: Literal[True] = Field(
        default=True, validation_alias="json", serialization_alias="json"
    )
    markdown_output: Literal[True] = Field(
        default=True, validation_alias="markdown", serialization_alias="markdown"
    )
    html_output: Literal[True] = Field(
        default=True, validation_alias="html", serialization_alias="html"
    )
    language: Literal["ja"] = "ja"

    # Compatibility accessors keep orchestration code concise while avoiding a
    # Pydantic field named ``json``, which conflicts with BaseModel's legacy
    # serialization method.
    @property
    def json(self) -> bool:
        return self.json_output

    @property
    def markdown(self) -> bool:
        return self.markdown_output

    @property
    def html(self) -> bool:
        return self.html_output


class ProcessingSettings(SettingsModel):
    continue_on_error: bool = True


class AppSettings(SettingsModel):
    timezone: Literal["Asia/Tokyo"] = "Asia/Tokyo"
    paths: PathsSettings = Field(default_factory=PathsSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    input: InputSettings = Field(default_factory=InputSettings)
    frames: FrameSettings = Field(default_factory=FrameSettings)
    genai: GenAISettings = Field(default_factory=GenAISettings)
    prompts: PromptSettings = Field(default_factory=PromptSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)



def _substitute_env(value: Any, environ: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group("name")
            if name in environ:
                return environ[name]
            default = match.group("default")
            if default is not None:
                return default
            raise SettingsError(f"environment variable {name!r} is required by the YAML file")

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_substitute_env(item, environ) for item in value]
    if isinstance(value, dict):
        return {key: _substitute_env(item, environ) for key, item in value.items()}
    return value


def _parse_env_value(value: str) -> Any:
    """Use YAML scalar syntax for booleans, numbers, lists, and dictionaries."""

    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise SettingsError(f"invalid environment override value: {value!r}") from exc
    return value if parsed is None and value.strip() else parsed


def _deep_set(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    if not path or any(not part for part in path):
        raise SettingsError("invalid empty environment override path")
    cursor = target
    for part in path[:-1]:
        existing = cursor.get(part)
        if existing is None:
            existing = {}
            cursor[part] = existing
        if not isinstance(existing, dict):
            raise SettingsError(f"cannot apply nested override below {part!r}")
        cursor = existing
    cursor[path[-1]] = value


_DIRECT_OVERRIDES: dict[str, tuple[str, ...]] = {
    "GENAI_PROVIDER": ("genai", "provider"),
    "GENAI_MODEL": ("genai", "model"),
    "OPENAI_API_KEY": ("genai", "api_key"),
    "GENAI_MAX_IMAGES_PER_REQUEST": ("genai", "max_images_per_request"),
}


def _apply_env_overrides(data: dict[str, Any], environ: Mapping[str, str]) -> None:
    for variable, path in _DIRECT_OVERRIDES.items():
        if variable not in environ:
            continue
        value: Any = environ[variable]
        # API keys are opaque strings and must not be interpreted as YAML.
        if variable != "OPENAI_API_KEY":
            value = _parse_env_value(value)
        _deep_set(data, path, value)

    # Generic nested overrides use ANALYZER_SECTION__FIELD. Examples:
    # ANALYZER_FRAMES__INTERVAL_SEC=2 and ANALYZER_TIMEZONE=Asia/Tokyo.
    prefix = "ANALYZER_"
    for variable, raw_value in environ.items():
        if not variable.startswith(prefix) or variable == "ANALYZER_CONFIG":
            continue
        key = variable[len(prefix):]
        path = tuple(part.lower() for part in key.split("__"))
        is_secret = path == ("genai", "api_key")
        value = raw_value if is_secret else _parse_env_value(raw_value)
        _deep_set(data, path, value)


def load_settings(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppSettings:
    """Load YAML, expand ``${NAME}``, then apply environment overrides.

    ``ANALYZER_CONFIG`` may point to the YAML file when ``path`` is omitted.
    If neither is supplied, the built-in validated defaults are used.
    """

    environment = os.environ if environ is None else environ
    config_path = path or environment.get("ANALYZER_CONFIG")
    data: dict[str, Any] = {}
    if config_path is not None:
        source = Path(config_path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SettingsError(f"cannot read configuration file {source}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise SettingsError(f"invalid YAML in configuration file {source}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise SettingsError("the YAML document root must be a mapping")
        data = _substitute_env(raw, environment)

    _apply_env_overrides(data, environment)
    return AppSettings.model_validate(data)


# Conventional aliases for integration code and external users.
Settings = AppSettings
Config = AppSettings
load_config = load_settings


__all__ = [
    "AppSettings",
    "Config",
    "FrameSettings",
    "GenAISettings",
    "InputSettings",
    "PathsSettings",
    "ProcessingSettings",
    "PromptSettings",
    "ReportSettings",
    "ResolutionReductionSettings",
    "RetrySettings",
    "ScheduleSettings",
    "Settings",
    "SettingsError",
    "load_config",
    "load_settings",
]

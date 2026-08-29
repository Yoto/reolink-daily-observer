"""Typed YAML configuration with explicit environment-variable overrides."""

from __future__ import annotations

import logging
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


LOGGER = logging.getLogger(__name__)


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


class BatchSettings(SettingsModel):
    """Deferred bulk submission for the image-heavy event observation stage.

    A day of recordings is a natural batch: every clip is independent, and the
    daily run already looks at yesterday. Trading same-minute latency for the
    provider's bulk discount therefore costs the schedule nothing, so this is
    on by default and can be turned off per run with ``--sync``.
    """

    enabled: bool = True
    completion_window: Literal["24h"] = "24h"
    poll_interval_sec: float = Field(default=30.0, gt=0, le=3600)
    # Slightly beyond the completion window, so a batch that is finishing right
    # at the deadline is still collected instead of abandoned one minute early.
    max_wait_sec: float = Field(default=90_000.0, gt=0)
    # Provider ceilings for a single batch; a day is split across as many
    # batches as these require.
    max_requests_per_batch: int = Field(default=50_000, ge=1, le=50_000)
    max_input_bytes: int = Field(default=180_000_000, gt=0, le=200_000_000)
    # Batch requests are billed at half the synchronous rate. This only scales
    # the locally computed estimate; it never changes what is sent.
    discount_ratio: float = Field(default=0.5, gt=0, le=1)
    # The uploaded JSONL is this run's scratch payload and, at roughly a
    # megabyte per analyzed second of video, by far the largest object the run
    # leaves in the project's file storage. Result files are always kept.
    delete_input_file: bool = True


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
    # Triage responds for every event of the day in a single response, so the
    # ceiling has to clear the busiest day, not the largest single event.
    max_output_tokens: int = Field(default=32768, gt=0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)
    batch: BatchSettings = Field(default_factory=BatchSettings)

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
    event: Path = Path("prompts/event_observation_v2.txt")
    event_synthesis: Path = Path("prompts/event_synthesis_v1.txt")
    triage: Path = Path("prompts/triage_v3.txt")
    daily_report: Path = Path("prompts/daily_report_v2.txt")
    event_version: VersionIdentifier = "event_observation_v2"
    event_synthesis_version: VersionIdentifier = "event_synthesis_v1"
    triage_version: VersionIdentifier = "triage_v3"
    daily_report_version: VersionIdentifier = "daily_report_v2"


class SceneSettings(SettingsModel):
    """Free-text description of what this camera normally sees.

    Every field is optional. Empty fields are omitted from prompts entirely so
    an unconfigured deployment behaves exactly like the previous version.
    The values describe a private household, so the real file is expected to
    live outside version control (see ``AppSettings.scene_file``).
    """

    location: str = ""
    camera_view: str = ""
    household: str = ""
    routine_patterns: tuple[NonEmpty, ...] = ()
    known_vehicles: tuple[NonEmpty, ...] = ()
    expected_visitors: tuple[NonEmpty, ...] = ()
    notes: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(
            self.location
            or self.camera_view
            or self.household
            or self.routine_patterns
            or self.known_vehicles
            or self.expected_visitors
            or self.notes
        )

    def prompt_payload(self) -> dict[str, Any]:
        """Return only the populated fields, for embedding in a prompt."""

        payload: dict[str, Any] = {}
        for key in ("location", "camera_view", "household", "notes"):
            value: str = getattr(self, key)
            if value:
                payload[key] = value
        for key in ("routine_patterns", "known_vehicles", "expected_visitors"):
            values: tuple[str, ...] = getattr(self, key)
            if values:
                payload[key] = list(values)
        return payload


class TriageSettings(SettingsModel):
    """Text-only judgement pass that runs between events and the report."""

    enabled: bool = True
    # Items at or above this score are surfaced as "requires a look".
    attention_score_threshold: int = Field(default=7, ge=0, le=10)
    # A non-null ``notable`` promotes an item regardless of its score.
    notable_always_attention: bool = True
    # When a documented routine explains an event and nothing was flagged as
    # notable, the score cannot exceed this. Ordinary household activity should
    # not reach the attention threshold just because a detail went unresolved.
    routine_explained_score_cap: int = Field(default=4, ge=0, le=10)
    # Fold events belonging to one real-world occurrence into a single item, so
    # one visit split across several clips is reported once.
    group_related_events: bool = True
    # Prior daily reports supplied as a baseline for novelty judgement.
    history_days: int = Field(default=14, ge=0, le=90)
    # Retry only judgements omitted from an otherwise schema-valid response.
    missing_retry_attempts: int = Field(default=1, ge=0, le=3)
    max_attention_items: int = Field(default=20, ge=1)


class ReportSettings(SettingsModel):
    language: Literal["ja"] = "ja"


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
    # Household details are sensitive, so the scene description is normally
    # kept in a separate, git-ignored file referenced by ``scene_file``.
    scene_file: Path | None = None
    scene: SceneSettings = Field(default_factory=SceneSettings)
    triage: TriageSettings = Field(default_factory=TriageSettings)
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
    "GENAI_BATCH_ENABLED": ("genai", "batch", "enabled"),
}

_NON_SETTINGS_ANALYZER_ENV_VARS = {
    "ANALYZER_CONFIG",
    # Consumed by scripts/container-entrypoint.sh before the Python process
    # starts; it controls filesystem permissions, not AppSettings.
    "ANALYZER_UMASK",
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
        if (
            not variable.startswith(prefix)
            or variable in _NON_SETTINGS_ANALYZER_ENV_VARS
        ):
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
    _merge_scene_file(data, environment, config_path)
    return AppSettings.model_validate(data)


def _merge_scene_file(
    data: dict[str, Any],
    environ: Mapping[str, str],
    config_path: str | os.PathLike[str] | None,
) -> None:
    """Merge an external scene YAML under the ``scene`` key.

    The scene description names a household and its habits, so it is kept in a
    separate file that never enters version control or a container image. An
    inline ``scene`` block still wins, field by field, so a deployment can
    override a single value without copying the whole file.
    """

    reference = data.get("scene_file")
    if reference is None:
        return
    if not isinstance(reference, (str, os.PathLike)):
        raise SettingsError("scene_file must be a path")
    source = Path(reference)
    if not source.is_absolute() and config_path is not None:
        # Resolve relative to the configuration file so a bind-mounted config
        # directory keeps working without absolute container paths.
        candidate = Path(config_path).parent / source
        if candidate.exists():
            source = candidate
    if not source.exists():
        # The scene file is git-ignored, so a fresh checkout will not have one.
        # Failing here would block the whole run over an optional quality input,
        # but staying silent would hide why judgement quality is poor.
        LOGGER.warning(
            "scene file %s not found; prompts will omit the scene description "
            "and resident/visitor judgement will be markedly less reliable",
            source,
        )
        return
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SettingsError(f"cannot read scene file {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SettingsError(f"invalid YAML in scene file {source}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SettingsError("the scene file root must be a mapping")
    # A scene file may either be a bare mapping of scene fields or wrap them in
    # a top-level ``scene`` key; accept both so the example file reads naturally.
    if set(raw) == {"scene"}:
        nested = raw["scene"]
        if not isinstance(nested, dict):
            raise SettingsError("the scene file 'scene' key must be a mapping")
        raw = nested
    resolved = _substitute_env(raw, environ)
    inline = data.get("scene") or {}
    if not isinstance(inline, dict):
        raise SettingsError("the inline scene block must be a mapping")
    data["scene"] = {**resolved, **inline}


# Conventional aliases for integration code and external users.
Settings = AppSettings
Config = AppSettings
load_config = load_settings


__all__ = [
    "AppSettings",
    "BatchSettings",
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
    "SceneSettings",
    "Settings",
    "SettingsError",
    "TriageSettings",
    "load_config",
    "load_settings",
]

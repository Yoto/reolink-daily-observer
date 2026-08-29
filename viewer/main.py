"""FastAPI application that renders analyzer-owned daily report JSON."""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from viewer.models import DailyReport


LOGGER = logging.getLogger(__name__)
PACKAGE_ROOT = Path(__file__).resolve().parent
MAX_REPORT_BYTES = 10 * 1024 * 1024
PERSON_TYPE_LABELS = {
    "resident": "住人と考えられる",
    "visitor": "来訪者と考えられる",
    "unknown": "判別できない",
    "not_applicable": "人物なし",
}
WEEKDAY_LABELS = ("月", "火", "水", "木", "金", "土", "日")


class ReportStore:
    """Read reports without following output-directory symlinks."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def available_dates(self) -> list[date]:
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return []
        available: list[date] = []
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                day = date.fromisoformat(entry.name)
                self.load(day)
            except (ValueError, FileNotFoundError, ValidationError, OSError):
                continue
            available.append(day)
        return sorted(available)

    def load(self, day: date) -> DailyReport:
        day_directory = self.root / day.isoformat()
        report_path = day_directory / "daily_report.json"
        try:
            directory_mode = day_directory.lstat().st_mode
            file_mode = report_path.lstat().st_mode
            if not stat.S_ISDIR(directory_mode) or not stat.S_ISREG(file_mode):
                raise FileNotFoundError(report_path)
            if report_path.stat().st_size > MAX_REPORT_BYTES:
                raise ValueError("daily report exceeds the viewer size limit")
            resolved = report_path.resolve(strict=True)
            if not resolved.is_relative_to(self.root):
                raise ValueError("daily report resolves outside the output root")
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            report = DailyReport.model_validate(payload)
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            raise ValueError("daily report is invalid") from exc
        if report.date != day:
            raise ValueError("daily report date does not match its directory")
        return report


def _video_path(
    report_date: date,
    source_file: str | None,
    layout: Literal["nested", "flat"],
) -> str | None:
    if source_file is None:
        return None
    path = PurePosixPath(source_file)
    nested_prefix = (
        str(report_date.year),
        f"{report_date.month:02d}",
        f"{report_date.day:02d}",
    )
    flat_prefix = report_date.isoformat()
    if len(path.parts) == 1:
        prefix = nested_prefix if layout == "nested" else (flat_prefix,)
        path = PurePosixPath(*prefix, path.name)
    elif path.parts[:-1] not in {nested_prefix, (flat_prefix,)}:
        return None
    return "/videos/" + quote(path.as_posix(), safe="/")


def _view_context(
    report: DailyReport,
    *,
    layout: Literal["nested", "flat"],
) -> dict[str, object]:
    payload = report.model_dump(mode="python")
    weekday = WEEKDAY_LABELS[report.date.weekday()]
    payload["date_ja"] = (
        f"{report.date.year}年{report.date.month}月{report.date.day}日（{weekday}）"
    )
    for collection in ("attention_items", "representative_events"):
        for item in payload[collection]:
            timestamp = item["recording_time"]
            item["recording_time_display"] = (
                timestamp.strftime("%H:%M") if timestamp is not None else None
            )
            item["video_url"] = _video_path(report.date, item["source_file"], layout)
    for item in payload["attention_items"]:
        item["person_type_label"] = PERSON_TYPE_LABELS.get(
            item["person_type"], item["person_type"]
        )
    attention_event_ids = {
        item["event_id"] for item in payload["attention_items"]
    }
    payload["family_scenes"] = [
        item
        for item in payload["representative_events"]
        if item["event_id"] not in attention_event_ids
    ][:3]
    payload["family_comment"] = _family_comment(report)
    for failure in payload["processing_summary"]["failures"]:
        timestamp = failure["recording_time"]
        failure["recording_time_display"] = (
            timestamp.strftime("%H:%M") if timestamp is not None else None
        )
    return payload


def _family_comment(report: DailyReport) -> str:
    """Build a calm closing line without inventing facts absent from the report."""

    if report.recurring_patterns:
        return report.recurring_patterns[0]
    if report.attention_items:
        return "お時間のあるときに、確認をお願いしたい動画をご覧ください。"
    if report.event_count == 0:
        return "今日はカメラものんびりできた一日だったようです。"
    if report.event_count >= 8:
        return "今日はカメラの前も、少しにぎやかな一日だったようです。"
    return "今日もいつもの景色を、静かに見守りました。"


def create_app(
    *,
    output_root: Path | None = None,
    camera_date_layout: Literal["nested", "flat"] | None = None,
) -> FastAPI:
    root = output_root or Path(os.environ.get("REPORT_OUTPUT_DIR", "/data/output"))
    raw_layout = camera_date_layout or os.environ.get("CAMERA_DATE_LAYOUT", "nested")
    if raw_layout not in {"nested", "flat"}:
        raise RuntimeError("CAMERA_DATE_LAYOUT must be 'nested' or 'flat'")
    layout: Literal["nested", "flat"] = raw_layout
    store = ReportStore(root)
    templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")

    application = FastAPI(
        title="Reolink Daily Report Viewer",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.mount(
        "/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static"
    )

    @application.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/", include_in_schema=False)
    def latest_report() -> RedirectResponse:
        dates = store.available_dates()
        if not dates:
            raise HTTPException(status_code=404, detail="No daily reports are available")
        return RedirectResponse(f"/report/{dates[-1].isoformat()}", status_code=302)

    @application.get("/report", include_in_schema=False)
    def select_report(
        report_date: Annotated[date, Query(alias="date")],
    ) -> RedirectResponse:
        return RedirectResponse(f"/report/{report_date.isoformat()}", status_code=302)

    @application.get("/report/details", include_in_schema=False)
    def select_report_details(
        report_date: Annotated[date, Query(alias="date")],
    ) -> RedirectResponse:
        return RedirectResponse(
            f"/report/{report_date.isoformat()}/details", status_code=302
        )

    def render_report(request: Request, report_date: date, template: str):
        try:
            report = store.load(report_date)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Daily report not found") from exc
        except ValueError as exc:
            LOGGER.exception("refusing invalid daily report date=%s", report_date)
            raise HTTPException(status_code=500, detail="Daily report is invalid") from exc

        dates = store.available_dates()
        position = dates.index(report_date)
        previous_date = dates[position - 1] if position > 0 else None
        next_date = dates[position + 1] if position + 1 < len(dates) else None
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={
                "report": _view_context(report, layout=layout),
                "previous_date": previous_date,
                "next_date": next_date,
            },
        )

    @application.get("/report/{report_date}", include_in_schema=False)
    def show_report(request: Request, report_date: date):
        return render_report(request, report_date, "report.html")

    @application.get("/report/{report_date}/details", include_in_schema=False)
    def show_report_details(request: Request, report_date: date):
        return render_report(request, report_date, "report_detail.html")

    return application


app = create_app()

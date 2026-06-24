#!/usr/bin/env python3
"""Local web version of Air Trip Admin."""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import (
    AppSettings,
    DB_PATH,
    DEFAULT_FLIGHT_PROVIDER,
    DEFAULT_LANGUAGE,
    FlightStatusResult,
    LinkItem,
    OpenSkyFlightProvider,
    PlaceholderScheduleValidationProvider,
    ChecklistItem,
    TimingStep,
    Trip,
    TripStore,
    arrival_timing_schedule,
    checklist_progress,
    departure_timing_schedule,
    estimated_home_arrival,
    estimated_safe_leave_home,
    format_cost,
    format_minutes,
    multiline_or_none,
    parse_datetime,
    parse_minutes,
    parse_timing_steps_text,
    split_items,
    t,
    total_trip_cost,
    value_or_none,
)


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
SETTINGS = AppSettings.load()
LANGUAGE = SETTINGS.language if SETTINGS.language in {"en", "es", "de"} else DEFAULT_LANGUAGE

app = FastAPI(title="Air Trip Admin Web")
store = TripStore(DB_PATH)
flight_provider = None
flight_status_cache: dict[int, FlightStatusResult] = {}


def _build_flight_provider() -> object:
    provider_name = os.environ.get(
        "FLIGHT_VALIDATION_PROVIDER",
        SETTINGS.flight_provider or DEFAULT_FLIGHT_PROVIDER,
    ).strip().lower()
    if provider_name in ("opensky", ""):
        return OpenSkyFlightProvider()
    if provider_name in ("placeholder", "schedule-placeholder"):
        return PlaceholderScheduleValidationProvider()
    return PlaceholderScheduleValidationProvider()


flight_provider = _build_flight_provider()


def _texts() -> dict[str, str]:
    return {
        "t_new": t(LANGUAGE, "new"),
        "t_refresh": t(LANGUAGE, "refresh"),
        "t_trip_list": "Trips",
        "t_trip_details": t(LANGUAGE, "trip_details"),
        "t_select_trip": t(LANGUAGE, "select_trip_first"),
        "t_no_trips": t(LANGUAGE, "no_trips_found"),
        "t_no_departure": "no departure date",
        "t_edit": t(LANGUAGE, "edit"),
        "t_delete": t(LANGUAGE, "delete"),
        "t_delete_confirm_prefix": "Delete \"",
        "t_delete_confirm_suffix": "\"? This cannot be undone.",
        "t_refresh_status": t(LANGUAGE, "flight_status"),
        "t_create_trip": t(LANGUAGE, "create_trip"),
        "t_edit_trip": "Edit trip #",
        "t_fix_errors": "Fix the following:",
        "t_save": t(LANGUAGE, "save"),
        "t_cancel": t(LANGUAGE, "cancel"),
        "t_trip_title": t(LANGUAGE, "trip_title"),
        "t_departure_airport": t(LANGUAGE, "departure_airport"),
        "t_arrival_airport": t(LANGUAGE, "arrival_airport"),
        "t_departure_datetime": t(LANGUAGE, "departure_datetime", format="YYYY-MM-DD HH:MM"),
        "t_flight_arrival_time": t(LANGUAGE, "flight_arrival_time", format="YYYY-MM-DD HH:MM"),
        "t_passenger_name": t(LANGUAGE, "passenger_name"),
        "t_airline_code": t(LANGUAGE, "airline_code"),
        "t_flight_number": t(LANGUAGE, "flight_number"),
        "t_airline": t(LANGUAGE, "airline"),
        "t_booking_reference": t(LANGUAGE, "booking_reference"),
        "t_ticket_number": t(LANGUAGE, "ticket_number"),
        "t_ticket_cost": t(LANGUAGE, "ticket_cost"),
        "t_seat": t(LANGUAGE, "seat"),
        "t_cabin_class": t(LANGUAGE, "cabin_class"),
        "t_checkin_done": t(LANGUAGE, "checkin_completed"),
        "t_done": t(LANGUAGE, "done_label"),
        "t_ticket_notes": t(LANGUAGE, "ticket_notes"),
        "t_documentation_required": t(LANGUAGE, "documentation_required"),
        "t_documents_to_carry": t(LANGUAGE, "documents_to_carry"),
        "t_clothes_items": t(LANGUAGE, "clothes_items"),
        "t_electronics_items": t(LANGUAGE, "electronics_items"),
        "t_health_items": t(LANGUAGE, "health_items"),
        "t_other_items": t(LANGUAGE, "other_items"),
        "t_general_notes": t(LANGUAGE, "general_notes"),
        "t_timing_steps": t(LANGUAGE, "timing_steps"),
        "t_timing_hint": t(LANGUAGE, "timing_steps_hint"),
        "t_arrival_timing_steps": t(LANGUAGE, "arrival_timing_steps"),
        "t_arrival_timing_hint": t(LANGUAGE, "arrival_timing_steps_hint"),
        "t_checklist_items_label": t(LANGUAGE, "checklist_items_label"),
        "t_checklist_items_hint": t(LANGUAGE, "checklist_items_hint"),
        "t_links_of_interest_label": t(LANGUAGE, "links_of_interest_label"),
        "t_links_of_interest_hint": t(LANGUAGE, "links_of_interest_hint"),
        "t_step_name": t(LANGUAGE, "step_name_label"),
        "t_step_minutes": t(LANGUAGE, "step_minutes_label"),
        "t_step_cost": t(LANGUAGE, "step_cost_label"),
        "t_remove": "Remove",
        "t_add_row": "Add row",
        "t_checklist_item_name_label": t(LANGUAGE, "checklist_item_name_label"),
        "t_checklist_item_done_label": t(LANGUAGE, "checklist_item_done_label"),
        "t_link_name_label": t(LANGUAGE, "link_name_label"),
        "t_link_url_label": t(LANGUAGE, "link_url_label"),
        "t_save": t(LANGUAGE, "save"),
    }


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _set_language(value: str | None) -> str:
    if value in {"en", "es", "de"}:
        return value
    return LANGUAGE


def _rows_from_trip(trip: Trip) -> dict[str, list[dict[str, Any]]]:
    complete, total = checklist_progress(trip)
    safe_leave_home, timing_total_minutes = estimated_safe_leave_home(trip)
    estimated_home_arrive, arrival_total_minutes = estimated_home_arrival(trip)
    departure_schedule = departure_timing_schedule(trip)
    arrival_schedule = arrival_timing_schedule(trip)
    trip_total_cost = total_trip_cost(trip)

    sections: list[dict[str, Any]] = [
        {
            "title": t(LANGUAGE, "overview_panel_title"),
            "rows": [
                {"label": t(LANGUAGE, "trip_label"), "value": value_or_none(trip.title)},
                {"label": t(LANGUAGE, "route_label"), "value": f"{value_or_none(trip.departure_airport)} -> {value_or_none(trip.arrival_airport)}"},
                {"label": t(LANGUAGE, "departure_label"), "value": value_or_none(trip.departure_datetime)},
                {"label": t(LANGUAGE, "flight_arrival_time_label"), "value": value_or_none(trip.flight_arrival_time)},
                {"label": t(LANGUAGE, "created_at_label"), "value": value_or_none(trip.created_at)},
                {"label": t(LANGUAGE, "updated_at_label"), "value": value_or_none(trip.updated_at)},
            ],
        },
        {
            "title": t(LANGUAGE, "ticket_panel_title"),
            "rows": [
                {"label": t(LANGUAGE, "passenger_label"), "value": value_or_none(trip.passenger_name)},
                {"label": t(LANGUAGE, "checkin_label"), "value": t(LANGUAGE, "done_label") if trip.checkin_done else t(LANGUAGE, "pending_label")},
                {"label": t(LANGUAGE, "flight_label"), "value": value_or_none(f"{trip.airline_code}{trip.flight_number}".strip())},
                {"label": t(LANGUAGE, "airline"), "value": value_or_none(trip.airline)},
                {"label": t(LANGUAGE, "booking_reference"), "value": value_or_none(trip.booking_reference)},
                {"label": t(LANGUAGE, "ticket_number"), "value": value_or_none(trip.ticket_number)},
                {"label": t(LANGUAGE, "ticket_cost"), "value": value_or_none(trip.ticket_cost)},
                {"label": t(LANGUAGE, "total_cost_label"), "value": format_cost(trip_total_cost) if trip_total_cost is not None else "(none)"},
            ],
        },
        {
            "title": t(LANGUAGE, "docs_panel_title"),
            "rows": [
                {"label": t(LANGUAGE, "required_documentation_label"), "value": multiline_or_none(trip.documentation_required)},
                {"label": t(LANGUAGE, "documents_to_carry"), "value": multiline_or_none(trip.documents_to_carry)},
            ],
        },
        {
            "title": t(LANGUAGE, "timing_panel_title"),
            "rows": [
                {"label": t(LANGUAGE, "estimated_safe_leave_home"), "value": safe_leave_home or t(LANGUAGE, "timing_estimate_unavailable")},
                {"label": t(LANGUAGE, "total_timing_buffer_label"), "value": format_minutes(timing_total_minutes) if timing_total_minutes is not None else "(none)"},
                {"label": t(LANGUAGE, "arrival_total_timing_label"), "value": format_minutes(arrival_total_minutes) if arrival_total_minutes is not None else "(none)"},
                {"label": t(LANGUAGE, "estimated_home_arrival"), "value": estimated_home_arrive or t(LANGUAGE, "arrival_timing_estimate_unavailable")},
            ],
        },
        {
            "title": t(LANGUAGE, "packing_panel_title"),
            "rows": [
                {"label": t(LANGUAGE, "clothes_label"), "value": multiline_or_none(trip.clothes_items)},
                {"label": t(LANGUAGE, "electronics_label"), "value": multiline_or_none(trip.electronics_items)},
                {"label": t(LANGUAGE, "health_label"), "value": multiline_or_none(trip.health_items)},
                {"label": t(LANGUAGE, "other_items_label"), "value": multiline_or_none(trip.other_items)},
                {"label": t(LANGUAGE, "packing_snapshot_label"), "value": f"{t(LANGUAGE, 'clothes_label')} {len(trip.clothes_items)} | {t(LANGUAGE, 'electronics_label')} {len(trip.electronics_items)} | {t(LANGUAGE, 'health_label')} {len(trip.health_items)} | {t(LANGUAGE, 'other_items_label')} {len(trip.other_items)}"},
            ],
        },
        {
            "title": t(LANGUAGE, "checklist_panel_title"),
            "rows": [
                {"label": t(LANGUAGE, "checklist_progress_label"), "value": f"{complete}/{total} completed"},
                *[{"label": item.name, "value": t(LANGUAGE, "done_label") if item.done else t(LANGUAGE, "pending_label")} for item in trip.checklist_items],
            ],
        },
        {
            "title": t(LANGUAGE, "links_of_interest_label"),
            "rows": [
                {"label": item.name.strip() or t(LANGUAGE, "unnamed_link_label"), "value": item.url.strip() or "(none)", "link_url": item.url.strip() or None}
                for item in trip.links_of_interest
            ] or [{"label": t(LANGUAGE, "links_of_interest_label"), "value": "(none)"}],
        },
    ]

    flight_lines = []
    cached = flight_status_cache.get(trip.id or -1)
    if cached is not None:
        flight_lines.extend(cached.lines)
    elif trip.id is None:
        flight_lines.append(t(LANGUAGE, "save_trip_before_status"))
    else:
        flight_lines.extend(
            [
                f"{t(LANGUAGE, 'provider_label')}: {getattr(flight_provider, 'provider_name', 'unknown')}",
                f"{t(LANGUAGE, 'lookup_key_label')}: {value_or_none(f'{trip.airline_code}{trip.flight_number}'.strip())}",
                t(LANGUAGE, "press_status_hint"),
            ]
        )

    sections.append(
        {
            "title": t(LANGUAGE, "flight_panel_title"),
            "rows": [{"label": f"{index + 1}", "value": line} for index, line in enumerate(flight_lines)] or [{"label": t(LANGUAGE, "result_label"), "value": "(none)"}],
        }
    )
    return {"sections": sections}


def _trip_form_context(trip: Trip) -> dict[str, Any]:
    return {
        "trip": trip,
        "checklist_items": trip.checklist_items or [ChecklistItem(name="", done=False)],
        "timing_steps": trip.timing_steps or [TimingStep(name="", minutes=0, cost="")],
        "arrival_timing_steps": trip.arrival_timing_steps or [TimingStep(name="", minutes=0, cost="")],
        "links_of_interest": trip.links_of_interest or [LinkItem(name="", url="")],
        "errors": [],
    }


def _parse_checklist(form: Any) -> tuple[list[ChecklistItem], list[str]]:
    names = form.getlist("checklist_name")
    dones = form.getlist("checklist_done")
    items: list[ChecklistItem] = []
    errors: list[str] = []
    for index, name in enumerate(names):
        label = str(name).strip()
        done = str(dones[index]).strip() in {"1", "true", "True", "on"} if index < len(dones) else False
        if not label and not done:
            continue
        if not label:
            errors.append(f"Checklist item row {index + 1} requires a name")
            continue
        items.append(ChecklistItem(label, done))
    return items, errors


def _parse_links(form: Any) -> tuple[list[LinkItem], list[str]]:
    names = form.getlist("link_name")
    urls = form.getlist("link_url")
    items: list[LinkItem] = []
    errors: list[str] = []
    for index, name in enumerate(names):
        label = str(name).strip()
        url = str(urls[index]).strip() if index < len(urls) else ""
        if not label and not url:
            continue
        if not url:
            errors.append(f"Link row {index + 1} requires a URL")
            continue
        items.append(LinkItem(label, url))
    return items, errors


def _parse_timing_steps(form: Any, prefix: str) -> tuple[list[TimingStep], list[str]]:
    names = form.getlist(f"{prefix}_name")
    minutes = form.getlist(f"{prefix}_minutes")
    costs = form.getlist(f"{prefix}_cost")
    raw_lines: list[str] = []
    for index, name in enumerate(names):
        label = str(name).strip()
        minute_value = str(minutes[index]).strip() if index < len(minutes) else ""
        cost = str(costs[index]).strip() if index < len(costs) else ""
        if not label and not minute_value and not cost:
            continue
        raw_lines.append(f"{label} | {minute_value} | {cost}")
    return parse_timing_steps_text("\n".join(raw_lines))


def _trip_from_form(form: Any, *, existing: Trip | None = None) -> tuple[Trip | None, list[str]]:
    errors: list[str] = []

    def req(name: str, label: str) -> str:
        value = str(form.get(name, "")).strip()
        if not value:
            errors.append(f"{label} is required")
        return value

    def opt_dt(name: str, label: str, required: bool = False) -> str:
        value = str(form.get(name, "")).strip()
        if not value:
            if required:
                errors.append(f"{label} is required")
            return ""
        try:
            return parse_datetime(value)
        except ValueError:
            errors.append(f"{label} must use YYYY-MM-DD HH:MM")
            return value

    trip = Trip(
        id=existing.id if existing else None,
        title=req("title", "Trip title"),
        departure_airport=req("departure_airport", "Departure airport"),
        arrival_airport=req("arrival_airport", "Arrival airport"),
        departure_datetime=opt_dt("departure_datetime", "Departure date/time", required=True),
        flight_arrival_time=opt_dt("flight_arrival_time", "Flight arrival time"),
        passenger_name=str(form.get("passenger_name", "")).strip(),
        checkin_done=str(form.get("checkin_done", "")).strip() in {"1", "true", "True", "on"},
        checklist_items=[],
        airline_code=str(form.get("airline_code", "")).strip().upper(),
        flight_number=str(form.get("flight_number", "")).strip().upper(),
        airline=str(form.get("airline", "")).strip(),
        booking_reference=str(form.get("booking_reference", "")).strip(),
        ticket_number=str(form.get("ticket_number", "")).strip(),
        ticket_cost=str(form.get("ticket_cost", "")).strip(),
        seat=str(form.get("seat", "")).strip(),
        cabin_class=str(form.get("cabin_class", "")).strip(),
        ticket_notes=str(form.get("ticket_notes", "")).strip(),
        documentation_required=split_items(str(form.get("documentation_required", "")).strip()),
        timing_steps=[],
        arrival_timing_steps=[],
        clothes_items=split_items(str(form.get("clothes_items", "")).strip()),
        electronics_items=split_items(str(form.get("electronics_items", "")).strip()),
        health_items=split_items(str(form.get("health_items", "")).strip()),
        documents_to_carry=split_items(str(form.get("documents_to_carry", "")).strip()),
        other_items=split_items(str(form.get("other_items", "")).strip()),
        links_of_interest=[],
        general_notes=str(form.get("general_notes", "")).strip(),
        created_at=existing.created_at if existing else "",
        updated_at=existing.updated_at if existing else "",
    )
    departure_steps, departure_errors = _parse_timing_steps(form, "timing")
    arrival_steps, arrival_errors = _parse_timing_steps(form, "arrival")
    checklist_items, checklist_errors = _parse_checklist(form)
    links_of_interest, link_errors = _parse_links(form)
    errors.extend(departure_errors)
    errors.extend(arrival_errors)
    errors.extend(checklist_errors)
    errors.extend(link_errors)
    trip.timing_steps = departure_steps
    trip.arrival_timing_steps = arrival_steps
    trip.checklist_items = checklist_items
    trip.links_of_interest = links_of_interest
    if errors:
        return None, errors
    return trip, []


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, trip_id: int | None = None, notice: str | None = None) -> HTMLResponse:
    trips = store.list_trips()
    selected = None
    if trip_id is not None:
        selected = store.get_trip(trip_id)
    elif trips:
        selected = trips[0]
    context = {
        "request": request,
        "language": LANGUAGE,
        "trips": trips,
        "selected_trip": selected,
        "detail": _rows_from_trip(selected) if selected else {"sections": []},
        "notice": notice,
        **_texts(),
    }
    if selected is not None:
        context["delete_confirm"] = f'Delete "{selected.title}"? This cannot be undone.'
    return TEMPLATES.TemplateResponse("index.html", context)


@app.get("/trip/new", response_class=HTMLResponse)
async def new_trip(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        "form.html",
        {"request": request, "language": LANGUAGE, "trip": Trip(), **_trip_form_context(Trip()), **_texts(), "form_title": "Create trip"},
    )


@app.get("/trip/{trip_id}/edit", response_class=HTMLResponse)
async def edit_trip(request: Request, trip_id: int) -> HTMLResponse:
    trip = store.get_trip(trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return TEMPLATES.TemplateResponse("form.html", {"request": request, "language": LANGUAGE, "trip": trip, **_trip_form_context(trip), **_texts(), "form_title": f"Edit trip #{trip.id}"})


@app.get("/trip/{trip_id}", response_class=HTMLResponse)
async def show_trip(trip_id: int) -> RedirectResponse:
    return _redirect(f"/?trip_id={trip_id}")


@app.post("/trip", response_model=None)
async def create_trip(request: Request):
    form = await request.form()
    trip, errors = _trip_from_form(form)
    if trip is None:
        return TEMPLATES.TemplateResponse("form.html", {"request": request, "language": LANGUAGE, "trip": Trip(), **_trip_form_context(Trip()), **_texts(), "errors": errors, "form_title": "Create trip"}, status_code=400)
    trip.id = None
    trip_id = store.save_trip(trip)
    return _redirect(f"/?trip_id={trip_id}&notice={t(LANGUAGE, 'trip_saved')}")


@app.post("/trip/{trip_id}", response_model=None)
async def update_trip(request: Request, trip_id: int):
    existing = store.get_trip(trip_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    form = await request.form()
    trip, errors = _trip_from_form(form, existing=existing)
    if trip is None:
        return TEMPLATES.TemplateResponse("form.html", {"request": request, "language": LANGUAGE, "trip": existing, **_trip_form_context(existing), **_texts(), "errors": errors, "form_title": f"Edit trip #{trip_id}"}, status_code=400)
    trip.id = trip_id
    store.save_trip(trip)
    return _redirect(f"/?trip_id={trip_id}&notice={t(LANGUAGE, 'trip_saved')}")


@app.post("/trip/{trip_id}/delete", response_model=None)
async def delete_trip(trip_id: int):
    store.delete_trip(trip_id)
    return _redirect(f"/?notice={t(LANGUAGE, 'trip_deleted')}")


@app.post("/trip/{trip_id}/status", response_model=None)
async def refresh_status(trip_id: int):
    trip = store.get_trip(trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    try:
        result = await asyncio.to_thread(flight_provider.fetch, trip)
    except Exception as exc:  # pragma: no cover - network/provider failure path
        result = FlightStatusResult(False, f"Error: {exc}", [str(exc)])
    flight_status_cache[trip_id] = result
    return _redirect(f"/?trip_id={trip_id}&notice={t(LANGUAGE, 'flight_status')}")


def main() -> None:
    import uvicorn

    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()

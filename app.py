#!/usr/bin/env python3
"""Air trip admin TUI built with Textual.

The app provides:
- a left frame with a list of trips
- a right frame with notebook tabs for categorized trip data
- create / edit / delete actions in modal screens

Data is stored in a local SQLite database next to this file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Protocol

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabPane,
    TabbedContent,
    TextArea,
)


DB_PATH = Path(__file__).with_name("trips.db")
SETTINGS_PATH = Path(__file__).with_name("app_settings.json")
TRANSLATIONS_PATH = Path(__file__).with_name("translations.json")
DT_FORMAT = "%Y-%m-%d %H:%M"
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
DEFAULT_FLIGHT_PROVIDER = "opensky"
DEFAULT_LANGUAGE = "en"
DB_SCHEMA_VERSION = 6


def load_translations() -> dict[str, dict[str, str]]:
    if not TRANSLATIONS_PATH.exists():
        raise FileNotFoundError(f"Missing translations file: {TRANSLATIONS_PATH}")
    try:
        payload = json.loads(TRANSLATIONS_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Unable to read translations file: {TRANSLATIONS_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in translations file: {TRANSLATIONS_PATH}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Translations file must contain a JSON object: {TRANSLATIONS_PATH}")
    translations: dict[str, dict[str, str]] = {}
    for language, values in payload.items():
        if not isinstance(language, str) or not isinstance(values, dict):
            continue
        translations[language] = {
            str(key): str(value) for key, value in values.items()
        }
    if DEFAULT_LANGUAGE not in translations:
        raise RuntimeError(f"Translations file must include the '{DEFAULT_LANGUAGE}' language: {TRANSLATIONS_PATH}")
    return translations


TRANSLATIONS = load_translations()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_datetime(value: str) -> str:
    datetime.strptime(value, DT_FORMAT)
    return value


def parse_datetime_value(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, DT_FORMAT)
    except ValueError:
        return None


def departure_date(value: str) -> str:
    if not value:
        return ""
    return value.split(" ", 1)[0]


def split_items(raw: str) -> list[str]:
    items = [item.strip() for item in re.split(r"[\n,]+", raw) if item.strip()]
    return items


def join_items(items: Iterable[str]) -> str:
    return ", ".join(item for item in items if item)


def value_or_none(value: str | None) -> str:
    return value.strip() if value else "(none)"


def parse_cost(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", value.strip())
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def total_trip_cost(trip: "Trip") -> float | None:
    amounts = [parse_cost(trip.ticket_cost)]
    amounts.extend(parse_cost(step.cost) for step in trip.timing_steps)
    amounts.extend(parse_cost(step.cost) for step in trip.arrival_timing_steps)
    valid_amounts = [amount for amount in amounts if amount is not None]
    if not valid_amounts:
        return None
    return sum(valid_amounts)


def format_cost(value: float | None) -> str:
    if value is None:
        return "(none)"
    return f"{value:.2f}"


def parse_minutes(value: str | None) -> int | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        minutes = int(raw)
    except ValueError:
        return None
    if minutes < 0:
        return None
    return minutes


def format_minutes(value: int | None) -> str:
    if value is None:
        return "(none)"
    return f"{value} min"


@dataclass
class TimingStep:
    name: str
    minutes: int
    cost: str = ""


@dataclass
class ChecklistItem:
    name: str
    done: bool = False


def parse_timing_steps_text(raw: str) -> tuple[list[TimingStep], list[str]]:
    steps: list[TimingStep] = []
    errors: list[str] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2 or len(parts) > 3:
            errors.append(f"Timing step line {line_number} must use 'Name | minutes | cost'")
            continue
        name_part = parts[0]
        minutes_part = parts[1]
        cost_part = parts[2] if len(parts) == 3 else ""
        if not name_part:
            errors.append(f"Timing step line {line_number} is missing a name")
            continue
        minutes = parse_minutes(minutes_part)
        if minutes is None:
            errors.append(f"Timing step line {line_number} must use whole minutes")
            continue
        steps.append(TimingStep(name=name_part, minutes=minutes, cost=cost_part))
    return steps, errors


def timing_steps_to_text(steps: Iterable["TimingStep"]) -> str:
    return "\n".join(f"{step.name} | {step.minutes} | {step.cost}".rstrip() for step in steps if step.name.strip())


def timing_steps_from_json(raw: str | None) -> list[TimingStep]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    steps: list[TimingStep] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        minutes = parse_minutes(str(item.get("minutes") or ""))
        if not name or minutes is None:
            continue
        steps.append(TimingStep(name=name, minutes=minutes, cost=str(item.get("cost") or "").strip()))
    return steps


def timing_steps_to_json(steps: Iterable["TimingStep"]) -> str:
    return json.dumps([asdict(step) for step in steps], ensure_ascii=False)


def checklist_items_from_json(raw: str | None) -> list[ChecklistItem]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items: list[ChecklistItem] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        items.append(ChecklistItem(name=name, done=bool(item.get("done"))))
    return items


def checklist_items_to_json(items: Iterable["ChecklistItem"]) -> str:
    return json.dumps([asdict(item) for item in items], ensure_ascii=False)


def legacy_checklist_items_from_row(row: sqlite3.Row) -> list[ChecklistItem]:
    available_columns = set(row.keys())
    legacy_fields = [
        ("checklist_ticket_ready", "Ticket ready"),
        ("checklist_checkin_done", "Check-in completed"),
        ("checklist_documentation_ready", "Documentation ready"),
        ("checklist_bags_ready", "Bags ready"),
        ("checklist_packing_done", "Packing completed"),
    ]
    items: list[ChecklistItem] = []
    for field_name, label in legacy_fields:
        if field_name not in available_columns:
            continue
        items.append(ChecklistItem(name=label, done=bool(row[field_name])))
    return items


def legacy_timing_steps_from_row(row: sqlite3.Row) -> list[TimingStep]:
    available_columns = set(row.keys())
    legacy_fields = [
        ("home_to_train_station_minutes", "Home to train station"),
        ("train_time_minutes", "Train time"),
        ("train_station_to_airport_minutes", "Train station to airport"),
        ("traffic_buffer_minutes", "Traffic / schedule buffer"),
        ("airport_arrival_buffer_minutes", "Airport arrival buffer"),
        ("checkin_buffer_minutes", "Check-in / bag drop buffer"),
        ("security_buffer_minutes", "Security buffer"),
        ("terminal_walk_buffer_minutes", "Terminal walk buffer"),
        ("boarding_buffer_minutes", "Boarding buffer"),
    ]
    steps: list[TimingStep] = []
    for field_name, label in legacy_fields:
        if field_name not in available_columns:
            continue
        minutes = parse_minutes(str(row[field_name] or ""))
        if minutes is None:
            continue
        steps.append(TimingStep(label, minutes, ""))
    return steps


def estimated_safe_leave_home(trip: "Trip") -> tuple[str | None, int | None]:
    departure_at = parse_datetime_value(trip.departure_datetime)
    total_minutes = sum(step.minutes for step in trip.timing_steps if step.minutes >= 0)
    if departure_at is None or total_minutes <= 0:
        return None, None
    safe_leave_at = departure_at - timedelta(minutes=total_minutes)
    return safe_leave_at.strftime(DT_FORMAT), total_minutes


def estimated_home_arrival(trip: "Trip") -> tuple[str | None, int | None]:
    arrival_at = parse_datetime_value(trip.flight_arrival_time)
    total_minutes = sum(step.minutes for step in trip.arrival_timing_steps if step.minutes >= 0)
    if arrival_at is None or total_minutes <= 0:
        return None, None
    home_arrival_at = arrival_at + timedelta(minutes=total_minutes)
    return home_arrival_at.strftime(DT_FORMAT), total_minutes


def departure_timing_schedule(trip: "Trip") -> list[tuple[TimingStep, str]]:
    departure_at = parse_datetime_value(trip.departure_datetime)
    safe_leave_at, _ = estimated_safe_leave_home(trip)
    current_at = parse_datetime_value(safe_leave_at or "")
    if departure_at is None or current_at is None:
        return [(step, "") for step in trip.timing_steps]
    scheduled: list[tuple[TimingStep, str]] = []
    for step in trip.timing_steps:
        start_at = current_at
        current_at += timedelta(minutes=step.minutes)
        scheduled.append((step, f"{start_at.strftime('%H:%M')} -> {current_at.strftime('%H:%M')}"))
    return scheduled


def arrival_timing_schedule(trip: "Trip") -> list[tuple[TimingStep, str]]:
    current_at = parse_datetime_value(trip.flight_arrival_time)
    if current_at is None:
        return [(step, "") for step in trip.arrival_timing_steps]
    scheduled: list[tuple[TimingStep, str]] = []
    for step in trip.arrival_timing_steps:
        start_at = current_at
        current_at += timedelta(minutes=step.minutes)
        scheduled.append((step, f"{start_at.strftime('%H:%M')} -> {current_at.strftime('%H:%M')}"))
    return scheduled


def checklist_progress(trip: "Trip") -> tuple[int, int]:
    total = len(trip.checklist_items)
    done = sum(1 for item in trip.checklist_items if item.done)
    return done, total


def multiline_or_none(items: Iterable[str]) -> str:
    cleaned = [item.strip() for item in items if item and item.strip()]
    return "\n".join(cleaned) if cleaned else "(none)"


def bool_cell(language: str, value: bool) -> Text:
    return Text(
        t(language, "yes_label") if value else t(language, "no_label"),
        style="bold green" if value else "bold red",
    )


def status_cell(language: str, done: bool) -> Text:
    return Text(
        t(language, "done_label") if done else t(language, "pending_label"),
        style="bold green" if done else "bold yellow",
    )


def warning_cell(message: str) -> Text:
    return Text(message, style="bold yellow")


def right_text(value: str, style: str = "") -> Text:
    return Text(value, style=style, justify="right")


def severity_cell(message: str) -> Text:
    lowered = message.lower()
    if any(token in lowered for token in ("error", "failed", "missing", "cancel", "exception", "denied")):
        return Text(message, style="bold red")
    if any(token in lowered for token in ("warning", "unavailable", "no ", "cannot", "invalid", "pending")):
        return Text(message, style="bold yellow")
    if any(token in lowered for token in ("updated", "ok", "done", "completed", "ready", "success")):
        return Text(message, style="bold green")
    return Text(message)


def t(language: str, key: str, **kwargs: object) -> str:
    template = TRANSLATIONS.get(language, TRANSLATIONS[DEFAULT_LANGUAGE]).get(
        key,
        TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key),
    )
    return template.format(**kwargs)


@dataclass
class FlightStatusResult:
    ok: bool
    summary: str
    lines: list[str]
    fetched_at: str = field(default_factory=now_iso)


class FlightValidationProvider(Protocol):
    provider_name: str

    def fetch(self, trip: "Trip") -> FlightStatusResult:
        ...


@dataclass
class AppSettings:
    language: str = DEFAULT_LANGUAGE
    flight_provider: str = DEFAULT_FLIGHT_PROVIDER

    @classmethod
    def load(cls) -> "AppSettings":
        if not SETTINGS_PATH.exists():
            return cls()
        try:
            payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        language = str(payload.get("language") or DEFAULT_LANGUAGE)
        if language not in TRANSLATIONS:
            language = DEFAULT_LANGUAGE
        flight_provider = str(payload.get("flight_provider") or DEFAULT_FLIGHT_PROVIDER).strip().lower()
        if flight_provider not in {"opensky", "placeholder", "schedule-placeholder"}:
            flight_provider = DEFAULT_FLIGHT_PROVIDER
        return cls(language=language, flight_provider=flight_provider)

    def save(self) -> None:
        SETTINGS_PATH.write_text(
            json.dumps(
                {
                    "language": self.language,
                    "flight_provider": self.flight_provider,
                },
                ensure_ascii=True,
                indent=2,
            ),
            encoding="utf-8",
        )


@dataclass
class Trip:
    id: int | None = None
    title: str = ""
    departure_airport: str = ""
    arrival_airport: str = ""
    departure_datetime: str = ""
    flight_arrival_time: str = ""
    passenger_name: str = ""
    checkin_done: bool = False
    checklist_items: list[ChecklistItem] = field(default_factory=list)
    airline_code: str = ""
    flight_number: str = ""
    airline: str = ""
    booking_reference: str = ""
    ticket_number: str = ""
    ticket_cost: str = ""
    seat: str = ""
    cabin_class: str = ""
    ticket_notes: str = ""
    documentation_required: list[str] = field(default_factory=list)
    timing_steps: list[TimingStep] = field(default_factory=list)
    arrival_timing_steps: list[TimingStep] = field(default_factory=list)
    clothes_items: list[str] = field(default_factory=list)
    electronics_items: list[str] = field(default_factory=list)
    health_items: list[str] = field(default_factory=list)
    documents_to_carry: list[str] = field(default_factory=list)
    other_items: list[str] = field(default_factory=list)
    general_notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Trip":
        return cls(
            id=row["id"],
            title=row["title"],
            departure_airport=row["departure_airport"],
            arrival_airport=row["arrival_airport"],
            departure_datetime=row["departure_datetime"],
            flight_arrival_time=row["flight_arrival_time"] if "flight_arrival_time" in row.keys() else "",
            passenger_name=row["passenger_name"],
            checkin_done=bool(row["checkin_done"]),
            checklist_items=checklist_items_from_json(row["checklist_items"]) or legacy_checklist_items_from_row(row),
            airline_code=row["airline_code"],
            flight_number=row["flight_number"],
            airline=row["airline"],
            booking_reference=row["booking_reference"],
            ticket_number=row["ticket_number"],
            ticket_cost=row["ticket_cost"],
            seat=row["seat"],
            cabin_class=row["cabin_class"],
            ticket_notes=row["ticket_notes"],
            documentation_required=from_json(row["documentation_required"]),
            timing_steps=timing_steps_from_json(row["timing_steps"]) or legacy_timing_steps_from_row(row),
            arrival_timing_steps=timing_steps_from_json(row["arrival_timing_steps"] if "arrival_timing_steps" in row.keys() else ""),
            clothes_items=from_json(row["clothes_items"]),
            electronics_items=from_json(row["electronics_items"]),
            health_items=from_json(row["health_items"]),
            documents_to_carry=from_json(row["documents_to_carry"]),
            other_items=from_json(row["other_items"]),
            general_notes=row["general_notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def pack(self) -> dict[str, object]:
        return {
            "title": self.title,
            "departure_airport": self.departure_airport,
            "arrival_airport": self.arrival_airport,
            "departure_datetime": self.departure_datetime,
            "flight_arrival_time": self.flight_arrival_time,
            "passenger_name": self.passenger_name,
            "checkin_done": int(self.checkin_done),
            "checklist_items": checklist_items_to_json(self.checklist_items),
            "airline_code": self.airline_code,
            "flight_number": self.flight_number,
            "airline": self.airline,
            "booking_reference": self.booking_reference,
            "ticket_number": self.ticket_number,
            "ticket_cost": self.ticket_cost,
            "seat": self.seat,
            "cabin_class": self.cabin_class,
            "ticket_notes": self.ticket_notes,
            "documentation_required": to_json(self.documentation_required),
            "timing_steps": timing_steps_to_json(self.timing_steps),
            "arrival_timing_steps": timing_steps_to_json(self.arrival_timing_steps),
            "clothes_items": to_json(self.clothes_items),
            "electronics_items": to_json(self.electronics_items),
            "health_items": to_json(self.health_items),
            "documents_to_carry": to_json(self.documents_to_carry),
            "other_items": to_json(self.other_items),
            "general_notes": self.general_notes,
            "updated_at": self.updated_at or now_iso(),
        }

    def duplicate(self) -> "Trip":
        return Trip(
            title=f"{self.title} (copy)" if self.title else "Trip copy",
            departure_airport=self.departure_airport,
            arrival_airport=self.arrival_airport,
            departure_datetime=self.departure_datetime,
            flight_arrival_time=self.flight_arrival_time,
            passenger_name=self.passenger_name,
            checkin_done=self.checkin_done,
            checklist_items=[ChecklistItem(item.name, item.done) for item in self.checklist_items],
            airline_code=self.airline_code,
            flight_number=self.flight_number,
            airline=self.airline,
            booking_reference=self.booking_reference,
            ticket_number=self.ticket_number,
            ticket_cost=self.ticket_cost,
            seat=self.seat,
            cabin_class=self.cabin_class,
            ticket_notes=self.ticket_notes,
            documentation_required=list(self.documentation_required),
            timing_steps=[TimingStep(step.name, step.minutes, step.cost) for step in self.timing_steps],
            arrival_timing_steps=[TimingStep(step.name, step.minutes, step.cost) for step in self.arrival_timing_steps],
            clothes_items=list(self.clothes_items),
            electronics_items=list(self.electronics_items),
            health_items=list(self.health_items),
            documents_to_carry=list(self.documents_to_carry),
            other_items=list(self.other_items),
            general_notes=self.general_notes,
            updated_at=now_iso(),
        )


def to_json(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False)


def from_json(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


class TripStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        if not self._table_exists("trips"):
            self._create_trips_table("trips")
        self._ensure_column("passenger_name", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("checkin_done", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("flight_arrival_time", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("checklist_items", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("airline_code", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("flight_number", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("ticket_cost", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("timing_steps", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("arrival_timing_steps", "TEXT NOT NULL DEFAULT '[]'")
        self._migrate_legacy_timing_steps()
        self._migrate_legacy_checklist_items()
        self._drop_legacy_transport_columns()
        self._drop_legacy_checklist_columns()
        self.conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
        self.conn.commit()

    def _create_trips_table(self, table_name: str) -> None:
        self.conn.execute(
            f"""
            CREATE TABLE {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                departure_airport TEXT NOT NULL,
                arrival_airport TEXT NOT NULL,
                departure_datetime TEXT NOT NULL,
                flight_arrival_time TEXT NOT NULL DEFAULT '',
                passenger_name TEXT NOT NULL DEFAULT '',
                checkin_done INTEGER NOT NULL DEFAULT 0,
                checklist_items TEXT NOT NULL DEFAULT '[]',
                airline_code TEXT NOT NULL DEFAULT '',
                flight_number TEXT NOT NULL DEFAULT '',
                airline TEXT NOT NULL DEFAULT '',
                booking_reference TEXT NOT NULL DEFAULT '',
                ticket_number TEXT NOT NULL DEFAULT '',
                ticket_cost TEXT NOT NULL DEFAULT '',
                seat TEXT NOT NULL DEFAULT '',
                cabin_class TEXT NOT NULL DEFAULT '',
                ticket_notes TEXT NOT NULL DEFAULT '',
                documentation_required TEXT NOT NULL DEFAULT '[]',
                timing_steps TEXT NOT NULL DEFAULT '[]',
                arrival_timing_steps TEXT NOT NULL DEFAULT '[]',
                clothes_items TEXT NOT NULL DEFAULT '[]',
                electronics_items TEXT NOT NULL DEFAULT '[]',
                health_items TEXT NOT NULL DEFAULT '[]',
                documents_to_carry TEXT NOT NULL DEFAULT '[]',
                other_items TEXT NOT NULL DEFAULT '[]',
                general_notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _table_exists(self, table_name: str) -> bool:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _ensure_column(self, column_name: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(trips)").fetchall()
        }
        if column_name not in columns:
            self.conn.execute(f"ALTER TABLE trips ADD COLUMN {column_name} {definition}")

    def _migrate_legacy_timing_steps(self) -> None:
        rows = self.conn.execute("SELECT * FROM trips").fetchall()
        for row in rows:
            existing_steps = timing_steps_from_json(row["timing_steps"]) if "timing_steps" in row.keys() else []
            if existing_steps:
                continue
            legacy_steps = legacy_timing_steps_from_row(row)
            if not legacy_steps:
                continue
            self.conn.execute(
                "UPDATE trips SET timing_steps = ? WHERE id = ?",
                (timing_steps_to_json(legacy_steps), row["id"]),
            )

    def _migrate_legacy_checklist_items(self) -> None:
        rows = self.conn.execute("SELECT * FROM trips").fetchall()
        for row in rows:
            existing_items = checklist_items_from_json(row["checklist_items"]) if "checklist_items" in row.keys() else []
            if existing_items:
                continue
            legacy_items = legacy_checklist_items_from_row(row)
            if not legacy_items:
                continue
            self.conn.execute(
                "UPDATE trips SET checklist_items = ? WHERE id = ?",
                (checklist_items_to_json(legacy_items), row["id"]),
            )

    def _drop_legacy_transport_columns(self) -> None:
        legacy_columns = {
            "checklist_transport_booked",
            "outbound_transport_mode",
            "outbound_transport_provider",
            "outbound_transport_cost",
            "outbound_leave_home",
            "outbound_arrive_airport",
            "outbound_transport_notes",
        }
        current_columns = [row["name"] for row in self.conn.execute("PRAGMA table_info(trips)").fetchall()]
        if not legacy_columns.intersection(current_columns):
            return
        with self.conn:
            self._create_trips_table("trips__new")
            kept_columns = [
                "id",
                "title",
                "departure_airport",
                "arrival_airport",
                "departure_datetime",
                "flight_arrival_time",
                "passenger_name",
                "checkin_done",
                "checklist_items",
                "airline_code",
                "flight_number",
                "airline",
                "booking_reference",
                "ticket_number",
                "ticket_cost",
                "seat",
                "cabin_class",
                "ticket_notes",
                "documentation_required",
                "timing_steps",
                "arrival_timing_steps",
                "clothes_items",
                "electronics_items",
                "health_items",
                "documents_to_carry",
                "other_items",
                "general_notes",
                "created_at",
                "updated_at",
            ]
            columns_sql = ", ".join(kept_columns)
            self.conn.execute(
                f"INSERT INTO trips__new ({columns_sql}) SELECT {columns_sql} FROM trips"
            )
            self.conn.execute("DROP TABLE trips")
            self.conn.execute("ALTER TABLE trips__new RENAME TO trips")

    def _drop_legacy_checklist_columns(self) -> None:
        legacy_columns = {
            "checklist_ticket_ready",
            "checklist_checkin_done",
            "checklist_documentation_ready",
            "checklist_bags_ready",
            "checklist_packing_done",
        }
        current_columns = [row["name"] for row in self.conn.execute("PRAGMA table_info(trips)").fetchall()]
        if not legacy_columns.intersection(current_columns):
            return
        with self.conn:
            self._create_trips_table("trips__new")
            kept_columns = [
                "id",
                "title",
                "departure_airport",
                "arrival_airport",
                "departure_datetime",
                "flight_arrival_time",
                "passenger_name",
                "checkin_done",
                "checklist_items",
                "airline_code",
                "flight_number",
                "airline",
                "booking_reference",
                "ticket_number",
                "ticket_cost",
                "seat",
                "cabin_class",
                "ticket_notes",
                "documentation_required",
                "timing_steps",
                "arrival_timing_steps",
                "clothes_items",
                "electronics_items",
                "health_items",
                "documents_to_carry",
                "other_items",
                "general_notes",
                "created_at",
                "updated_at",
            ]
            columns_sql = ", ".join(kept_columns)
            self.conn.execute(
                f"INSERT INTO trips__new ({columns_sql}) SELECT {columns_sql} FROM trips"
            )
            self.conn.execute("DROP TABLE trips")
            self.conn.execute("ALTER TABLE trips__new RENAME TO trips")

    def list_trips(self, query: str | None = None) -> list[Trip]:
        if query:
            rows = self.conn.execute(
                """
                SELECT *
                FROM trips
                WHERE title LIKE ? OR departure_airport LIKE ? OR arrival_airport LIKE ? OR booking_reference LIKE ?
                ORDER BY departure_datetime DESC, id DESC
                """,
                tuple(f"%{query}%" for _ in range(4)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trips ORDER BY departure_datetime DESC, id DESC"
            ).fetchall()
        return [Trip.from_row(row) for row in rows]

    def get_trip(self, trip_id: int) -> Trip | None:
        row = self.conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        return Trip.from_row(row) if row else None

    def save_trip(self, trip: Trip) -> int:
        data = trip.pack()
        if trip.id is None:
            created_at = now_iso()
            data["created_at"] = created_at
            data["updated_at"] = created_at
            columns = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            cur = self.conn.execute(
                f"INSERT INTO trips ({columns}) VALUES ({placeholders})",
                tuple(data.values()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        self.conn.execute(
            """
            UPDATE trips
            SET title = :title,
                departure_airport = :departure_airport,
                arrival_airport = :arrival_airport,
                departure_datetime = :departure_datetime,
                flight_arrival_time = :flight_arrival_time,
                passenger_name = :passenger_name,
                checkin_done = :checkin_done,
                checklist_items = :checklist_items,
                airline_code = :airline_code,
                flight_number = :flight_number,
                airline = :airline,
                booking_reference = :booking_reference,
                ticket_number = :ticket_number,
                ticket_cost = :ticket_cost,
                seat = :seat,
                cabin_class = :cabin_class,
                ticket_notes = :ticket_notes,
                documentation_required = :documentation_required,
                timing_steps = :timing_steps,
                arrival_timing_steps = :arrival_timing_steps,
                clothes_items = :clothes_items,
                electronics_items = :electronics_items,
                health_items = :health_items,
                documents_to_carry = :documents_to_carry,
                other_items = :other_items,
                general_notes = :general_notes,
                updated_at = :updated_at
            WHERE id = :id
            """,
            {**data, "id": trip.id},
        )
        self.conn.commit()
        return trip.id

    def delete_trip(self, trip_id: int) -> None:
        self.conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
        self.conn.commit()


class OpenSkyFlightProvider:
    provider_name = "OpenSky"

    @staticmethod
    def build_callsign(trip: Trip) -> str:
        if not trip.airline_code.strip():
            raise ValueError("Airline code is required for live status lookup.")
        if not trip.flight_number.strip():
            raise ValueError("Flight number is required for live status lookup.")
        return f"{trip.airline_code.strip().upper()}{trip.flight_number.strip().upper()}"

    def _fetch_token(self) -> str:
        client_id = os.environ.get("OPENSKY_CLIENT_ID", "").strip()
        client_secret = os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise ValueError("OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET must be configured.")

        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            OPENSKY_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise ValueError("OpenSky token response did not contain an access token.")
        return token

    def _parse_state_result(self, trip: Trip, payload: dict[str, object]) -> FlightStatusResult:
        states = payload.get("states")
        if not isinstance(states, list):
            return FlightStatusResult(False, "Unexpected response", ["OpenSky returned no state list."])

        callsign = self.build_callsign(trip)
        matching_state: list[object] | None = None
        for state in states:
            if not isinstance(state, list) or len(state) < 17:
                continue
            state_callsign = str(state[1] or "").strip().upper()
            if state_callsign == callsign:
                matching_state = state
                break

        if matching_state is None:
            return FlightStatusResult(
                False,
                "No live state found",
                [
                    "No current OpenSky state matched this callsign.",
                    "This usually means the flight is not currently airborne/on-network, the callsign differs, or the trip date is not current.",
                    f"Requested callsign: {callsign}",
                    f"Trip departure date: {departure_date(trip.departure_datetime) or '(missing)'}",
                ],
            )

        on_ground = bool(matching_state[8])
        velocity_mps = matching_state[9]
        altitude_m = matching_state[13] if matching_state[13] is not None else matching_state[7]
        summary = "on ground" if on_ground else "airborne"
        lines = [
            f"Provider: {self.provider_name}",
            f"Flight: {callsign}",
            f"Status: {summary}",
            f"Origin country: {value_or_none(str(matching_state[2] or ''))}",
            f"Longitude: {value_or_none(str(matching_state[5] or ''))}",
            f"Latitude: {value_or_none(str(matching_state[6] or ''))}",
            f"Altitude meters: {value_or_none(str(altitude_m or ''))}",
            f"Ground speed m/s: {value_or_none(str(velocity_mps or ''))}",
            f"Heading: {value_or_none(str(matching_state[10] or ''))}",
            f"Vertical rate m/s: {value_or_none(str(matching_state[11] or ''))}",
            f"Squawk: {value_or_none(str(matching_state[14] or ''))}",
            f"Last contact unix time: {value_or_none(str(matching_state[4] or ''))}",
            "Provider note: OpenSky free REST data is strongest for current live aircraft state, not full commercial schedule status.",
        ]
        return FlightStatusResult(True, summary, lines)

    def fetch(self, trip: Trip) -> FlightStatusResult:
        try:
            token = self._fetch_token()
            self.build_callsign(trip)
            request = urllib.request.Request(
                OPENSKY_STATES_URL,
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return self._parse_state_result(trip, payload)
        except ValueError as exc:
            return FlightStatusResult(False, "Configuration error", [str(exc)])
        except urllib.error.HTTPError as exc:
            return FlightStatusResult(False, "HTTP error", [f"{exc.code} {exc.reason}"])
        except urllib.error.URLError as exc:
            return FlightStatusResult(False, "Network error", [str(exc.reason)])
        except json.JSONDecodeError:
            return FlightStatusResult(False, "Parse error", ["Provider response was not valid JSON."])
        except Exception as exc:
            return FlightStatusResult(False, "Unexpected error", [str(exc)])


class PlaceholderScheduleValidationProvider:
    provider_name = "Placeholder schedule validator"

    def fetch(self, trip: Trip) -> FlightStatusResult:
        return FlightStatusResult(
            False,
            "Provider not implemented",
            [
                f"Selected provider: {self.provider_name}",
                "This app is now provider-pluggable.",
                "Add a paid schedule-validation API here to validate flight number, date/time, origin, and destination.",
                "Expected trip fields are already present: airline_code, flight_number, departure_datetime, departure_airport, arrival_airport.",
            ],
        )


class TripListItem(ListItem):
    def __init__(self, trip: Trip) -> None:
        title = trip.title or "(untitled trip)"
        summary = (
            f"[b]{title}[/b]\n"
            f"{trip.departure_datetime or 'no departure date'} | {trip.departure_airport} -> {trip.arrival_airport}"
        )
        super().__init__(Static(summary, markup=True), id=f"trip-{trip.id}")
        self.trip_id = trip.id


class TimingStepListItem(ListItem):
    def __init__(self, step: TimingStep, index: int) -> None:
        cost = step.cost.strip() or "(none)"
        line = f"{index + 1}. {step.name} | {step.minutes} min | {cost}"
        super().__init__(Static(line), id=f"timing-step-{index}")
        self.step_index = index


class ArrivalTimingStepListItem(ListItem):
    def __init__(self, step: TimingStep, index: int) -> None:
        cost = step.cost.strip() or "(none)"
        line = f"{index + 1}. {step.name} | {step.minutes} min | {cost}"
        super().__init__(Static(line), id=f"arrival-timing-step-{index}")
        self.step_index = index


class ChecklistItemListItem(ListItem):
    def __init__(self, item: ChecklistItem, index: int) -> None:
        mark = "[x]" if item.done else "[ ]"
        line = f"{index + 1}. {mark} {item.name}"
        super().__init__(Static(line), id=f"checklist-item-{index}")
        self.checklist_index = index


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, language: str, title: str, message: str) -> None:
        super().__init__()
        self.language = language
        self.title_text = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(id="confirm-card"):
            yield Static(self.title_text, classes="dialog-title")
            yield Static(self.message, classes="dialog-message")
            with Horizontal(classes="dialog-buttons"):
                yield Button(t(self.language, "cancel"), variant="default", id="cancel")
                yield Button(t(self.language, "delete"), variant="error", id="confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)


class InfoScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, language: str) -> None:
        super().__init__()
        self.language = language

    def compose(self) -> ComposeResult:
        with Container(id="confirm-card"):
            yield Static(t(self.language, "about_title"), classes="dialog-title")
            yield Static(t(self.language, "about_body"), classes="dialog-message")
            with Horizontal(classes="dialog-buttons"):
                yield Button(t(self.language, "close"), variant="primary", id="close")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class LanguageScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, language: str) -> None:
        super().__init__()
        self.language = language

    def compose(self) -> ComposeResult:
        with Container(id="confirm-card"):
            yield Static(t(self.language, "language_title"), classes="dialog-title")
            with Horizontal(classes="dialog-buttons"):
                yield Button("English", variant="primary", id="lang-en")
                yield Button("Español", variant="primary", id="lang-es")
                yield Button("Deutsch", variant="primary", id="lang-de")
                yield Button(t(self.language, "cancel"), variant="default", id="cancel")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self.dismiss(event.button.id.replace("lang-", ""))


class TripEditorScreen(ModalScreen[Trip | None]):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, language: str, trip: Trip | None = None) -> None:
        super().__init__()
        self.language = language
        self.trip = trip or Trip()
        self._timing_steps = [TimingStep(step.name, step.minutes, step.cost) for step in self.trip.timing_steps]
        self._selected_timing_step_index: int | None = 0 if self._timing_steps else None
        self._arrival_timing_steps = [TimingStep(step.name, step.minutes, step.cost) for step in self.trip.arrival_timing_steps]
        self._selected_arrival_timing_step_index: int | None = 0 if self._arrival_timing_steps else None
        self._checklist_items = [ChecklistItem(item.name, item.done) for item in self.trip.checklist_items]
        self._selected_checklist_item_index: int | None = 0 if self._checklist_items else None

    def compose(self) -> ComposeResult:
        with Container(id="editor-card"):
            yield Static(
                t(self.language, "create_trip")
                if self.trip.id is None
                else t(self.language, "edit_trip_title", id=self.trip.id),
                classes="dialog-title",
            )
            yield Static(t(self.language, "save_hint"), classes="dialog-message")
            with TabbedContent(id="editor-tabs"):
                with TabPane(t(self.language, "overview"), id="editor-overview"):
                    with VerticalScroll(classes="editor-tab"):
                        with Horizontal(classes="overview-route-row"):
                            with Vertical(classes="ticket-field-block"):
                                yield Label(f"{t(self.language, 'trip_title')}{t(self.language, 'required_suffix')}")
                                yield Input(value=self.trip.title or "", id="title", classes="editor-input")
                            with Vertical(classes="ticket-field-block"):
                                yield Label(f"{t(self.language, 'departure_airport')}{t(self.language, 'required_suffix')}")
                                yield Input(value=self.trip.departure_airport or "", id="departure_airport", classes="editor-input")
                            with Vertical(classes="ticket-field-block"):
                                yield Label(f"{t(self.language, 'arrival_airport')}{t(self.language, 'required_suffix')}")
                                yield Input(value=self.trip.arrival_airport or "", id="arrival_airport", classes="editor-input")
                        with Horizontal(classes="overview-time-row"):
                            with Vertical(classes="ticket-field-block"):
                                yield Label(f"{t(self.language, 'departure_datetime', format=DT_FORMAT)}{t(self.language, 'required_suffix')}")
                                yield Input(value=self.trip.departure_datetime or "", id="departure_datetime", classes="editor-input")
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "flight_arrival_time", format=DT_FORMAT))
                                yield Input(value=self.trip.flight_arrival_time or "", id="flight_arrival_time", classes="editor-input")
                with TabPane(t(self.language, "ticket"), id="editor-ticket"):
                    with VerticalScroll(classes="editor-tab"):
                        with Horizontal(classes="ticket-passenger-row"):
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "passenger_name"))
                                yield Input(value=self.trip.passenger_name or "", id="passenger_name", classes="editor-input")
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "checkin_completed"))
                                yield Checkbox("", value=self.trip.checkin_done, id="checkin_done", classes="editor-checkbox")
                        with Horizontal(classes="ticket-airline-row"):
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "airline_code"))
                                yield Input(value=self.trip.airline_code or "", id="airline_code", classes="editor-input")
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "flight_number"))
                                yield Input(value=self.trip.flight_number or "", id="flight_number", classes="editor-input")
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "airline"))
                                yield Input(value=self.trip.airline or "", id="airline", classes="editor-input")
                        with Horizontal(classes="ticket-booking-row"):
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "booking_reference"))
                                yield Input(value=self.trip.booking_reference or "", id="booking_reference", classes="editor-input")
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "ticket_number"))
                                yield Input(value=self.trip.ticket_number or "", id="ticket_number", classes="editor-input")
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "ticket_cost"))
                                yield Input(value=self.trip.ticket_cost or "", id="ticket_cost", classes="editor-input")
                        with Horizontal(classes="ticket-seat-row"):
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "seat"))
                                yield Input(value=self.trip.seat or "", id="seat", classes="editor-input")
                            with Vertical(classes="ticket-field-block"):
                                yield Label(t(self.language, "cabin_class"))
                                yield Input(value=self.trip.cabin_class or "", id="cabin_class", classes="editor-input")
                        yield from self._field(
                            "ticket_notes",
                            t(self.language, "ticket_notes"),
                            self.trip.ticket_notes,
                            widget="textarea",
                        )
                with TabPane(t(self.language, "docs"), id="editor-docs"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field(
                            "documentation_required",
                            t(self.language, "documentation_required"),
                            join_items(self.trip.documentation_required),
                        )
                        yield from self._field(
                            "documents_to_carry",
                            t(self.language, "documents_to_carry"),
                            join_items(self.trip.documents_to_carry),
                        )
                with TabPane(t(self.language, "timing"), id="editor-timing"):
                    with VerticalScroll(classes="editor-tab"):
                        with TabbedContent(id="editor-timing-subtabs"):
                            with TabPane(t(self.language, "departure_timing_section"), id="editor-timing-outbound"):
                                with VerticalScroll(classes="editor-tab"):
                                    yield Label(t(self.language, "timing_steps"))
                                    yield Static(t(self.language, "timing_steps_hint"), classes="dialog-message")
                                    with Horizontal(classes="timing-input-row"):
                                        yield Input(placeholder=t(self.language, "step_name_label"), id="timing-step-name", classes="editor-input")
                                        yield Input(placeholder=t(self.language, "step_minutes_label"), id="timing-step-minutes", classes="editor-input")
                                        yield Input(placeholder=t(self.language, "step_cost_label"), id="timing-step-cost", classes="editor-input")
                                    with Horizontal(classes="dialog-buttons"):
                                        yield Button(t(self.language, "add_step"), variant="primary", id="timing-add")
                                        yield Button(t(self.language, "update_step"), variant="default", id="timing-update")
                                        yield Button(t(self.language, "remove_step"), variant="warning", id="timing-remove")
                                        yield Button(t(self.language, "move_step_up"), variant="default", id="timing-up")
                                        yield Button(t(self.language, "move_step_down"), variant="default", id="timing-down")
                                    yield ListView(id="timing-steps-list")
                            with TabPane(t(self.language, "arrival_timing_section"), id="editor-timing-arrival"):
                                with VerticalScroll(classes="editor-tab"):
                                    yield Label(t(self.language, "arrival_timing_steps"))
                                    yield Static(t(self.language, "arrival_timing_steps_hint"), classes="dialog-message")
                                    with Horizontal(classes="timing-input-row"):
                                        yield Input(placeholder=t(self.language, "step_name_label"), id="arrival-timing-step-name", classes="editor-input")
                                        yield Input(placeholder=t(self.language, "step_minutes_label"), id="arrival-timing-step-minutes", classes="editor-input")
                                        yield Input(placeholder=t(self.language, "step_cost_label"), id="arrival-timing-step-cost", classes="editor-input")
                                    with Horizontal(classes="dialog-buttons"):
                                        yield Button(t(self.language, "add_arrival_step"), variant="primary", id="arrival-timing-add")
                                        yield Button(t(self.language, "update_arrival_step"), variant="default", id="arrival-timing-update")
                                        yield Button(t(self.language, "remove_arrival_step"), variant="warning", id="arrival-timing-remove")
                                        yield Button(t(self.language, "move_arrival_step_up"), variant="default", id="arrival-timing-up")
                                        yield Button(t(self.language, "move_arrival_step_down"), variant="default", id="arrival-timing-down")
                                    yield ListView(id="arrival-timing-steps-list")
                with TabPane(t(self.language, "packing"), id="editor-packing"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field(
                            "clothes_items",
                            t(self.language, "clothes_items"),
                            join_items(self.trip.clothes_items),
                        )
                        yield from self._field(
                            "electronics_items",
                            t(self.language, "electronics_items"),
                            join_items(self.trip.electronics_items),
                        )
                        yield from self._field(
                            "health_items",
                            t(self.language, "health_items"),
                            join_items(self.trip.health_items),
                        )
                        yield from self._field(
                            "other_items",
                            t(self.language, "other_items"),
                            join_items(self.trip.other_items),
                        )
                with TabPane(t(self.language, "checklist"), id="editor-checklist"):
                    with VerticalScroll(classes="editor-tab"):
                        yield Label(t(self.language, "checklist_items_label"))
                        yield Static(t(self.language, "checklist_items_hint"), classes="dialog-message")
                        with Horizontal(classes="checklist-input-row"):
                            yield Input(placeholder=t(self.language, "checklist_item_name_label"), id="checklist-item-name", classes="editor-input")
                            yield Checkbox(t(self.language, "checklist_item_done_label"), id="checklist-item-done", classes="editor-checkbox")
                        with Horizontal(classes="dialog-buttons"):
                            yield Button(t(self.language, "add_checklist_item"), variant="primary", id="checklist-add")
                            yield Button(t(self.language, "update_checklist_item"), variant="default", id="checklist-update")
                            yield Button(t(self.language, "remove_checklist_item"), variant="warning", id="checklist-remove")
                            yield Button(t(self.language, "move_checklist_item_up"), variant="default", id="checklist-up")
                            yield Button(t(self.language, "move_checklist_item_down"), variant="default", id="checklist-down")
                        yield ListView(id="checklist-items-list")
                with TabPane(t(self.language, "notes"), id="editor-notes"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field(
                            "general_notes",
                            t(self.language, "general_notes"),
                            self.trip.general_notes,
                            widget="textarea",
                        )
            yield Static("", id="editor-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button(t(self.language, "cancel"), variant="default", id="cancel")
                yield Button(t(self.language, "save"), variant="success", id="save")

    def _field(
        self,
        field_name: str,
        label: str,
        value: str | bool,
        *,
        required: bool = False,
        widget: str = "input",
    ) -> Iterable[object]:
        if widget == "checkbox":
            yield Checkbox(label, value=bool(value), id=field_name, classes="editor-checkbox")
        else:
            yield Label(f"{label}{t(self.language, 'required_suffix') if required else ''}")
        if widget == "textarea":
            yield TextArea(text=value or "", id=field_name, classes="editor-textarea")
        elif widget == "checkbox":
            return
        else:
            yield Input(value=value or "", id=field_name, classes="editor-input")

    def _get_input(self, field_name: str) -> str:
        return self.query_one(f"#{field_name}", Input).value.strip()

    def _get_textarea(self, field_name: str) -> str:
        return self.query_one(f"#{field_name}", TextArea).text.strip()

    def _get_checkbox(self, field_name: str) -> bool:
        return self.query_one(f"#{field_name}", Checkbox).value

    def _set_timing_inputs(self, name: str = "", minutes: str = "", cost: str = "") -> None:
        self.query_one("#timing-step-name", Input).value = name
        self.query_one("#timing-step-minutes", Input).value = minutes
        self.query_one("#timing-step-cost", Input).value = cost

    def _set_arrival_timing_inputs(self, name: str = "", minutes: str = "", cost: str = "") -> None:
        self.query_one("#arrival-timing-step-name", Input).value = name
        self.query_one("#arrival-timing-step-minutes", Input).value = minutes
        self.query_one("#arrival-timing-step-cost", Input).value = cost

    def _set_checklist_inputs(self, name: str = "", done: bool = False) -> None:
        self.query_one("#checklist-item-name", Input).value = name
        self.query_one("#checklist-item-done", Checkbox).value = done

    async def _refresh_timing_steps_list(self) -> None:
        timing_list = self.query_one("#timing-steps-list", ListView)
        await timing_list.clear()
        for index, step in enumerate(self._timing_steps):
            await timing_list.append(TimingStepListItem(step, index))
        if self._timing_steps and self._selected_timing_step_index is not None:
            self._selected_timing_step_index = max(0, min(self._selected_timing_step_index, len(self._timing_steps) - 1))
            timing_list.index = self._selected_timing_step_index
            step = self._timing_steps[self._selected_timing_step_index]
            self._set_timing_inputs(step.name, str(step.minutes), step.cost)
        else:
            self._selected_timing_step_index = None
            self._set_timing_inputs()

    async def _refresh_arrival_timing_steps_list(self) -> None:
        timing_list = self.query_one("#arrival-timing-steps-list", ListView)
        await timing_list.clear()
        for index, step in enumerate(self._arrival_timing_steps):
            await timing_list.append(ArrivalTimingStepListItem(step, index))
        if self._arrival_timing_steps and self._selected_arrival_timing_step_index is not None:
            self._selected_arrival_timing_step_index = max(0, min(self._selected_arrival_timing_step_index, len(self._arrival_timing_steps) - 1))
            timing_list.index = self._selected_arrival_timing_step_index
            step = self._arrival_timing_steps[self._selected_arrival_timing_step_index]
            self._set_arrival_timing_inputs(step.name, str(step.minutes), step.cost)
        else:
            self._selected_arrival_timing_step_index = None
            self._set_arrival_timing_inputs()

    async def _refresh_checklist_items_list(self) -> None:
        checklist_list = self.query_one("#checklist-items-list", ListView)
        await checklist_list.clear()
        for index, item in enumerate(self._checklist_items):
            await checklist_list.append(ChecklistItemListItem(item, index))
        if self._checklist_items and self._selected_checklist_item_index is not None:
            self._selected_checklist_item_index = max(0, min(self._selected_checklist_item_index, len(self._checklist_items) - 1))
            checklist_list.index = self._selected_checklist_item_index
            item = self._checklist_items[self._selected_checklist_item_index]
            self._set_checklist_inputs(item.name, item.done)
        else:
            self._selected_checklist_item_index = None
            self._set_checklist_inputs()

    def _set_status(self, message: str) -> None:
        self.query_one("#editor-status", Static).update(message)

    def _collect_trip(self) -> tuple[Trip | None, str | None]:
        errors: list[str] = []

        def required_text(field_name: str, label: str) -> str:
            value = self._get_input(field_name)
            if not value:
                errors.append(f"{label} is required")
            return value

        def optional_dt(field_name: str, label: str, required: bool = False) -> str:
            value = self._get_input(field_name)
            if not value:
                if required:
                    errors.append(f"{label} is required")
                return ""
            try:
                return parse_datetime(value)
            except ValueError:
                errors.append(f"{label} must use {DT_FORMAT}")
                return value

        trip = Trip(
            id=self.trip.id,
            title=required_text("title", "Trip title"),
            departure_airport=required_text("departure_airport", "Departure airport"),
            arrival_airport=required_text("arrival_airport", "Arrival airport"),
            departure_datetime=optional_dt("departure_datetime", "Departure date/time", required=True),
            flight_arrival_time=optional_dt("flight_arrival_time", "Flight arrival time"),
            passenger_name=self._get_input("passenger_name"),
            checkin_done=self._get_checkbox("checkin_done"),
            checklist_items=[ChecklistItem(item.name, item.done) for item in self._checklist_items],
            airline_code=self._get_input("airline_code").upper(),
            flight_number=self._get_input("flight_number").upper(),
            airline=self._get_input("airline"),
            booking_reference=self._get_input("booking_reference"),
            ticket_number=self._get_input("ticket_number"),
            ticket_cost=self._get_input("ticket_cost"),
            seat=self._get_input("seat"),
            cabin_class=self._get_input("cabin_class"),
            ticket_notes=self._get_textarea("ticket_notes"),
            documentation_required=split_items(self._get_input("documentation_required")),
            timing_steps=[TimingStep(step.name, step.minutes, step.cost) for step in self._timing_steps],
            arrival_timing_steps=[TimingStep(step.name, step.minutes, step.cost) for step in self._arrival_timing_steps],
            clothes_items=split_items(self._get_input("clothes_items")),
            electronics_items=split_items(self._get_input("electronics_items")),
            health_items=split_items(self._get_input("health_items")),
            documents_to_carry=split_items(self._get_input("documents_to_carry")),
            other_items=split_items(self._get_input("other_items")),
            general_notes=self._get_textarea("general_notes"),
            created_at=self.trip.created_at,
            updated_at=now_iso(),
        )

        if errors:
            return None, "; ".join(errors)
        return trip, None

    async def on_mount(self) -> None:
        await self._refresh_timing_steps_list()
        await self._refresh_arrival_timing_steps_list()
        await self._refresh_checklist_items_list()

    def _selected_timing_item(self, item: ListItem | None) -> None:
        if item is None:
            return
        step_index = getattr(item, "step_index", None)
        if step_index is None:
            return
        self._selected_timing_step_index = step_index
        step = self._timing_steps[step_index]
        self._set_timing_inputs(step.name, str(step.minutes), step.cost)

    def _selected_arrival_timing_item(self, item: ListItem | None) -> None:
        if item is None:
            return
        step_index = getattr(item, "step_index", None)
        if step_index is None:
            return
        self._selected_arrival_timing_step_index = step_index
        step = self._arrival_timing_steps[step_index]
        self._set_arrival_timing_inputs(step.name, str(step.minutes), step.cost)

    def _selected_checklist_item(self, item: ListItem | None) -> None:
        if item is None:
            return
        checklist_index = getattr(item, "checklist_index", None)
        if checklist_index is None:
            return
        self._selected_checklist_item_index = checklist_index
        checklist_item = self._checklist_items[checklist_index]
        self._set_checklist_inputs(checklist_item.name, checklist_item.done)

    async def _add_or_update_timing_step(self, *, update_existing: bool) -> None:
        name = self.query_one("#timing-step-name", Input).value.strip()
        minutes_raw = self.query_one("#timing-step-minutes", Input).value.strip()
        cost = self.query_one("#timing-step-cost", Input).value.strip()
        minutes = parse_minutes(minutes_raw)
        if not name or minutes is None:
            self._set_status("Timing step requires name and whole minutes.")
            return
        if update_existing:
            if self._selected_timing_step_index is None:
                self._set_status("Select a timing step first.")
                return
            self._timing_steps[self._selected_timing_step_index] = TimingStep(name, minutes, cost)
        else:
            self._timing_steps.append(TimingStep(name, minutes, cost))
            self._selected_timing_step_index = len(self._timing_steps) - 1
        await self._refresh_timing_steps_list()

    async def _remove_timing_step(self) -> None:
        if self._selected_timing_step_index is None:
            self._set_status("Select a timing step first.")
            return
        del self._timing_steps[self._selected_timing_step_index]
        if not self._timing_steps:
            self._selected_timing_step_index = None
        else:
            self._selected_timing_step_index = min(self._selected_timing_step_index, len(self._timing_steps) - 1)
        await self._refresh_timing_steps_list()

    async def _move_timing_step(self, delta: int) -> None:
        if self._selected_timing_step_index is None:
            self._set_status("Select a timing step first.")
            return
        new_index = self._selected_timing_step_index + delta
        if new_index < 0 or new_index >= len(self._timing_steps):
            return
        step = self._timing_steps.pop(self._selected_timing_step_index)
        self._timing_steps.insert(new_index, step)
        self._selected_timing_step_index = new_index
        await self._refresh_timing_steps_list()

    async def _add_or_update_arrival_timing_step(self, *, update_existing: bool) -> None:
        name = self.query_one("#arrival-timing-step-name", Input).value.strip()
        minutes_raw = self.query_one("#arrival-timing-step-minutes", Input).value.strip()
        cost = self.query_one("#arrival-timing-step-cost", Input).value.strip()
        minutes = parse_minutes(minutes_raw)
        if not name or minutes is None:
            self._set_status(t(self.language, "arrival_timing_step_requires_values"))
            return
        if update_existing:
            if self._selected_arrival_timing_step_index is None:
                self._set_status(t(self.language, "select_arrival_timing_step_first"))
                return
            self._arrival_timing_steps[self._selected_arrival_timing_step_index] = TimingStep(name, minutes, cost)
        else:
            self._arrival_timing_steps.append(TimingStep(name, minutes, cost))
            self._selected_arrival_timing_step_index = len(self._arrival_timing_steps) - 1
        await self._refresh_arrival_timing_steps_list()

    async def _remove_arrival_timing_step(self) -> None:
        if self._selected_arrival_timing_step_index is None:
            self._set_status(t(self.language, "select_arrival_timing_step_first"))
            return
        del self._arrival_timing_steps[self._selected_arrival_timing_step_index]
        if not self._arrival_timing_steps:
            self._selected_arrival_timing_step_index = None
        else:
            self._selected_arrival_timing_step_index = min(self._selected_arrival_timing_step_index, len(self._arrival_timing_steps) - 1)
        await self._refresh_arrival_timing_steps_list()

    async def _move_arrival_timing_step(self, delta: int) -> None:
        if self._selected_arrival_timing_step_index is None:
            self._set_status(t(self.language, "select_arrival_timing_step_first"))
            return
        new_index = self._selected_arrival_timing_step_index + delta
        if new_index < 0 or new_index >= len(self._arrival_timing_steps):
            return
        step = self._arrival_timing_steps.pop(self._selected_arrival_timing_step_index)
        self._arrival_timing_steps.insert(new_index, step)
        self._selected_arrival_timing_step_index = new_index
        await self._refresh_arrival_timing_steps_list()

    async def _add_or_update_checklist_item(self, *, update_existing: bool) -> None:
        name = self.query_one("#checklist-item-name", Input).value.strip()
        done = self.query_one("#checklist-item-done", Checkbox).value
        if not name:
            self._set_status(t(self.language, "checklist_item_requires_name"))
            return
        if update_existing:
            if self._selected_checklist_item_index is None:
                self._set_status(t(self.language, "select_checklist_item_first"))
                return
            self._checklist_items[self._selected_checklist_item_index] = ChecklistItem(name, done)
        else:
            self._checklist_items.append(ChecklistItem(name, done))
            self._selected_checklist_item_index = len(self._checklist_items) - 1
        await self._refresh_checklist_items_list()

    async def _remove_checklist_item(self) -> None:
        if self._selected_checklist_item_index is None:
            self._set_status(t(self.language, "select_checklist_item_first"))
            return
        del self._checklist_items[self._selected_checklist_item_index]
        if not self._checklist_items:
            self._selected_checklist_item_index = None
        else:
            self._selected_checklist_item_index = min(self._selected_checklist_item_index, len(self._checklist_items) - 1)
        await self._refresh_checklist_items_list()

    async def _move_checklist_item(self, delta: int) -> None:
        if self._selected_checklist_item_index is None:
            self._set_status(t(self.language, "select_checklist_item_first"))
            return
        new_index = self._selected_checklist_item_index + delta
        if new_index < 0 or new_index >= len(self._checklist_items):
            return
        item = self._checklist_items.pop(self._selected_checklist_item_index)
        self._checklist_items.insert(new_index, item)
        self._selected_checklist_item_index = new_index
        await self._refresh_checklist_items_list()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "editor-ticket":
            try:
                self.query_one("#ticket_notes", TextArea).focus()
            except Exception:
                pass

    def action_save(self) -> None:
        trip, error = self._collect_trip()
        if error:
            self._set_status(f"[b][red]Fix the following:[/red][/b] {error}")
            return
        self.dismiss(trip)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "timing-steps-list":
            self._selected_timing_item(event.item)
        elif event.list_view.id == "arrival-timing-steps-list":
            self._selected_arrival_timing_item(event.item)
        elif event.list_view.id == "checklist-items-list":
            self._selected_checklist_item(event.item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "timing-steps-list":
            self._selected_timing_item(event.item)
        elif event.list_view.id == "arrival-timing-steps-list":
            self._selected_arrival_timing_item(event.item)
        elif event.list_view.id == "checklist-items-list":
            self._selected_checklist_item(event.item)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "timing-add":
            await self._add_or_update_timing_step(update_existing=False)
        elif event.button.id == "timing-update":
            await self._add_or_update_timing_step(update_existing=True)
        elif event.button.id == "timing-remove":
            await self._remove_timing_step()
        elif event.button.id == "timing-up":
            await self._move_timing_step(-1)
        elif event.button.id == "timing-down":
            await self._move_timing_step(1)
        elif event.button.id == "arrival-timing-add":
            await self._add_or_update_arrival_timing_step(update_existing=False)
        elif event.button.id == "arrival-timing-update":
            await self._add_or_update_arrival_timing_step(update_existing=True)
        elif event.button.id == "arrival-timing-remove":
            await self._remove_arrival_timing_step()
        elif event.button.id == "arrival-timing-up":
            await self._move_arrival_timing_step(-1)
        elif event.button.id == "arrival-timing-down":
            await self._move_arrival_timing_step(1)
        elif event.button.id == "checklist-add":
            await self._add_or_update_checklist_item(update_existing=False)
        elif event.button.id == "checklist-update":
            await self._add_or_update_checklist_item(update_existing=True)
        elif event.button.id == "checklist-remove":
            await self._remove_checklist_item()
        elif event.button.id == "checklist-up":
            await self._move_checklist_item(-1)
        elif event.button.id == "checklist-down":
            await self._move_checklist_item(1)


class TripAdminApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #workspace {
        height: 1fr;
        layout: horizontal;
    }

    #left-pane {
        width: 30%;
        min-width: 28;
        border: tall $accent;
        padding: 1;
    }

    #right-pane {
        width: 1fr;
        border: tall $accent;
        padding: 1;
    }

    #right-pane TabbedContent {
        height: 1fr;
        margin-top: 1;
    }

    .detail-pane {
        padding: 0 1 1 0;
    }

    .timing-input-row {
        height: auto;
    }

    .table-title {
        text-style: bold;
        color: $accent-lighten-1;
        margin: 0 0 1 0;
    }

    DataTable {
        height: auto;
        min-height: 12;
        background: $surface;
        color: $text;
        border: round $accent 45%;
    }

    #trip-filter {
        margin-bottom: 1;
    }

    #trip-list {
        height: 1fr;
    }

    #status-line {
        height: auto;
        color: $text-muted;
        margin-top: 1;
    }

    #trip-tabs {
        height: 1fr;
    }

    .detail-pane {
        padding: 1;
    }

    .timing-section-card {
        border: round $accent 35%;
        padding: 1;
        margin-bottom: 1;
        background: $surface-darken-1;
    }

    .timing-section-title {
        text-style: bold;
        color: $accent-lighten-1;
        margin-bottom: 1;
    }

    #editor-card, #confirm-card {
        width: 92%;
        height: 92%;
        border: thick $accent;
        background: $panel;
        padding: 1 2;
    }

    #editor-tabs {
        height: 1fr;
    }

    .editor-tab {
        padding: 1 0;
    }

    .editor-input, .editor-textarea, .editor-checkbox {
        margin-bottom: 1;
    }

    .ticket-airline-row {
        height: auto;
    }

    .ticket-passenger-row {
        height: auto;
    }

    .ticket-booking-row {
        height: auto;
    }

    .ticket-seat-row {
        height: auto;
    }

    .overview-route-row {
        height: auto;
    }

    .overview-time-row {
        height: auto;
    }

    .ticket-field-block {
        width: 1fr;
        height: auto;
    }

    #title {
        width: 2fr;
    }

    #departure_airport, #arrival_airport {
        width: 1fr;
    }

    #departure_datetime, #flight_arrival_time {
        width: 1fr;
    }

    #passenger_name {
        width: 3fr;
    }

    #checkin_done {
        width: 1fr;
    }

    #airline_code {
        width: 1fr;
    }

    #flight_number {
        width: 1fr;
    }

    #airline {
        width: 2fr;
    }

    #booking_reference {
        width: 2fr;
    }

    #ticket_number, #ticket_cost {
        width: 1fr;
    }

    #timing-step-name, #arrival-timing-step-name {
        width: 3fr;
    }

    #timing-step-minutes, #timing-step-cost,
    #arrival-timing-step-minutes, #arrival-timing-step-cost {
        width: 1fr;
    }

    .checklist-input-row {
        height: auto;
    }

    #checklist-item-name {
        width: 3fr;
    }

    #checklist-item-done {
        width: 1fr;
    }

    .editor-textarea {
        height: 14;
        min-height: 14;
    }

    .dialog-title {
        text-style: bold;
        margin-bottom: 1;
    }

    .dialog-message {
        color: $text-muted;
        margin-bottom: 1;
    }

    .dialog-buttons {
        height: auto;
        dock: bottom;
        align-horizontal: right;
        margin-top: 1;
    }
    """

    BINDINGS: list[Binding] = []

    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings.load()
        self.language = self.settings.language
        self.store = TripStore(DB_PATH)
        self._filter_text = ""
        self._selected_trip_id: int | None = None
        self._flight_status_cache: dict[int, FlightStatusResult] = {}
        self._status_log: list[str] = []
        self.flight_provider = self._build_flight_provider()
        self._set_app_bindings()

    def _detail_table_columns(self, widget_id: str) -> list[tuple[str, int]]:
        if widget_id == "timing-view":
            return [
                (t(self.language, "step_name_label"), 34),
                (t(self.language, "step_minutes_label"), 18),
                (t(self.language, "calculated_time_label"), 22),
                (t(self.language, "step_cost_label"), 18),
            ]
        column_widths = {
            "overview-view": (26, 54),
            "ticket-view": (28, 54),
            "docs-view": (28, 66),
            "packing-view": (24, 66),
            "flight-view": (10, 88),
            "checklist-view": (38, 14),
            "notes-view": (18, 92),
            "summary-view": (28, 62),
            "log-view": (8, 96),
        }
        field_width, value_width = column_widths.get(widget_id, (28, 60))
        return [
            (t(self.language, "field_column"), field_width),
            (t(self.language, "value_column"), value_width),
        ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, name=t(self.language, "app_title"))
        with Container(id="workspace"):
            with Vertical(id="left-pane"):
                yield Input(placeholder=t(self.language, "filter_placeholder"), id="trip-filter")
                with Horizontal(classes="dialog-buttons"):
                    yield Button(t(self.language, "new"), variant="success", id="new-trip")
                    yield Button(t(self.language, "edit"), variant="primary", id="edit-trip")
                    yield Button(t(self.language, "duplicate"), variant="default", id="duplicate-trip")
                yield ListView(id="trip-list")
                yield Static("", id="status-line")
            with Vertical(id="right-pane"):
                yield Static(t(self.language, "trip_details"), classes="dialog-title")
                with TabbedContent(id="trip-tabs"):
                    with TabPane(t(self.language, "overview"), id="overview"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "overview_panel_title"), id="overview-title", classes="table-title")
                            yield DataTable(id="overview-view")
                    with TabPane(t(self.language, "ticket"), id="ticket"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "ticket_panel_title"), id="ticket-title", classes="table-title")
                            yield DataTable(id="ticket-view")
                    with TabPane(t(self.language, "docs"), id="docs"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "docs_panel_title"), id="docs-title", classes="table-title")
                            yield DataTable(id="docs-view")
                    with TabPane(t(self.language, "timing"), id="timing"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "timing_panel_title"), id="timing-title", classes="table-title")
                            yield DataTable(id="timing-view")
                    with TabPane(t(self.language, "packing"), id="packing"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "packing_panel_title"), id="packing-title", classes="table-title")
                            yield DataTable(id="packing-view")
                    with TabPane(t(self.language, "flight"), id="flight"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "flight_panel_title"), id="flight-title", classes="table-title")
                            yield DataTable(id="flight-view")
                    with TabPane(t(self.language, "checklist"), id="checklist"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "checklist_panel_title"), id="checklist-title", classes="table-title")
                            yield DataTable(id="checklist-view")
                    with TabPane(t(self.language, "notes"), id="notes"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "notes_panel_title"), id="notes-title", classes="table-title")
                            yield DataTable(id="notes-view")
                    with TabPane(t(self.language, "summary"), id="summary"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "summary_panel_title"), id="summary-title", classes="table-title")
                            yield DataTable(id="summary-view")
                    with TabPane(t(self.language, "log"), id="log"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static(t(self.language, "status_log"), id="log-title", classes="table-title")
                            yield DataTable(id="log-view")
        yield Footer()

    async def on_mount(self) -> None:
        self._setup_detail_tables()
        self._update_main_tab_labels()
        await self.refresh_trip_list()

    def _set_status(self, message: str) -> None:
        self.query_one("#status-line", Static).update(message)

    def _append_status_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._status_log.append(f"[{timestamp}] {message}")
        self._update_table(
            "log-view",
            [(Text(str(index), style="dim"), severity_cell(entry)) for index, entry in enumerate(self._status_log[-200:], start=1)]
            or [(Text("", style="dim"), Text(t(self.language, "no_status_messages"), style="italic dim"))],
        )

    def _setup_detail_tables(self) -> None:
        for widget_id in (
            "overview-view",
            "ticket-view",
            "docs-view",
            "timing-view",
            "packing-view",
            "flight-view",
            "checklist-view",
            "notes-view",
            "summary-view",
            "log-view",
        ):
            table = self.query_one(f"#{widget_id}", DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = True
            table.fixed_columns = 1
            self._ensure_table_columns(widget_id)

    def _ensure_table_columns(self, widget_id: str) -> None:
        table = self.query_one(f"#{widget_id}", DataTable)
        columns = self._detail_table_columns(widget_id)
        if len(table.ordered_columns) == len(columns):
            return
        table.clear(columns=True)
        for label, width in columns:
            table.add_column(label, width=width)

    def _update_table(self, widget_id: str, rows: list[tuple[object, ...]]) -> None:
        table = self.query_one(f"#{widget_id}", DataTable)
        self._ensure_table_columns(widget_id)
        table.clear()
        column_count = len(table.ordered_columns)
        for row in rows:
            padded_row = tuple(row) + ("",) * max(0, column_count - len(row))
            cell_heights = [str(getattr(cell, "plain", cell)).count("\n") + 1 for cell in padded_row]
            table.add_row(*padded_row[:column_count], height=max(cell_heights))

    def _build_flight_provider(self) -> FlightValidationProvider:
        provider_name = os.environ.get(
            "FLIGHT_VALIDATION_PROVIDER",
            self.settings.flight_provider or DEFAULT_FLIGHT_PROVIDER,
        ).strip().lower()
        if provider_name in ("opensky", ""):
            return OpenSkyFlightProvider()
        if provider_name in ("placeholder", "schedule-placeholder"):
            return PlaceholderScheduleValidationProvider()
        return PlaceholderScheduleValidationProvider()

    def _set_app_bindings(self) -> None:
        bindings = [
            Binding("n", "new_trip", t(self.language, "new")),
            Binding("e", "edit_trip", t(self.language, "edit")),
            Binding("c", "duplicate_trip", t(self.language, "copy")),
            Binding("d", "delete_trip", t(self.language, "delete")),
            Binding("x", "toggle_checklist_item", t(self.language, "toggle_checklist_item")),
            Binding("enter", "toggle_checklist_item", show=False),
            Binding("l", "show_language", t(self.language, "language")),
            Binding("i", "show_info", t(self.language, "info")),
            Binding("s", "refresh_flight_status", t(self.language, "flight_status")),
            Binding("r", "refresh", t(self.language, "refresh")),
            Binding("f", "focus_filter", t(self.language, "filter")),
            Binding("q", "quit", t(self.language, "quit")),
        ]
        type(self).BINDINGS = bindings
        type(self)._merged_bindings = type(self)._merge_bindings()
        self.BINDINGS = bindings
        self._bindings = type(self)._merged_bindings.copy()
        if self.ENABLE_COMMAND_PALETTE:
            for _key, binding in self._bindings:
                if binding.action in {"command_palette", "app.command_palette"}:
                    break
            else:
                self._bindings._add_binding(
                    Binding(
                        self.COMMAND_PALETTE_BINDING,
                        "command_palette",
                        "palette",
                        show=False,
                        key_display=self.COMMAND_PALETTE_DISPLAY,
                    )
                )

    def _update_main_tab_labels(self) -> None:
        try:
            tabs = self.query_one("#trip-tabs", TabbedContent)
        except Exception:
            return
        labels = {
            "overview": t(self.language, "overview"),
            "ticket": t(self.language, "ticket"),
            "docs": t(self.language, "docs"),
            "timing": t(self.language, "timing"),
            "packing": t(self.language, "packing"),
            "flight": t(self.language, "flight"),
            "checklist": t(self.language, "checklist"),
            "notes": t(self.language, "notes"),
            "summary": t(self.language, "summary"),
            "log": t(self.language, "log"),
        }
        for pane_id, label in labels.items():
            try:
                tabs.get_tab(pane_id).label = label
            except Exception:
                continue
        title_labels = {
            "overview-title": t(self.language, "overview_panel_title"),
            "ticket-title": t(self.language, "ticket_panel_title"),
            "docs-title": t(self.language, "docs_panel_title"),
            "timing-title": t(self.language, "timing_panel_title"),
            "packing-title": t(self.language, "packing_panel_title"),
            "flight-title": t(self.language, "flight_panel_title"),
            "checklist-title": t(self.language, "checklist_panel_title"),
            "notes-title": t(self.language, "notes_panel_title"),
            "summary-title": t(self.language, "summary_panel_title"),
            "log-title": t(self.language, "status_log"),
        }
        for widget_id, label in title_labels.items():
            try:
                self.query_one(f"#{widget_id}", Static).update(label)
            except Exception:
                continue

    def _current_trip(self) -> Trip | None:
        if self._selected_trip_id is None:
            return None
        return self.store.get_trip(self._selected_trip_id)

    async def refresh_trip_list(self, select_trip_id: int | None = None) -> None:
        trip_list = self.query_one("#trip-list", ListView)
        await trip_list.clear()

        trips = self.store.list_trips(self._filter_text or None)
        for trip in trips:
            await trip_list.append(TripListItem(trip))

        if not trips:
            self._selected_trip_id = None
            self._clear_details(t(self.language, "no_trips_found"))
            self._set_status(t(self.language, "no_trips_found"))
            return

        index = 0
        if select_trip_id is not None:
            for idx, trip in enumerate(trips):
                if trip.id == select_trip_id:
                    index = idx
                    break

        trip_list.index = index
        trip = trips[index]
        self._selected_trip_id = trip.id
        self._show_trip(trip)
        self._set_status(t(self.language, "trips_loaded", count=len(trips)))

    def _clear_details(self, message: str) -> None:
        for widget_id in (
            "overview-view",
            "ticket-view",
            "docs-view",
            "timing-view",
            "packing-view",
            "flight-view",
            "checklist-view",
            "notes-view",
            "summary-view",
            "log-view",
        ):
            if widget_id == "timing-view":
                self._update_table(widget_id, [(Text("", style="dim"), Text(message, style="italic dim"), Text("", style="dim"), Text("", style="dim"))])
            else:
                self._update_table(widget_id, [(Text("", style="dim"), Text(message, style="italic dim"))])
        self._update_table(
            "log-view",
            [(Text(str(index), style="dim"), severity_cell(entry)) for index, entry in enumerate(self._status_log[-200:], start=1)]
            or [(Text("", style="dim"), Text(t(self.language, "no_status_messages"), style="italic dim"))],
        )

    def _show_trip(self, trip: Trip | None) -> None:
        if trip is None:
            self._clear_details(t(self.language, "select_trip_first"))
            return

        completed_steps, total_checklist_items = checklist_progress(trip)
        safe_leave_home, timing_total_minutes = estimated_safe_leave_home(trip)
        estimated_home_arrive, arrival_timing_total_minutes = estimated_home_arrival(trip)
        departure_schedule = departure_timing_schedule(trip)
        arrival_schedule = arrival_timing_schedule(trip)

        overview_rows = [
            (t(self.language, "title_label"), value_or_none(trip.title)),
            (t(self.language, "route_label"), f"{value_or_none(trip.departure_airport)} -> {value_or_none(trip.arrival_airport)}"),
            (t(self.language, "departure_label"), value_or_none(trip.departure_datetime)),
            (t(self.language, "flight_arrival_time_label"), value_or_none(trip.flight_arrival_time)),
            (t(self.language, "checklist_progress_label"), f"{completed_steps}/{total_checklist_items} completed"),
            (t(self.language, "created_at_label"), value_or_none(trip.created_at)),
            (t(self.language, "updated_at_label"), value_or_none(trip.updated_at)),
        ]
        ticket_rows = [
            (t(self.language, "passenger_name"), value_or_none(trip.passenger_name)),
            (t(self.language, "checkin_done_label"), bool_cell(self.language, trip.checkin_done)),
            (t(self.language, "airline_code"), value_or_none(trip.airline_code)),
            (t(self.language, "flight_number"), value_or_none(trip.flight_number)),
            (t(self.language, "airline"), value_or_none(trip.airline)),
            (t(self.language, "booking_reference"), value_or_none(trip.booking_reference)),
            (t(self.language, "ticket_number"), value_or_none(trip.ticket_number)),
            (t(self.language, "ticket_cost"), right_text(value_or_none(trip.ticket_cost), "cyan")),
            (t(self.language, "seat"), value_or_none(trip.seat)),
            (t(self.language, "cabin_class"), value_or_none(trip.cabin_class)),
            (t(self.language, "notes_label"), value_or_none(trip.ticket_notes)),
        ]
        flight_rows = [
            (Text(str(index), style="dim"), severity_cell(line)) for index, line in enumerate(self._flight_status_lines(trip), start=1)
        ]
        docs_rows = [
            (t(self.language, "required_documentation_label"), multiline_or_none(trip.documentation_required)),
            (t(self.language, "documents_to_carry"), multiline_or_none(trip.documents_to_carry)),
        ]
        timing_rows = [
            (t(self.language, "departure_timing_section"), "", "", ""),
            (t(self.language, "departure_label"), value_or_none(trip.departure_datetime), "", ""),
            *[
                (
                    step.name,
                    right_text(format_minutes(step.minutes), "cyan"),
                    value_or_none(scheduled_at),
                    right_text(value_or_none(step.cost), "cyan"),
                )
                for step, scheduled_at in departure_schedule
            ],
            (t(self.language, "total_timing_buffer_label"), right_text(format_minutes(timing_total_minutes), "bold cyan") if timing_total_minutes is not None else warning_cell("(none)"), "", ""),
            (
                t(self.language, "estimated_safe_leave_home"),
                Text(safe_leave_home, style="bold green") if safe_leave_home else warning_cell(t(self.language, "timing_estimate_unavailable")),
                "",
                "",
            ),
            (t(self.language, "arrival_timing_section"), "", "", ""),
            (t(self.language, "flight_arrival_time_label"), value_or_none(trip.flight_arrival_time), "", ""),
            *[
                (
                    step.name,
                    right_text(format_minutes(step.minutes), "cyan"),
                    value_or_none(scheduled_at),
                    right_text(value_or_none(step.cost), "cyan"),
                )
                for step, scheduled_at in arrival_schedule
            ],
            (t(self.language, "arrival_total_timing_label"), right_text(format_minutes(arrival_timing_total_minutes), "bold cyan") if arrival_timing_total_minutes is not None else warning_cell("(none)"), "", ""),
            (
                t(self.language, "estimated_home_arrival"),
                Text(estimated_home_arrive, style="bold green") if estimated_home_arrive else warning_cell(t(self.language, "arrival_timing_estimate_unavailable")),
                "",
                "",
            ),
        ]
        packing_rows = [
            (t(self.language, "clothes_label"), multiline_or_none(trip.clothes_items)),
            (t(self.language, "electronics_label"), multiline_or_none(trip.electronics_items)),
            (t(self.language, "health_label"), multiline_or_none(trip.health_items)),
            (t(self.language, "other_items_label"), multiline_or_none(trip.other_items)),
        ]
        checklist_rows = [
            (item.name, status_cell(self.language, item.done))
            for item in trip.checklist_items
        ] or [(t(self.language, "checklist_items_label"), warning_cell("(none)"))]
        notes_rows = [(t(self.language, "notes_label"), trip.general_notes.strip() if trip.general_notes.strip() else "(none)")]
        trip_total_cost = total_trip_cost(trip)
        summary_rows = [
            (t(self.language, "trip_label"), value_or_none(trip.title)),
            (t(self.language, "passenger_label"), value_or_none(trip.passenger_name)),
            (t(self.language, "route_label"), f"{value_or_none(trip.departure_airport)} -> {value_or_none(trip.arrival_airport)}"),
            (t(self.language, "departure_label"), value_or_none(trip.departure_datetime)),
            (t(self.language, "flight_arrival_time_label"), value_or_none(trip.flight_arrival_time)),
            (t(self.language, "flight_label"), value_or_none(f"{trip.airline_code}{trip.flight_number}".strip())),
            (t(self.language, "airline"), value_or_none(trip.airline)),
            (t(self.language, "booking_reference"), value_or_none(trip.booking_reference)),
            (t(self.language, "ticket_cost"), right_text(value_or_none(trip.ticket_cost), "cyan")),
            (t(self.language, "total_cost_label"), right_text(format_cost(trip_total_cost), "bold cyan") if trip_total_cost is not None else "(none)"),
            (t(self.language, "checkin_label"), status_cell(self.language, trip.checkin_done)),
            (t(self.language, "checklist_progress_label"), f"{completed_steps}/{total_checklist_items} completed"),
            (t(self.language, "required_documentation_label"), multiline_or_none(trip.documentation_required)),
            (t(self.language, "packing_snapshot_label"), f"{t(self.language, 'clothes_label')} {len(trip.clothes_items)} | {t(self.language, 'electronics_label')} {len(trip.electronics_items)} | {t(self.language, 'health_label')} {len(trip.health_items)} | {t(self.language, 'other_items_label')} {len(trip.other_items)}"),
        ]

        self._update_table("overview-view", overview_rows)
        self._update_table("ticket-view", ticket_rows)
        self._update_table("docs-view", docs_rows)
        self._update_table("timing-view", timing_rows)
        self._update_table("packing-view", packing_rows)
        self._update_table("flight-view", flight_rows)
        self._update_table("checklist-view", checklist_rows)
        self._update_table("notes-view", notes_rows)
        self._update_table("summary-view", summary_rows)
        self._update_table(
            "log-view",
            [(Text(str(index), style="dim"), severity_cell(entry)) for index, entry in enumerate(self._status_log[-200:], start=1)]
            or [(Text("", style="dim"), Text(t(self.language, "no_status_messages"), style="italic dim"))],
        )

    def _flight_status_lines(self, trip: Trip) -> list[str]:
        flight_id = f"{trip.airline_code}{trip.flight_number}".strip()
        if trip.id is None:
            return [t(self.language, "save_trip_before_status")]
        cached = self._flight_status_cache.get(trip.id)
        if cached is None:
            return [
                f"{t(self.language, 'provider_label')}: {self.flight_provider.provider_name}",
                f"{t(self.language, 'lookup_key_label')}: {flight_id or '(missing airline code / flight number)'}",
                f"{t(self.language, 'departure_date_label')}: {departure_date(trip.departure_datetime) or '(missing departure date)'}",
                "",
                t(self.language, "press_status_hint"),
                t(self.language, "choose_provider_hint"),
            ]
        return [
            f"{t(self.language, 'provider_label')}: {self.flight_provider.provider_name}",
            f"{t(self.language, 'lookup_key_label')}: {flight_id or '(missing airline code / flight number)'}",
            f"{t(self.language, 'fetched_at_label')}: {cached.fetched_at}",
            f"{t(self.language, 'result_label')}: {cached.summary}",
            "",
            *cached.lines,
        ]

    async def _refresh_flight_status_for_trip(self, trip: Trip) -> None:
        result = await asyncio.to_thread(self.flight_provider.fetch, trip)
        if trip.id is None:
            return
        self._flight_status_cache[trip.id] = result
        log_prefix = f"Status lookup via {self.flight_provider.provider_name} for {trip.title or trip.id}"
        self._append_status_log(f"{log_prefix}: {result.summary}")
        for line in result.lines:
            self._append_status_log(f"{log_prefix}: {line}")
        if self._selected_trip_id == trip.id:
            self._show_trip(self.store.get_trip(trip.id))
        self._set_status(f"Flight status updated: {result.summary}")

    async def _save_trip_and_refresh(self, result: Trip) -> None:
        trip_id = self.store.save_trip(result)
        result.id = trip_id
        await self.refresh_trip_list(select_trip_id=trip_id)
        self._set_status(t(self.language, "trip_saved"))

    async def _duplicate_trip_and_refresh(self, trip: Trip) -> None:
        duplicated = trip.duplicate()
        trip_id = self.store.save_trip(duplicated)
        duplicated.id = trip_id
        await self.refresh_trip_list(select_trip_id=trip_id)
        self._append_status_log(t(self.language, "trip_duplicated_log", source=trip.title, target=duplicated.title))
        self._set_status(t(self.language, "trip_duplicated"))

    async def _delete_trip_and_refresh(self, trip_id: int) -> None:
        self.store.delete_trip(trip_id)
        self._selected_trip_id = None
        await self.refresh_trip_list()
        self._set_status(t(self.language, "trip_deleted"))

    async def _toggle_checklist_item_and_refresh(self, trip: Trip, item_index: int) -> None:
        if item_index < 0 or item_index >= len(trip.checklist_items):
            self._set_status(t(self.language, "select_checklist_item_first"))
            return
        item = trip.checklist_items[item_index]
        item.done = not item.done
        trip.updated_at = now_iso()
        self.store.save_trip(trip)
        await self.refresh_trip_list(select_trip_id=trip.id)
        state_label = t(self.language, "done_label") if item.done else t(self.language, "pending_label")
        self._append_status_log(t(self.language, "checklist_item_toggled_log", name=item.name, state=state_label))
        self._set_status(t(self.language, "checklist_item_toggled_status", name=item.name, state=state_label))

    def _open_editor(self, trip: Trip | None = None) -> None:
        def _on_dismissed(result: Trip | None) -> None:
            if result is None:
                return
            self.run_worker(self._save_trip_and_refresh(result), name="save-trip")

        self.push_screen(TripEditorScreen(self.language, trip), callback=_on_dismissed)

    async def action_new_trip(self) -> None:
        self._open_editor()

    async def action_edit_trip(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            return
        self._open_editor(trip)

    async def action_delete_trip(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            return

        def _on_confirmed(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._delete_trip_and_refresh(trip.id or 0), name="delete-trip")

        self.push_screen(
            ConfirmScreen(
                self.language,
                t(self.language, "confirm_delete_title"),
                t(self.language, "confirm_delete_message", title=trip.title),
            ),
            callback=_on_confirmed,
        )

    async def action_duplicate_trip(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            return
        self.run_worker(self._duplicate_trip_and_refresh(trip), name="duplicate-trip")

    async def action_toggle_checklist_item(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            return
        tabs = self.query_one("#trip-tabs", TabbedContent)
        if tabs.active != "checklist":
            self._set_status(t(self.language, "open_checklist_tab_first"))
            return
        if not trip.checklist_items:
            self._set_status(t(self.language, "no_checklist_items"))
            return
        table = self.query_one("#checklist-view", DataTable)
        row_index = table.cursor_row
        if row_index < 0 or row_index >= len(trip.checklist_items):
            self._set_status(t(self.language, "select_checklist_item_first"))
            return
        await self._toggle_checklist_item_and_refresh(trip, row_index)

    async def action_refresh(self) -> None:
        await self.refresh_trip_list(self._selected_trip_id)

    async def action_refresh_flight_status(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            self._append_status_log("Flight status request failed: no trip selected.")
            return
        self._set_status(t(self.language, "flight_status"))
        self._append_status_log(
            f"Starting flight status request with provider {self.flight_provider.provider_name} for {trip.title or trip.id} using key {trip.airline_code}{trip.flight_number}."
        )
        self.run_worker(self._refresh_flight_status_for_trip(trip), name="flight-status", exclusive=True)

    def action_show_info(self) -> None:
        self.push_screen(InfoScreen(self.language))

    def action_show_language(self) -> None:
        def _on_language(language: str | None) -> None:
            if not language or language == self.language:
                return
            self.run_worker(self._apply_language(language), name="language-change")

        self.push_screen(LanguageScreen(self.language), callback=_on_language)

    async def _apply_language(self, language: str) -> None:
        self.language = language
        self.settings.language = language
        self.settings.save()
        self._set_app_bindings()
        self.refresh_bindings()
        await self.recompose()
        self._update_main_tab_labels()
        await self.refresh_trip_list(self._selected_trip_id)
        self._set_status(t(self.language, "language_saved", lang_label=t(self.language, "lang_name")))

    def action_focus_filter(self) -> None:
        self.query_one("#trip-filter", Input).focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "trip-filter":
            return
        self._filter_text = event.value.strip()
        await self.refresh_trip_list()

    def _select_trip_from_item(self, item: ListItem | None) -> None:
        if item is None:
            return
        trip_id = getattr(item, "trip_id", None)
        if trip_id is None:
            return
        self._selected_trip_id = trip_id
        trip = self.store.get_trip(trip_id)
        self._show_trip(trip)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._select_trip_from_item(event.item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._select_trip_from_item(event.item)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "checklist-view":
            return
        try:
            tabs = self.query_one("#trip-tabs", TabbedContent)
        except Exception:
            return
        if tabs.active != "checklist":
            return
        self.run_worker(self.action_toggle_checklist_item(), name="toggle-checklist-row")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "new-trip":
            await self.action_new_trip()
        elif button_id == "edit-trip":
            await self.action_edit_trip()
        elif button_id == "duplicate-trip":
            await self.action_duplicate_trip()


def main() -> None:
    TripAdminApp().run()


if __name__ == "__main__":
    main()

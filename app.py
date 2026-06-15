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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Protocol

from rich.panel import Panel
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
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
DT_FORMAT = "%Y-%m-%d %H:%M"
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
DEFAULT_FLIGHT_PROVIDER = "opensky"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_datetime(value: str) -> str:
    datetime.strptime(value, DT_FORMAT)
    return value


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


def bullets(items: Iterable[str]) -> str:
    cleaned = [item for item in items if item]
    return "\n".join(f"  - {item}" for item in cleaned) if cleaned else "  (none)"


def section(title: str, lines: Iterable[str]) -> str:
    body = "\n".join(lines)
    return f"{title}\n{'=' * len(title)}\n{body}"


def panel(title: str, lines: Iterable[str]) -> Panel:
    return Panel(section(title, lines), expand=True)


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
class Trip:
    id: int | None = None
    title: str = ""
    departure_airport: str = ""
    arrival_airport: str = ""
    departure_datetime: str = ""
    return_datetime: str = ""
    passenger_name: str = ""
    checkin_done: bool = False
    checklist_ticket_ready: bool = False
    checklist_checkin_done: bool = False
    checklist_documentation_ready: bool = False
    checklist_transport_booked: bool = False
    checklist_bags_ready: bool = False
    checklist_packing_done: bool = False
    airline_code: str = ""
    flight_number: str = ""
    airline: str = ""
    booking_reference: str = ""
    ticket_number: str = ""
    seat: str = ""
    cabin_class: str = ""
    ticket_notes: str = ""
    documentation_required: list[str] = field(default_factory=list)
    outbound_transport_mode: str = ""
    outbound_transport_provider: str = ""
    outbound_leave_home: str = ""
    outbound_arrive_airport: str = ""
    outbound_transport_notes: str = ""
    return_transport_mode: str = ""
    return_transport_provider: str = ""
    return_leave_airport: str = ""
    return_arrive_home: str = ""
    return_transport_notes: str = ""
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
            return_datetime=row["return_datetime"],
            passenger_name=row["passenger_name"],
            checkin_done=bool(row["checkin_done"]),
            checklist_ticket_ready=bool(row["checklist_ticket_ready"]),
            checklist_checkin_done=bool(row["checklist_checkin_done"]),
            checklist_documentation_ready=bool(row["checklist_documentation_ready"]),
            checklist_transport_booked=bool(row["checklist_transport_booked"]),
            checklist_bags_ready=bool(row["checklist_bags_ready"]),
            checklist_packing_done=bool(row["checklist_packing_done"]),
            airline_code=row["airline_code"],
            flight_number=row["flight_number"],
            airline=row["airline"],
            booking_reference=row["booking_reference"],
            ticket_number=row["ticket_number"],
            seat=row["seat"],
            cabin_class=row["cabin_class"],
            ticket_notes=row["ticket_notes"],
            documentation_required=from_json(row["documentation_required"]),
            outbound_transport_mode=row["outbound_transport_mode"],
            outbound_transport_provider=row["outbound_transport_provider"],
            outbound_leave_home=row["outbound_leave_home"],
            outbound_arrive_airport=row["outbound_arrive_airport"],
            outbound_transport_notes=row["outbound_transport_notes"],
            return_transport_mode=row["return_transport_mode"],
            return_transport_provider=row["return_transport_provider"],
            return_leave_airport=row["return_leave_airport"],
            return_arrive_home=row["return_arrive_home"],
            return_transport_notes=row["return_transport_notes"],
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
            "return_datetime": self.return_datetime,
            "passenger_name": self.passenger_name,
            "checkin_done": int(self.checkin_done),
            "checklist_ticket_ready": int(self.checklist_ticket_ready),
            "checklist_checkin_done": int(self.checklist_checkin_done),
            "checklist_documentation_ready": int(self.checklist_documentation_ready),
            "checklist_transport_booked": int(self.checklist_transport_booked),
            "checklist_bags_ready": int(self.checklist_bags_ready),
            "checklist_packing_done": int(self.checklist_packing_done),
            "airline_code": self.airline_code,
            "flight_number": self.flight_number,
            "airline": self.airline,
            "booking_reference": self.booking_reference,
            "ticket_number": self.ticket_number,
            "seat": self.seat,
            "cabin_class": self.cabin_class,
            "ticket_notes": self.ticket_notes,
            "documentation_required": to_json(self.documentation_required),
            "outbound_transport_mode": self.outbound_transport_mode,
            "outbound_transport_provider": self.outbound_transport_provider,
            "outbound_leave_home": self.outbound_leave_home,
            "outbound_arrive_airport": self.outbound_arrive_airport,
            "outbound_transport_notes": self.outbound_transport_notes,
            "return_transport_mode": self.return_transport_mode,
            "return_transport_provider": self.return_transport_provider,
            "return_leave_airport": self.return_leave_airport,
            "return_arrive_home": self.return_arrive_home,
            "return_transport_notes": self.return_transport_notes,
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
            return_datetime=self.return_datetime,
            passenger_name=self.passenger_name,
            checkin_done=self.checkin_done,
            checklist_ticket_ready=self.checklist_ticket_ready,
            checklist_checkin_done=self.checklist_checkin_done,
            checklist_documentation_ready=self.checklist_documentation_ready,
            checklist_transport_booked=self.checklist_transport_booked,
            checklist_bags_ready=self.checklist_bags_ready,
            checklist_packing_done=self.checklist_packing_done,
            airline_code=self.airline_code,
            flight_number=self.flight_number,
            airline=self.airline,
            booking_reference=self.booking_reference,
            ticket_number=self.ticket_number,
            seat=self.seat,
            cabin_class=self.cabin_class,
            ticket_notes=self.ticket_notes,
            documentation_required=list(self.documentation_required),
            outbound_transport_mode=self.outbound_transport_mode,
            outbound_transport_provider=self.outbound_transport_provider,
            outbound_leave_home=self.outbound_leave_home,
            outbound_arrive_airport=self.outbound_arrive_airport,
            outbound_transport_notes=self.outbound_transport_notes,
            return_transport_mode=self.return_transport_mode,
            return_transport_provider=self.return_transport_provider,
            return_leave_airport=self.return_leave_airport,
            return_arrive_home=self.return_arrive_home,
            return_transport_notes=self.return_transport_notes,
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
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                departure_airport TEXT NOT NULL,
                arrival_airport TEXT NOT NULL,
                departure_datetime TEXT NOT NULL,
                return_datetime TEXT NOT NULL DEFAULT '',
                passenger_name TEXT NOT NULL DEFAULT '',
                checkin_done INTEGER NOT NULL DEFAULT 0,
                checklist_ticket_ready INTEGER NOT NULL DEFAULT 0,
                checklist_checkin_done INTEGER NOT NULL DEFAULT 0,
                checklist_documentation_ready INTEGER NOT NULL DEFAULT 0,
                checklist_transport_booked INTEGER NOT NULL DEFAULT 0,
                checklist_bags_ready INTEGER NOT NULL DEFAULT 0,
                checklist_packing_done INTEGER NOT NULL DEFAULT 0,
                airline_code TEXT NOT NULL DEFAULT '',
                flight_number TEXT NOT NULL DEFAULT '',
                airline TEXT NOT NULL DEFAULT '',
                booking_reference TEXT NOT NULL DEFAULT '',
                ticket_number TEXT NOT NULL DEFAULT '',
                seat TEXT NOT NULL DEFAULT '',
                cabin_class TEXT NOT NULL DEFAULT '',
                ticket_notes TEXT NOT NULL DEFAULT '',
                documentation_required TEXT NOT NULL DEFAULT '[]',
                outbound_transport_mode TEXT NOT NULL DEFAULT '',
                outbound_transport_provider TEXT NOT NULL DEFAULT '',
                outbound_leave_home TEXT NOT NULL DEFAULT '',
                outbound_arrive_airport TEXT NOT NULL DEFAULT '',
                outbound_transport_notes TEXT NOT NULL DEFAULT '',
                return_transport_mode TEXT NOT NULL DEFAULT '',
                return_transport_provider TEXT NOT NULL DEFAULT '',
                return_leave_airport TEXT NOT NULL DEFAULT '',
                return_arrive_home TEXT NOT NULL DEFAULT '',
                return_transport_notes TEXT NOT NULL DEFAULT '',
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
        self._ensure_column("passenger_name", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("checkin_done", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("checklist_ticket_ready", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("checklist_checkin_done", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("checklist_documentation_ready", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("checklist_transport_booked", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("checklist_bags_ready", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("checklist_packing_done", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("airline_code", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("flight_number", "TEXT NOT NULL DEFAULT ''")
        self.conn.commit()

    def _ensure_column(self, column_name: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(trips)").fetchall()
        }
        if column_name not in columns:
            self.conn.execute(f"ALTER TABLE trips ADD COLUMN {column_name} {definition}")

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
                return_datetime = :return_datetime,
                passenger_name = :passenger_name,
                checkin_done = :checkin_done,
                checklist_ticket_ready = :checklist_ticket_ready,
                checklist_checkin_done = :checklist_checkin_done,
                checklist_documentation_ready = :checklist_documentation_ready,
                checklist_transport_booked = :checklist_transport_booked,
                checklist_bags_ready = :checklist_bags_ready,
                checklist_packing_done = :checklist_packing_done,
                airline_code = :airline_code,
                flight_number = :flight_number,
                airline = :airline,
                booking_reference = :booking_reference,
                ticket_number = :ticket_number,
                seat = :seat,
                cabin_class = :cabin_class,
                ticket_notes = :ticket_notes,
                documentation_required = :documentation_required,
                outbound_transport_mode = :outbound_transport_mode,
                outbound_transport_provider = :outbound_transport_provider,
                outbound_leave_home = :outbound_leave_home,
                outbound_arrive_airport = :outbound_arrive_airport,
                outbound_transport_notes = :outbound_transport_notes,
                return_transport_mode = :return_transport_mode,
                return_transport_provider = :return_transport_provider,
                return_leave_airport = :return_leave_airport,
                return_arrive_home = :return_arrive_home,
                return_transport_notes = :return_transport_notes,
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


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.title_text = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(id="confirm-card"):
            yield Static(self.title_text, classes="dialog-title")
            yield Static(self.message, classes="dialog-message")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Delete", variant="error", id="confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)


class InfoScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Container(id="confirm-card"):
            yield Static("About Air Trip Admin", classes="dialog-title")
            yield Static(
                "\n".join(
                    [
                        "Manage airplane trips with categorized tabs and SQLite storage.",
                        "",
                        "Shortcuts:",
                        "n new trip",
                        "e edit selected trip",
                        "c duplicate selected trip",
                        "d delete selected trip",
                        "s refresh flight status",
                        "r refresh list",
                        "f focus filter",
                        "i show this info",
                        "q quit",
                        "",
                        "Flight validation provider is selected with FLIGHT_VALIDATION_PROVIDER.",
                        "OpenSky requires OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET.",
                    ]
                ),
                classes="dialog-message",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", variant="primary", id="close")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class TripEditorScreen(ModalScreen[Trip | None]):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, trip: Trip | None = None) -> None:
        super().__init__()
        self.trip = trip or Trip()

    def compose(self) -> ComposeResult:
        with Container(id="editor-card"):
            yield Static(
                "Create trip" if self.trip.id is None else f"Edit trip #{self.trip.id}",
                classes="dialog-title",
            )
            yield Static("Ctrl+S to save, Esc to cancel.", classes="dialog-message")
            with TabbedContent(id="editor-tabs"):
                with TabPane("Overview", id="editor-overview"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field("title", "Trip title", self.trip.title, required=True)
                        yield from self._field(
                            "departure_airport",
                            "Departure airport",
                            self.trip.departure_airport,
                            required=True,
                        )
                        yield from self._field(
                            "arrival_airport",
                            "Arrival airport",
                            self.trip.arrival_airport,
                            required=True,
                        )
                        yield from self._field(
                            "departure_datetime",
                            f"Departure date/time ({DT_FORMAT})",
                            self.trip.departure_datetime,
                            required=True,
                        )
                        yield from self._field(
                            "return_datetime",
                            f"Return date/time ({DT_FORMAT})",
                            self.trip.return_datetime,
                        )
                with TabPane("Ticket", id="editor-ticket"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field("passenger_name", "Passenger name", self.trip.passenger_name)
                        yield from self._field("checkin_done", "Check-in completed", self.trip.checkin_done, widget="checkbox")
                        yield from self._field("airline_code", "Airline code (IATA/ICAO)", self.trip.airline_code)
                        yield from self._field("flight_number", "Flight number", self.trip.flight_number)
                        yield from self._field("airline", "Airline", self.trip.airline)
                        yield from self._field(
                            "booking_reference", "Booking reference", self.trip.booking_reference
                        )
                        yield from self._field("ticket_number", "Ticket number", self.trip.ticket_number)
                        yield from self._field("seat", "Seat", self.trip.seat)
                        yield from self._field("cabin_class", "Cabin class", self.trip.cabin_class)
                        yield from self._field(
                            "ticket_notes",
                            "Ticket notes",
                            self.trip.ticket_notes,
                            widget="textarea",
                        )
                with TabPane("Docs", id="editor-docs"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field(
                            "documentation_required",
                            "Documentation required",
                            join_items(self.trip.documentation_required),
                        )
                        yield from self._field(
                            "documents_to_carry",
                            "Documents to carry",
                            join_items(self.trip.documents_to_carry),
                        )
                with TabPane("Transport", id="editor-transport"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field(
                            "outbound_transport_mode",
                            "Outbound transport mode",
                            self.trip.outbound_transport_mode,
                        )
                        yield from self._field(
                            "outbound_transport_provider",
                            "Outbound transport provider",
                            self.trip.outbound_transport_provider,
                        )
                        yield from self._field(
                            "outbound_leave_home",
                            f"Leave home ({DT_FORMAT})",
                            self.trip.outbound_leave_home,
                        )
                        yield from self._field(
                            "outbound_arrive_airport",
                            f"Arrive airport ({DT_FORMAT})",
                            self.trip.outbound_arrive_airport,
                        )
                        yield from self._field(
                            "outbound_transport_notes",
                            "Outbound transport notes",
                            self.trip.outbound_transport_notes,
                            widget="textarea",
                        )
                        yield from self._field(
                            "return_transport_mode",
                            "Return transport mode",
                            self.trip.return_transport_mode,
                        )
                        yield from self._field(
                            "return_transport_provider",
                            "Return transport provider",
                            self.trip.return_transport_provider,
                        )
                        yield from self._field(
                            "return_leave_airport",
                            f"Leave airport ({DT_FORMAT})",
                            self.trip.return_leave_airport,
                        )
                        yield from self._field(
                            "return_arrive_home",
                            f"Arrive home ({DT_FORMAT})",
                            self.trip.return_arrive_home,
                        )
                        yield from self._field(
                            "return_transport_notes",
                            "Return transport notes",
                            self.trip.return_transport_notes,
                            widget="textarea",
                        )
                with TabPane("Packing", id="editor-packing"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field(
                            "clothes_items",
                            "Clothes items",
                            join_items(self.trip.clothes_items),
                        )
                        yield from self._field(
                            "electronics_items",
                            "Electronics items",
                            join_items(self.trip.electronics_items),
                        )
                        yield from self._field(
                            "health_items",
                            "Health items",
                            join_items(self.trip.health_items),
                        )
                        yield from self._field(
                            "other_items",
                            "Other items",
                            join_items(self.trip.other_items),
                        )
                with TabPane("Checklist", id="editor-checklist"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field(
                            "checklist_ticket_ready",
                            "Ticket ready",
                            self.trip.checklist_ticket_ready,
                            widget="checkbox",
                        )
                        yield from self._field(
                            "checklist_checkin_done",
                            "Check-in completed",
                            self.trip.checklist_checkin_done,
                            widget="checkbox",
                        )
                        yield from self._field(
                            "checklist_documentation_ready",
                            "Documentation ready",
                            self.trip.checklist_documentation_ready,
                            widget="checkbox",
                        )
                        yield from self._field(
                            "checklist_transport_booked",
                            "Transport tickets/reservations ready",
                            self.trip.checklist_transport_booked,
                            widget="checkbox",
                        )
                        yield from self._field(
                            "checklist_bags_ready",
                            "Bags ready",
                            self.trip.checklist_bags_ready,
                            widget="checkbox",
                        )
                        yield from self._field(
                            "checklist_packing_done",
                            "Packing completed",
                            self.trip.checklist_packing_done,
                            widget="checkbox",
                        )
                with TabPane("Notes", id="editor-notes"):
                    with VerticalScroll(classes="editor-tab"):
                        yield from self._field(
                            "general_notes",
                            "General notes",
                            self.trip.general_notes,
                            widget="textarea",
                        )
            yield Static("", id="editor-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Save", variant="success", id="save")

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
            yield Label(f"{label}{' *' if required else ''}")
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
            return_datetime=optional_dt("return_datetime", "Return date/time"),
            passenger_name=self._get_input("passenger_name"),
            checkin_done=self._get_checkbox("checkin_done"),
            checklist_ticket_ready=self._get_checkbox("checklist_ticket_ready"),
            checklist_checkin_done=self._get_checkbox("checklist_checkin_done"),
            checklist_documentation_ready=self._get_checkbox("checklist_documentation_ready"),
            checklist_transport_booked=self._get_checkbox("checklist_transport_booked"),
            checklist_bags_ready=self._get_checkbox("checklist_bags_ready"),
            checklist_packing_done=self._get_checkbox("checklist_packing_done"),
            airline_code=self._get_input("airline_code").upper(),
            flight_number=self._get_input("flight_number").upper(),
            airline=self._get_input("airline"),
            booking_reference=self._get_input("booking_reference"),
            ticket_number=self._get_input("ticket_number"),
            seat=self._get_input("seat"),
            cabin_class=self._get_input("cabin_class"),
            ticket_notes=self._get_textarea("ticket_notes"),
            documentation_required=split_items(self._get_input("documentation_required")),
            outbound_transport_mode=self._get_input("outbound_transport_mode"),
            outbound_transport_provider=self._get_input("outbound_transport_provider"),
            outbound_leave_home=optional_dt("outbound_leave_home", "Outbound leave home"),
            outbound_arrive_airport=optional_dt("outbound_arrive_airport", "Outbound arrive airport"),
            outbound_transport_notes=self._get_textarea("outbound_transport_notes"),
            return_transport_mode=self._get_input("return_transport_mode"),
            return_transport_provider=self._get_input("return_transport_provider"),
            return_leave_airport=optional_dt("return_leave_airport", "Return leave airport"),
            return_arrive_home=optional_dt("return_arrive_home", "Return arrive home"),
            return_transport_notes=self._get_textarea("return_transport_notes"),
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_cancel()


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

    .editor-textarea {
        height: 8;
        min-height: 8;
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

    BINDINGS = [
        Binding("n", "new_trip", "New"),
        Binding("e", "edit_trip", "Edit"),
        Binding("c", "duplicate_trip", "Copy"),
        Binding("d", "delete_trip", "Delete"),
        Binding("i", "show_info", "Info"),
        Binding("s", "refresh_flight_status", "Flight Status"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "focus_filter", "Filter"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.store = TripStore(DB_PATH)
        self._filter_text = ""
        self._selected_trip_id: int | None = None
        self._flight_status_cache: dict[int, FlightStatusResult] = {}
        self._status_log: list[str] = []
        self.flight_provider = self._build_flight_provider()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="workspace"):
            with Vertical(id="left-pane"):
                yield Input(placeholder="Filter trips by title, airport, or booking ref", id="trip-filter")
                with Horizontal(classes="dialog-buttons"):
                    yield Button("New", variant="success", id="new-trip")
                    yield Button("Edit", variant="primary", id="edit-trip")
                    yield Button("Duplicate", variant="default", id="duplicate-trip")
                yield ListView(id="trip-list")
                yield Static("", id="status-line")
            with Vertical(id="right-pane"):
                yield Static("Trip details", classes="dialog-title")
                with TabbedContent(id="trip-tabs"):
                    with TabPane("Overview", id="overview"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="overview-view")
                    with TabPane("Ticket", id="ticket"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="ticket-view")
                    with TabPane("Docs", id="docs"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="docs-view")
                    with TabPane("Transport", id="transport"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="transport-view")
                    with TabPane("Packing", id="packing"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="packing-view")
                    with TabPane("Flight", id="flight"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="flight-view")
                    with TabPane("Checklist", id="checklist"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="checklist-view")
                    with TabPane("Notes", id="notes"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="notes-view")
                    with TabPane("Summary", id="summary"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="summary-view")
                    with TabPane("Log", id="log"):
                        with VerticalScroll(classes="detail-pane"):
                            yield Static("", id="log-view")
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_trip_list()

    def _set_status(self, message: str) -> None:
        self.query_one("#status-line", Static).update(message)

    def _append_status_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._status_log.append(f"[{timestamp}] {message}")
        self.query_one("#log-view", Static).update(panel("Status log", self._status_log[-200:]))

    def _build_flight_provider(self) -> FlightValidationProvider:
        provider_name = os.environ.get("FLIGHT_VALIDATION_PROVIDER", DEFAULT_FLIGHT_PROVIDER).strip().lower()
        if provider_name in ("opensky", ""):
            return OpenSkyFlightProvider()
        if provider_name in ("placeholder", "schedule-placeholder"):
            return PlaceholderScheduleValidationProvider()
        return PlaceholderScheduleValidationProvider()

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
            self._clear_details("No trips saved yet." if not self._filter_text else "No trips match the filter.")
            self._set_status("No trips found.")
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
        self._set_status(f"{len(trips)} trip(s) loaded.")

    def _clear_details(self, message: str) -> None:
        placeholder = panel("No trip selected", [message])
        for widget_id in (
            "overview-view",
            "ticket-view",
            "docs-view",
            "transport-view",
            "packing-view",
            "flight-view",
            "checklist-view",
            "notes-view",
            "summary-view",
            "log-view",
        ):
            self.query_one(f"#{widget_id}", Static).update(placeholder)
        self.query_one("#log-view", Static).update(panel("Status log", self._status_log[-200:] or ["No status messages yet."]))

    def _show_trip(self, trip: Trip | None) -> None:
        if trip is None:
            self._clear_details("Select a trip from the list on the left.")
            return

        checklist_pairs = [
            ("Ticket ready", trip.checklist_ticket_ready),
            ("Check-in completed", trip.checklist_checkin_done),
            ("Documentation ready", trip.checklist_documentation_ready),
            ("Transport tickets/reservations ready", trip.checklist_transport_booked),
            ("Bags ready", trip.checklist_bags_ready),
            ("Packing completed", trip.checklist_packing_done),
        ]
        completed_steps = sum(1 for _, done in checklist_pairs if done)

        overview_lines = [
            f"Title: {value_or_none(trip.title)}",
            f"Route: {value_or_none(trip.departure_airport)} -> {value_or_none(trip.arrival_airport)}",
            f"Departure: {value_or_none(trip.departure_datetime)}",
            f"Return: {value_or_none(trip.return_datetime)}",
            f"Checklist progress: {completed_steps}/{len(checklist_pairs)} completed",
            f"Created at: {value_or_none(trip.created_at)}",
            f"Updated at: {value_or_none(trip.updated_at)}",
        ]
        ticket_lines = [
            f"Passenger name: {value_or_none(trip.passenger_name)}",
            f"Check-in done: {'[x] Yes' if trip.checkin_done else '[ ] No'}",
            f"Airline code: {value_or_none(trip.airline_code)}",
            f"Flight number: {value_or_none(trip.flight_number)}",
            f"Airline: {value_or_none(trip.airline)}",
            f"Booking reference: {value_or_none(trip.booking_reference)}",
            f"Ticket number: {value_or_none(trip.ticket_number)}",
            f"Seat: {value_or_none(trip.seat)}",
            f"Cabin class: {value_or_none(trip.cabin_class)}",
            f"Notes: {value_or_none(trip.ticket_notes)}",
        ]
        flight_lines = self._flight_status_lines(trip)
        docs_lines = [
            "Required documentation:",
            bullets(trip.documentation_required),
            "",
            "Documents to carry:",
            bullets(trip.documents_to_carry),
        ]
        transport_lines = [
            "Outbound",
            "-" * 8,
            f"Mode: {value_or_none(trip.outbound_transport_mode)}",
            f"Provider: {value_or_none(trip.outbound_transport_provider)}",
            f"Leave home: {value_or_none(trip.outbound_leave_home)}",
            f"Arrive airport: {value_or_none(trip.outbound_arrive_airport)}",
            f"Notes: {value_or_none(trip.outbound_transport_notes)}",
            "",
            "Return",
            "-" * 6,
            f"Mode: {value_or_none(trip.return_transport_mode)}",
            f"Provider: {value_or_none(trip.return_transport_provider)}",
            f"Leave airport: {value_or_none(trip.return_leave_airport)}",
            f"Arrive home: {value_or_none(trip.return_arrive_home)}",
            f"Notes: {value_or_none(trip.return_transport_notes)}",
        ]
        packing_lines = [
            "Clothes:",
            bullets(trip.clothes_items),
            "",
            "Electronics:",
            bullets(trip.electronics_items),
            "",
            "Health:",
            bullets(trip.health_items),
            "",
            "Other items:",
            bullets(trip.other_items),
        ]
        checklist_lines = [
            f"{'[x]' if done else '[ ]'} {label}"
            for label, done in checklist_pairs
        ]
        notes_lines = [
            trip.general_notes.strip() if trip.general_notes.strip() else "(none)",
        ]
        summary_lines = [
            f"Trip: {value_or_none(trip.title)}",
            f"Passenger: {value_or_none(trip.passenger_name)}",
            f"Route: {value_or_none(trip.departure_airport)} -> {value_or_none(trip.arrival_airport)}",
            f"Departure: {value_or_none(trip.departure_datetime)}",
            f"Return: {value_or_none(trip.return_datetime)}",
            f"Flight: {value_or_none(f'{trip.airline_code}{trip.flight_number}'.strip())}",
            f"Airline: {value_or_none(trip.airline)}",
            f"Booking reference: {value_or_none(trip.booking_reference)}",
            f"Check-in: {'done' if trip.checkin_done else 'pending'}",
            f"Checklist: {completed_steps}/{len(checklist_pairs)} completed",
            "",
            "Required documentation:",
            bullets(trip.documentation_required),
            "",
            "Packing snapshot:",
            f"Clothes {len(trip.clothes_items)} | Electronics {len(trip.electronics_items)} | Health {len(trip.health_items)} | Other {len(trip.other_items)}",
            "",
            "Transport snapshot:",
            f"Outbound {value_or_none(trip.outbound_transport_mode)} | Return {value_or_none(trip.return_transport_mode)}",
        ]

        self.query_one("#overview-view", Static).update(panel("Overview", overview_lines))
        self.query_one("#ticket-view", Static).update(panel("Ticket details", ticket_lines))
        self.query_one("#docs-view", Static).update(panel("Documentation", docs_lines))
        self.query_one("#transport-view", Static).update(panel("Transport", transport_lines))
        self.query_one("#packing-view", Static).update(panel("Packing lists", packing_lines))
        self.query_one("#flight-view", Static).update(panel("Flight status", flight_lines))
        self.query_one("#checklist-view", Static).update(panel("Trip checklist", checklist_lines))
        self.query_one("#notes-view", Static).update(panel("Notes", notes_lines))
        self.query_one("#summary-view", Static).update(panel("Trip summary", summary_lines))
        self.query_one("#log-view", Static).update(panel("Status log", self._status_log[-200:] or ["No status messages yet."]))

    def _flight_status_lines(self, trip: Trip) -> list[str]:
        flight_id = f"{trip.airline_code}{trip.flight_number}".strip()
        if trip.id is None:
            return ["Save the trip before querying live flight status."]
        cached = self._flight_status_cache.get(trip.id)
        if cached is None:
            return [
                f"Provider: {self.flight_provider.provider_name}",
                f"Lookup key: {flight_id or '(missing airline code / flight number)'}",
                f"Departure date: {departure_date(trip.departure_datetime) or '(missing departure date)'}",
                "",
                "Press 's' or the Status button to query live flight data.",
                "Choose provider with FLIGHT_VALIDATION_PROVIDER.",
            ]
        return [
            f"Provider: {self.flight_provider.provider_name}",
            f"Lookup key: {flight_id or '(missing airline code / flight number)'}",
            f"Fetched at: {cached.fetched_at}",
            f"Result: {cached.summary}",
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
        self._set_status("Trip saved.")

    async def _duplicate_trip_and_refresh(self, trip: Trip) -> None:
        duplicated = trip.duplicate()
        trip_id = self.store.save_trip(duplicated)
        duplicated.id = trip_id
        await self.refresh_trip_list(select_trip_id=trip_id)
        self._append_status_log(f"Duplicated trip '{trip.title}' into '{duplicated.title}'.")
        self._set_status("Trip duplicated.")

    async def _delete_trip_and_refresh(self, trip_id: int) -> None:
        self.store.delete_trip(trip_id)
        self._selected_trip_id = None
        await self.refresh_trip_list()
        self._set_status("Trip deleted.")

    def _open_editor(self, trip: Trip | None = None) -> None:
        def _on_dismissed(result: Trip | None) -> None:
            if result is None:
                return
            self.run_worker(self._save_trip_and_refresh(result), name="save-trip")

        self.push_screen(TripEditorScreen(trip), callback=_on_dismissed)

    async def action_new_trip(self) -> None:
        self._open_editor()

    async def action_edit_trip(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status("Select a trip first.")
            return
        self._open_editor(trip)

    async def action_delete_trip(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status("Select a trip first.")
            return

        def _on_confirmed(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._delete_trip_and_refresh(trip.id or 0), name="delete-trip")

        self.push_screen(
            ConfirmScreen("Delete trip", f"Delete '{trip.title}'? This cannot be undone."),
            callback=_on_confirmed,
        )

    async def action_duplicate_trip(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status("Select a trip first.")
            return
        self.run_worker(self._duplicate_trip_and_refresh(trip), name="duplicate-trip")

    async def action_refresh(self) -> None:
        await self.refresh_trip_list(self._selected_trip_id)

    async def action_refresh_flight_status(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status("Select a trip first.")
            self._append_status_log("Flight status request failed: no trip selected.")
            return
        self._set_status("Fetching live flight status...")
        self._append_status_log(
            f"Starting flight status request with provider {self.flight_provider.provider_name} for {trip.title or trip.id} using key {trip.airline_code}{trip.flight_number}."
        )
        self.run_worker(self._refresh_flight_status_for_trip(trip), name="flight-status", exclusive=True)

    def action_show_info(self) -> None:
        self.push_screen(InfoScreen())

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

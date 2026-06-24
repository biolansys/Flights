#!/usr/bin/env python3
"""PySide6 desktop GUI for Air Trip Admin."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import (
    AppSettings,
    DB_PATH,
    DT_FORMAT,
    DEFAULT_FLIGHT_PROVIDER,
    DEFAULT_LANGUAGE,
    ChecklistItem,
    FlightStatusResult,
    LinkItem,
    OpenSkyFlightProvider,
    PlaceholderScheduleValidationProvider,
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
    now_iso,
    parse_datetime,
    parse_minutes,
    parse_timing_steps_text,
    split_items,
    t,
    timing_steps_to_text,
    total_trip_cost,
    value_or_none,
)


LIST_FIELDS = {
    "documentation_required",
    "documents_to_carry",
    "clothes_items",
    "electronics_items",
    "health_items",
    "other_items",
}

TEXTAREA_FIELDS = {
    "ticket_notes",
    "documentation_required",
    "documents_to_carry",
    "clothes_items",
    "electronics_items",
    "health_items",
    "other_items",
    "general_notes",
}

CHECKBOX_FIELDS = {"checkin_done"}

EDITOR_TABS: list[tuple[str, list[str]]] = [
    ("overview", ["title", "departure_airport", "arrival_airport", "departure_datetime", "flight_arrival_time"]),
    (
        "ticket",
        [
            "passenger_name",
            "checkin_done",
            "airline_code",
            "flight_number",
            "airline",
            "booking_reference",
            "ticket_number",
            "ticket_cost",
            "seat",
            "cabin_class",
            "ticket_notes",
        ],
    ),
    ("docs", ["documentation_required", "documents_to_carry"]),
    ("timing", ["timing_bundle"]),
    ("packing", ["clothes_items", "electronics_items", "health_items", "other_items"]),
    ("checklist", ["checklist_items"]),
    ("notes", ["general_notes"]),
    ("links", ["links_of_interest"]),
]


def lines_or_none(items: Iterable[str]) -> str:
    cleaned = [item.strip() for item in items if item and item.strip()]
    return "\n".join(cleaned) if cleaned else "(none)"


def make_item(text: str, *, align_right: bool = False, color: QColor | None = None, bold: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if align_right:
        item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
    if color is not None:
        item.setForeground(color)
    if bold:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def status_item(text: str, ok: bool) -> QTableWidgetItem:
    return make_item(text, color=QColor("#15803d" if ok else "#b45309"), bold=True)


def severity_item(text: str) -> QTableWidgetItem:
    lowered = text.lower()
    if any(token in lowered for token in ("error", "failed", "missing", "cancel", "exception", "denied")):
        return make_item(text, color=QColor("#b91c1c"), bold=True)
    if any(token in lowered for token in ("warning", "unavailable", "cannot", "invalid", "pending", "no ")):
        return make_item(text, color=QColor("#b45309"), bold=True)
    if any(token in lowered for token in ("updated", "ok", "done", "completed", "ready", "success")):
        return make_item(text, color=QColor("#15803d"), bold=True)
    return make_item(text)


def departure_datetime_value(trip: Trip) -> datetime | None:
    if not trip.departure_datetime:
        return None
    try:
        return datetime.strptime(trip.departure_datetime, DT_FORMAT)
    except ValueError:
        return None

class PreferencesDialog(QDialog):
    def __init__(self, language: str, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.language = language
        self.setWindowTitle(t(language, "preferences_title"))
        self.resize(460, 240)
        root = QVBoxLayout(self)

        layout = QFormLayout()
        self.language_combo = QComboBox()
        self.language_values = ["en", "es", "de"]
        for code in self.language_values:
            self.language_combo.addItem(t(code, "lang_name"), code)
        self.language_combo.setCurrentIndex(self.language_values.index(settings.language if settings.language in self.language_values else DEFAULT_LANGUAGE))
        layout.addRow(t(language, "language"), self.language_combo)

        self.provider_combo = QComboBox()
        self.provider_combo.addItem("OpenSky", "opensky")
        self.provider_combo.addItem("Placeholder", "placeholder")
        provider_value = settings.flight_provider if settings.flight_provider in {"opensky", "placeholder", "schedule-placeholder"} else DEFAULT_FLIGHT_PROVIDER
        provider_index = 0 if provider_value == "opensky" else 1
        self.provider_combo.setCurrentIndex(provider_index)
        layout.addRow(t(language, "flight_provider_setting"), self.provider_combo)

        layout.addRow(t(language, "database_path_label"), QLabel(str(DB_PATH)))
        layout.addRow(t(language, "date_format_label"), QLabel(DT_FORMAT))
        root.addLayout(layout)

        note = QLabel(t(language, "preferences_provider_hint"))
        note.setWordWrap(True)
        note.setStyleSheet("color: #7c6a58;")
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def selected_language(self) -> str:
        return str(self.language_combo.currentData())

    def selected_provider(self) -> str:
        return str(self.provider_combo.currentData())


class TripCardWidget(QWidget):
    def __init__(self, language: str, trip: Trip) -> None:
        super().__init__()
        self.setStyleSheet(
            "background: #fcfaf6; border: 1px solid #e7dccb; border-radius: 6px;"
        )
        complete, total = checklist_progress(trip)
        total_cost = total_trip_cost(trip)
        route = f"{value_or_none(trip.departure_airport)} -> {value_or_none(trip.arrival_airport)}"
        when = value_or_none(trip.departure_datetime)
        passenger = value_or_none(trip.passenger_name)
        cost_text = format_cost(total_cost) if total_cost is not None else "(none)"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        title = QLabel(value_or_none(trip.title))
        title.setStyleSheet("font-weight: 800; color: #4a2d18;")
        title.setWordWrap(True)
        layout.addWidget(title)

        route_line = QLabel(f"{when} | {route}")
        route_line.setStyleSheet("color: #5f4b36;")
        route_line.setWordWrap(True)
        layout.addWidget(route_line)

        meta_line = QLabel(
            f"{t(language, 'passenger_label')}: {passenger} | "
            f"{t(language, 'checklist_progress_label')}: {complete}/{total} | "
            f"{t(language, 'total_cost_label')}: {cost_text}"
        )
        meta_line.setStyleSheet("color: #876a4d; font-size: 11px;")
        meta_line.setWordWrap(True)
        layout.addWidget(meta_line)


class TimingStepsEditor(QWidget):
    def __init__(self, language: str, steps: list[TimingStep] | None = None) -> None:
        super().__init__()
        self.language = language
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        add_button = QPushButton(t(language, "add_step"))
        update_button = QPushButton(t(language, "update_step"))
        remove_button = QPushButton(t(language, "remove_step"))
        up_button = QPushButton(t(language, "move_step_up"))
        down_button = QPushButton(t(language, "move_step_down"))
        add_button.clicked.connect(self.add_row)
        update_button.clicked.connect(self.update_current_row)
        remove_button.clicked.connect(self.remove_current_row)
        up_button.clicked.connect(lambda: self.move_current_row(-1))
        down_button.clicked.connect(lambda: self.move_current_row(1))
        controls.addWidget(add_button)
        controls.addWidget(update_button)
        controls.addWidget(remove_button)
        controls.addWidget(up_button)
        controls.addWidget(down_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([t(language, "step_name_label"), t(language, "step_minutes_label"), t(language, "step_cost_label")])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.viewport().setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropOverwriteMode(False)
        layout.addWidget(self.table, 1)

        hint = QLabel(t(language, "timing_steps_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7c6a58;")
        layout.addWidget(hint)

        for step in steps or []:
            self.add_row(step.name, str(step.minutes), step.cost)
        if self.table.rowCount() == 0:
            self.add_row()

    def add_row(self, name: str = "", minutes: str = "", cost: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(minutes))
        self.table.setItem(row, 2, QTableWidgetItem(cost))

    def remove_current_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def update_current_row(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        name_item = self.table.item(row, 0)
        minutes_item = self.table.item(row, 1)
        if name_item is not None:
            name_item.setText(name_item.text().strip())
        if minutes_item is not None:
            minutes_item.setText(minutes_item.text().strip())
        cost_item = self.table.item(row, 2)
        if cost_item is not None:
            cost_item.setText(cost_item.text().strip())

    def move_current_row(self, delta: int) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self.table.rowCount():
            return
        row_values = [
            (self.table.item(row, column).text() if self.table.item(row, column) else "")
            for column in range(3)
        ]
        self.table.removeRow(row)
        self.table.insertRow(new_row)
        for column, value in enumerate(row_values):
            self.table.setItem(new_row, column, QTableWidgetItem(value))
        self.table.setCurrentCell(new_row, 0)

    def get_steps(self) -> tuple[list[TimingStep], list[str]]:
        lines: list[str] = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            minutes_item = self.table.item(row, 1)
            cost_item = self.table.item(row, 2)
            name = (name_item.text() if name_item else "").strip()
            minutes = (minutes_item.text() if minutes_item else "").strip()
            cost = (cost_item.text() if cost_item else "").strip()
            if not name and not minutes and not cost:
                continue
            lines.append(f"{name} | {minutes} | {cost}")
        return parse_timing_steps_text("\n".join(lines))


class DualTimingEditor(QWidget):
    def __init__(self, language: str, departure_steps: list[TimingStep] | None = None, arrival_steps: list[TimingStep] | None = None) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        departure_page = QWidget()
        departure_layout = QVBoxLayout(departure_page)
        self.departure_editor = TimingStepsEditor(language, departure_steps)
        departure_layout.addWidget(self.departure_editor)
        self.tabs.addTab(departure_page, t(language, "departure_timing_section"))
        arrival_page = QWidget()
        arrival_layout = QVBoxLayout(arrival_page)
        self.arrival_editor = TimingStepsEditor(language, arrival_steps)
        arrival_layout.addWidget(self.arrival_editor)
        self.tabs.addTab(arrival_page, t(language, "arrival_timing_section"))
        layout.addWidget(self.tabs)

    def get_steps(self) -> tuple[list[TimingStep], list[TimingStep], list[str]]:
        departure_steps, departure_errors = self.departure_editor.get_steps()
        arrival_steps, arrival_errors = self.arrival_editor.get_steps()
        return departure_steps, arrival_steps, [*departure_errors, *arrival_errors]


class ChecklistItemsEditor(QWidget):
    def __init__(self, language: str, items: list[ChecklistItem] | None = None) -> None:
        super().__init__()
        self.language = language
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        add_button = QPushButton(t(language, "add_checklist_item"))
        update_button = QPushButton(t(language, "update_checklist_item"))
        remove_button = QPushButton(t(language, "remove_checklist_item"))
        up_button = QPushButton(t(language, "move_checklist_item_up"))
        down_button = QPushButton(t(language, "move_checklist_item_down"))
        add_button.clicked.connect(self.add_row)
        update_button.clicked.connect(self.update_current_row)
        remove_button.clicked.connect(self.remove_current_row)
        up_button.clicked.connect(lambda: self.move_current_row(-1))
        down_button.clicked.connect(lambda: self.move_current_row(1))
        controls.addWidget(add_button)
        controls.addWidget(update_button)
        controls.addWidget(remove_button)
        controls.addWidget(up_button)
        controls.addWidget(down_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([t(language, "checklist_item_name_label"), t(language, "checklist_item_done_label")])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.viewport().setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropOverwriteMode(False)
        layout.addWidget(self.table, 1)

        hint = QLabel(t(language, "checklist_items_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7c6a58;")
        layout.addWidget(hint)

        for item in items or []:
            self.add_row(item.name, item.done)
        if self.table.rowCount() == 0:
            self.add_row()

    def add_row(self, name: str = "", done: bool = False) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        done_item = QTableWidgetItem("")
        done_item.setFlags(done_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        done_item.setCheckState(Qt.CheckState.Checked if done else Qt.CheckState.Unchecked)
        self.table.setItem(row, 1, done_item)

    def remove_current_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def update_current_row(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        name_item = self.table.item(row, 0)
        if name_item is not None:
            name_item.setText(name_item.text().strip())

    def move_current_row(self, delta: int) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self.table.rowCount():
            return
        name_item = self.table.item(row, 0)
        done_item = self.table.item(row, 1)
        name = name_item.text() if name_item else ""
        done = done_item.checkState() == Qt.CheckState.Checked if done_item else False
        self.table.removeRow(row)
        self.table.insertRow(new_row)
        self.table.setItem(new_row, 0, QTableWidgetItem(name))
        moved_done_item = QTableWidgetItem("")
        moved_done_item.setFlags(moved_done_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        moved_done_item.setCheckState(Qt.CheckState.Checked if done else Qt.CheckState.Unchecked)
        self.table.setItem(new_row, 1, moved_done_item)
        self.table.setCurrentCell(new_row, 0)

    def get_items(self) -> tuple[list[ChecklistItem], list[str]]:
        items: list[ChecklistItem] = []
        errors: list[str] = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            done_item = self.table.item(row, 1)
            name = (name_item.text() if name_item else "").strip()
            if not name and done_item is not None and done_item.checkState() == Qt.CheckState.Unchecked:
                continue
            if not name:
                errors.append(f"Checklist item row {row + 1} requires a name")
                continue
            done = done_item.checkState() == Qt.CheckState.Checked if done_item is not None else False
            items.append(ChecklistItem(name=name, done=done))
        return items, errors


class LinksEditor(QWidget):
    def __init__(self, language: str, items: list[LinkItem] | None = None) -> None:
        super().__init__()
        self.language = language
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        add_button = QPushButton(t(language, "add_link"))
        update_button = QPushButton(t(language, "update_link"))
        remove_button = QPushButton(t(language, "remove_link"))
        up_button = QPushButton(t(language, "move_link_up"))
        down_button = QPushButton(t(language, "move_link_down"))
        add_button.clicked.connect(self.add_row)
        update_button.clicked.connect(self.update_current_row)
        remove_button.clicked.connect(self.remove_current_row)
        up_button.clicked.connect(lambda: self.move_current_row(-1))
        down_button.clicked.connect(lambda: self.move_current_row(1))
        controls.addWidget(add_button)
        controls.addWidget(update_button)
        controls.addWidget(remove_button)
        controls.addWidget(up_button)
        controls.addWidget(down_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([t(language, "link_name_label"), t(language, "link_url_label")])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.viewport().setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropOverwriteMode(False)
        layout.addWidget(self.table, 1)

        hint = QLabel(t(language, "links_of_interest_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7c6a58;")
        layout.addWidget(hint)

        for item in items or []:
            self.add_row(item.name, item.url)
        if self.table.rowCount() == 0:
            self.add_row()

    def add_row(self, name: str = "", url: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(url))

    def remove_current_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def update_current_row(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        for column in range(2):
            item = self.table.item(row, column)
            if item is not None:
                item.setText(item.text().strip())

    def move_current_row(self, delta: int) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self.table.rowCount():
            return
        row_values = [
            (self.table.item(row, column).text() if self.table.item(row, column) else "")
            for column in range(2)
        ]
        self.table.removeRow(row)
        self.table.insertRow(new_row)
        for column, value in enumerate(row_values):
            self.table.setItem(new_row, column, QTableWidgetItem(value))
        self.table.setCurrentCell(new_row, 0)

    def get_items(self) -> tuple[list[LinkItem], list[str]]:
        items: list[LinkItem] = []
        errors: list[str] = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            url_item = self.table.item(row, 1)
            name = (name_item.text() if name_item else "").strip()
            url = (url_item.text() if url_item else "").strip()
            if not name and not url:
                continue
            if not url:
                errors.append(f"Link row {row + 1} requires a URL")
                continue
            items.append(LinkItem(name=name, url=url))
        return items, errors


class TripEditorDialog(QDialog):
    def __init__(self, language: str, trip: Trip | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.language = language
        self.trip = trip or Trip()
        self.widgets: dict[str, QWidget] = {}
        self.setWindowTitle(
            t(language, "create_trip") if self.trip.id is None else t(language, "edit_trip_title", id=self.trip.id)
        )
        self.resize(760, 760)

        root = QVBoxLayout(self)
        root.addWidget(QLabel(t(language, "save_hint")))

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        for tab_key, fields in EDITOR_TABS:
            page = QWidget()
            layout = QFormLayout(page)
            layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            for field_name in fields:
                widget = self._make_widget(field_name)
                self.widgets[field_name] = widget
                label = QLabel(self._field_label(field_name))
                if isinstance(widget, QCheckBox) or field_name == "timing_bundle":
                    layout.addRow(widget)
                else:
                    layout.addRow(label, widget)
            self.tabs.addTab(page, t(language, tab_key))

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #b91c1c;")
        root.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _field_label(self, field_name: str) -> str:
        if field_name == "timing_bundle":
            return ""
        if field_name in {"departure_datetime", "flight_arrival_time"}:
            return t(self.language, field_name, format=DT_FORMAT)
        return t(self.language, field_name)

    def _make_widget(self, field_name: str) -> QWidget:
        current = getattr(self.trip, field_name) if hasattr(self.trip, field_name) else None
        if field_name == "timing_bundle":
            return DualTimingEditor(self.language, self.trip.timing_steps, self.trip.arrival_timing_steps)
        if field_name == "timing_steps":
            return TimingStepsEditor(self.language, current)
        if field_name == "checklist_items":
            return ChecklistItemsEditor(self.language, current)
        if field_name == "links_of_interest":
            return LinksEditor(self.language, current)
        if field_name in CHECKBOX_FIELDS:
            widget = QCheckBox(self._field_label(field_name))
            widget.setChecked(bool(current))
            return widget
        if field_name in TEXTAREA_FIELDS:
            widget = QPlainTextEdit()
            if field_name in LIST_FIELDS:
                widget.setPlainText("\n".join(current))
            else:
                widget.setPlainText(current or "")
            widget.setMinimumHeight(180)
            return widget
        widget = QLineEdit()
        widget.setText(current or "")
        return widget

    def _line(self, field_name: str) -> str:
        return self.widgets[field_name].text().strip()  # type: ignore[union-attr]

    def _text(self, field_name: str) -> str:
        return self.widgets[field_name].toPlainText().strip()  # type: ignore[union-attr]

    def _check(self, field_name: str) -> bool:
        return self.widgets[field_name].isChecked()  # type: ignore[union-attr]

    def _timing_steps(self) -> tuple[list[TimingStep], list[str]]:
        return self.widgets["timing_steps"].get_steps()  # type: ignore[union-attr]

    def _timing_bundle(self) -> tuple[list[TimingStep], list[TimingStep], list[str]]:
        return self.widgets["timing_bundle"].get_steps()  # type: ignore[union-attr]

    def _checklist_items(self) -> tuple[list[ChecklistItem], list[str]]:
        return self.widgets["checklist_items"].get_items()  # type: ignore[union-attr]

    def _links_of_interest(self) -> tuple[list[LinkItem], list[str]]:
        return self.widgets["links_of_interest"].get_items()  # type: ignore[union-attr]

    def _required(self, field_name: str, label: str, errors: list[str]) -> str:
        value = self._line(field_name)
        if not value:
            errors.append(f"{label} is required")
        return value

    def _optional_dt(self, field_name: str, label: str, errors: list[str], *, required: bool = False) -> str:
        value = self._line(field_name)
        if not value:
            if required:
                errors.append(f"{label} is required")
            return ""
        try:
            return parse_datetime(value)
        except ValueError:
            errors.append(f"{label} must use {DT_FORMAT}")
            return value

    def get_trip(self) -> Trip | None:
        errors: list[str] = []
        trip = Trip(
            id=self.trip.id,
            title=self._required("title", "Trip title", errors),
            departure_airport=self._required("departure_airport", "Departure airport", errors),
            arrival_airport=self._required("arrival_airport", "Arrival airport", errors),
            departure_datetime=self._optional_dt("departure_datetime", "Departure date/time", errors, required=True),
            flight_arrival_time=self._optional_dt("flight_arrival_time", "Flight arrival time", errors),
            passenger_name=self._line("passenger_name"),
            checkin_done=self._check("checkin_done"),
            checklist_items=[],
            airline_code=self._line("airline_code").upper(),
            flight_number=self._line("flight_number").upper(),
            airline=self._line("airline"),
            booking_reference=self._line("booking_reference"),
            ticket_number=self._line("ticket_number"),
            ticket_cost=self._line("ticket_cost"),
            seat=self._line("seat"),
            cabin_class=self._line("cabin_class"),
            ticket_notes=self._text("ticket_notes"),
            documentation_required=split_items(self._text("documentation_required")),
            documents_to_carry=split_items(self._text("documents_to_carry")),
            timing_steps=[],
            arrival_timing_steps=[],
            clothes_items=split_items(self._text("clothes_items")),
            electronics_items=split_items(self._text("electronics_items")),
            health_items=split_items(self._text("health_items")),
            other_items=split_items(self._text("other_items")),
            links_of_interest=[],
            general_notes=self._text("general_notes"),
            created_at=self.trip.created_at,
            updated_at=now_iso(),
        )
        departure_steps, arrival_steps, timing_errors = self._timing_bundle()
        errors.extend(timing_errors)
        trip.timing_steps = departure_steps
        trip.arrival_timing_steps = arrival_steps
        checklist_items, checklist_errors = self._checklist_items()
        errors.extend(checklist_errors)
        trip.checklist_items = checklist_items
        links_of_interest, links_errors = self._links_of_interest()
        errors.extend(links_errors)
        trip.links_of_interest = links_of_interest
        if errors:
            self.error_label.setText("; ".join(errors))
            return None
        self.error_label.setText("")
        return trip

    def accept(self) -> None:
        trip = self.get_trip()
        if trip is None:
            return
        self.trip = trip
        super().accept()


class TripAdminGui(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings.load()
        self.language = self.settings.language if self.settings.language in {"en", "es", "de"} else DEFAULT_LANGUAGE
        self.store = TripStore(DB_PATH)
        self.flight_provider = self._build_flight_provider()
        self._selected_trip_id: int | None = None
        self._status_log: list[str] = []
        self._flight_status_cache: dict[int, FlightStatusResult] = {}
        self._tabs: dict[str, QTableWidget] = {}
        self._tab_containers: dict[str, QWidget] = {}
        self._tab_titles: dict[str, QLabel] = {}
        self._actions: dict[str, QAction] = {}
        self._build_ui()
        self._reload_all()

    def _build_ui(self) -> None:
        self.setWindowTitle(t(self.language, "app_title"))
        self.resize(1400, 860)
        self.setStatusBar(QStatusBar())
        self._build_toolbar()
        self._build_menu()

        root = QSplitter()
        root.setOrientation(Qt.Orientation.Horizontal)
        self.setCentralWidget(root)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.left_title = QLabel(t(self.language, "app_title"))
        self.left_title.setStyleSheet("font-weight: 800; font-size: 18px; color: #7a431d;")
        left_layout.addWidget(self.left_title)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(t(self.language, "filter_placeholder"))
        self.filter_edit.textChanged.connect(self._reload_trip_list)
        left_layout.addWidget(self.filter_edit)
        button_row = QHBoxLayout()
        self.new_button = QPushButton(t(self.language, "new"))
        self.new_button.clicked.connect(self.create_trip)
        self.edit_button = QPushButton(t(self.language, "edit"))
        self.edit_button.clicked.connect(self.edit_trip)
        self.duplicate_button = QPushButton(t(self.language, "duplicate"))
        self.duplicate_button.clicked.connect(self.duplicate_trip)
        self.status_button = QPushButton(t(self.language, "flight_status"))
        self.status_button.clicked.connect(self.refresh_flight_status)
        for button in (self.new_button, self.edit_button, self.duplicate_button, self.status_button):
            button_row.addWidget(button)
        left_layout.addLayout(button_row)
        self.trip_list = QListWidget()
        self.trip_list.currentItemChanged.connect(self._on_trip_selected)
        left_layout.addWidget(self.trip_list, 1)
        self.left_hint = QLabel("")
        self.left_hint.setWordWrap(True)
        self.left_hint.setStyleSheet("color: #7c6a58;")
        left_layout.addWidget(self.left_hint)
        root.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.right_title = QLabel(t(self.language, "trip_details"))
        self.right_title.setStyleSheet("font-weight: 700; font-size: 16px; color: #593b24;")
        right_layout.addWidget(self.right_title)
        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, 1)
        root.addWidget(right)
        root.setStretchFactor(1, 1)

        self._add_table_tab("overview", t(self.language, "overview_panel_title"))
        self._add_table_tab("ticket", t(self.language, "ticket_panel_title"))
        self._add_table_tab("docs", t(self.language, "docs_panel_title"))
        self._add_table_tab("timing", t(self.language, "timing_panel_title"))
        self._add_table_tab("packing", t(self.language, "packing_panel_title"))
        self._add_table_tab("flight", t(self.language, "flight_panel_title"))
        self._add_table_tab("checklist", t(self.language, "checklist_panel_title"))
        self._add_table_tab("notes", t(self.language, "notes_panel_title"))
        self._add_table_tab("summary", t(self.language, "summary_panel_title"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.log_view, t(self.language, "log"))

        self.setStyleSheet(
            """
            QMainWindow { background: #f7f4ee; }
            QListWidget, QLineEdit, QPlainTextEdit, QTableWidget, QTabWidget::pane {
                background: white;
                border: 1px solid #d6c9b8;
            }
            QTableWidget {
                gridline-color: #eadfce;
                alternate-background-color: #fbf8f3;
                selection-background-color: #e9dcc2;
            }
            QToolBar {
                background: #efe5d4;
                border-bottom: 1px solid #d6c9b8;
                spacing: 6px;
                padding: 4px;
            }
            QHeaderView::section {
                background: #efe5d4;
                color: #3b2f1f;
                font-weight: 700;
                border: 0;
                border-bottom: 1px solid #d6c9b8;
                padding: 6px;
            }
            QTabBar::tab {
                background: #eadfce;
                color: #433424;
                padding: 8px 14px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #c8702e;
                color: white;
            }
            QPushButton {
                background: #efe5d4;
                border: 1px solid #d6c9b8;
                padding: 6px 10px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background: #e5d4bc;
            }
            QLabel {
                color: #3f3427;
            }
            """
        )
        self._update_hint()

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("main", self)
        self.addToolBar(toolbar)
        toolbar.setMovable(False)
        action_specs = [
            ("new", self.create_trip, "n"),
            ("edit", self.edit_trip, "e"),
            ("duplicate", self.duplicate_trip, "c"),
            ("delete", self.delete_trip, "d"),
            ("toggle_checklist_item", self.toggle_checklist_item, "x"),
            ("flight_status", self.refresh_flight_status, "s"),
            ("refresh", self._reload_all, "r"),
            ("filter", self.focus_filter, "f"),
            ("info", self.show_info, "i"),
            ("preferences", self.show_preferences, "l"),
            ("quit", self.close, "q"),
        ]
        for key, handler, shortcut in action_specs:
            action = QAction(t(self.language, key), self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(handler)
            self.addAction(action)
            toolbar.addAction(action)
            self._actions[key] = action

    def _build_menu(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu(t(self.language, "app_title"))
        for key in ("new", "edit", "duplicate", "delete", "refresh", "quit"):
            file_menu.addAction(self._actions[key])
        tools_menu = menu.addMenu(t(self.language, "preferences"))
        for key in ("toggle_checklist_item", "flight_status", "filter", "preferences"):
            tools_menu.addAction(self._actions[key])
        help_menu = menu.addMenu(t(self.language, "info"))
        help_menu.addAction(self._actions["info"])

    def _add_table_tab(self, key: str, title: str) -> None:
        column_count = 4 if key == "timing" else 2
        table = QTableWidget(0, column_count)
        if key == "timing":
            table.setHorizontalHeaderLabels([t(self.language, "step_name_label"), t(self.language, "step_minutes_label"), t(self.language, "calculated_time_label"), t(self.language, "step_cost_label")])
        else:
            table.setHorizontalHeaderLabels([t(self.language, "field_column"), t(self.language, "value_column")])
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setWordWrap(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tabs[key] = table
        container = QWidget()
        self._tab_containers[key] = container
        layout = QVBoxLayout(container)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: 700; color: #8a4f20;")
        self._tab_titles[key] = title_label
        layout.addWidget(title_label)
        layout.addWidget(table, 1)
        self.tabs.addTab(container, t(self.language, key))
        if key == "checklist":
            table.itemDoubleClicked.connect(self._on_checklist_item_activated)

    def _build_flight_provider(self):
        provider_name = os.environ.get(
            "FLIGHT_VALIDATION_PROVIDER",
            self.settings.flight_provider or DEFAULT_FLIGHT_PROVIDER,
        ).strip().lower()
        if provider_name in ("", "opensky"):
            return OpenSkyFlightProvider()
        return PlaceholderScheduleValidationProvider()

    def _log(self, message: str) -> None:
        self._status_log.append(message)
        self.log_view.setPlainText("\n".join(self._status_log[-300:]))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _set_status(self, message: str) -> None:
        self.statusBar().showMessage(message)
        self._log(message)

    def _reload_all(self) -> None:
        self._reload_trip_list()
        trip = self._current_trip()
        if trip is not None:
            self._show_trip(trip)
        else:
            self._clear_detail_tables(t(self.language, "select_trip_first"))

    def _reload_trip_list(self) -> None:
        query = self.filter_edit.text().strip() or None
        current_id = self._selected_trip_id
        trips = self.store.list_trips(query)
        self.trip_list.blockSignals(True)
        self.trip_list.clear()
        selected_item: QListWidgetItem | None = None
        today = datetime.now().date()
        upcoming: list[Trip] = []
        past: list[Trip] = []
        unscheduled: list[Trip] = []
        for trip in trips:
            departure_at = departure_datetime_value(trip)
            if departure_at is None:
                unscheduled.append(trip)
            elif departure_at.date() >= today:
                upcoming.append(trip)
            else:
                past.append(trip)

        for group_key, group_trips in (
            ("upcoming_trips", upcoming),
            ("past_trips", past),
            ("unscheduled_trips", unscheduled),
        ):
            if not group_trips:
                continue
            header = QListWidgetItem(t(self.language, group_key))
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            header.setForeground(QColor("#8a4f20"))
            header_font = header.font()
            header_font.setBold(True)
            header.setFont(header_font)
            self.trip_list.addItem(header)
            for trip in group_trips:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, trip.id)
                card = TripCardWidget(self.language, trip)
                item.setSizeHint(card.sizeHint())
                self.trip_list.addItem(item)
                self.trip_list.setItemWidget(item, card)
                if trip.id == current_id:
                    selected_item = item
        self.trip_list.blockSignals(False)
        if trips:
            if selected_item is not None:
                self.trip_list.setCurrentItem(selected_item)
                self._selected_trip_id = selected_item.data(Qt.ItemDataRole.UserRole)
            else:
                for row in range(self.trip_list.count()):
                    item = self.trip_list.item(row)
                    trip_id = item.data(Qt.ItemDataRole.UserRole)
                    if trip_id is not None:
                        self.trip_list.setCurrentItem(item)
                        self._selected_trip_id = trip_id
                        break
            self._show_trip(self._current_trip())
            self.statusBar().showMessage(t(self.language, "trips_loaded", count=len(trips)))
        else:
            self._selected_trip_id = None
            self._clear_detail_tables(t(self.language, "no_trips_found"))
            self.statusBar().showMessage(t(self.language, "no_trips_found"))

    def _update_hint(self) -> None:
        self.left_hint.setText(
            " | ".join(
                [
                    "n " + t(self.language, "new"),
                    "e " + t(self.language, "edit"),
                    "c " + t(self.language, "copy"),
                    "d " + t(self.language, "delete"),
                    "x " + t(self.language, "toggle_checklist_item"),
                    "s " + t(self.language, "flight_status"),
                    "i " + t(self.language, "info"),
                ]
            )
        )

    def _retranslate_ui(self) -> None:
        self.setWindowTitle(t(self.language, "app_title"))
        self.left_title.setText(t(self.language, "app_title"))
        self.right_title.setText(t(self.language, "trip_details"))
        self.filter_edit.setPlaceholderText(t(self.language, "filter_placeholder"))
        self.new_button.setText(t(self.language, "new"))
        self.edit_button.setText(t(self.language, "edit"))
        self.duplicate_button.setText(t(self.language, "duplicate"))
        self.status_button.setText(t(self.language, "flight_status"))
        for key, action in self._actions.items():
            action.setText(t(self.language, key))
        self.menuBar().clear()
        self._build_menu()
        tab_keys = [
            ("overview", "overview_panel_title"),
            ("ticket", "ticket_panel_title"),
            ("docs", "docs_panel_title"),
            ("timing", "timing_panel_title"),
            ("packing", "packing_panel_title"),
            ("flight", "flight_panel_title"),
            ("checklist", "checklist_panel_title"),
            ("notes", "notes_panel_title"),
            ("summary", "summary_panel_title"),
        ]
        for index, (tab_key, title_key) in enumerate(tab_keys):
            self.tabs.setTabText(index, t(self.language, tab_key))
            self._tab_titles[tab_key].setText(t(self.language, title_key))
            if tab_key == "timing":
                self._tabs[tab_key].setHorizontalHeaderLabels([t(self.language, "step_name_label"), t(self.language, "step_minutes_label"), t(self.language, "calculated_time_label"), t(self.language, "step_cost_label")])
            else:
                self._tabs[tab_key].setHorizontalHeaderLabels([t(self.language, "field_column"), t(self.language, "value_column")])
        self.tabs.setTabText(len(tab_keys), t(self.language, "log"))
        self._update_hint()
        self._reload_all()

    def _current_trip(self) -> Trip | None:
        if self._selected_trip_id is None:
            return None
        return self.store.get_trip(self._selected_trip_id)

    def _on_trip_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self._selected_trip_id = None
            self._clear_detail_tables(t(self.language, "select_trip_first"))
            return
        trip_id = current.data(Qt.ItemDataRole.UserRole)
        if trip_id is None:
            return
        self._selected_trip_id = trip_id
        self._show_trip(self._current_trip())

    def _clear_detail_tables(self, message: str) -> None:
        for key, table in self._tabs.items():
            table.setRowCount(1)
            table.setItem(0, 0, make_item(""))
            table.setItem(0, 1, make_item(message))
            if key == "timing":
                table.setItem(0, 2, make_item(""))
                table.setItem(0, 3, make_item(""))
            table.resizeRowsToContents()

    def _fill_table(self, key: str, rows: list[tuple[QTableWidgetItem, ...]]) -> None:
        table = self._tabs[key]
        table.setRowCount(len(rows))
        column_count = table.columnCount()
        for row_idx, row in enumerate(rows):
            for column in range(column_count):
                value = row[column] if column < len(row) else make_item("")
                table.setItem(row_idx, column, value)
        table.resizeRowsToContents()
        table.resizeColumnToContents(0)

    def _toggle_checklist_item_at_row(self, row: int) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            return
        checklist_tab_index = self.tabs.indexOf(self._tab_containers["checklist"])
        if self.tabs.currentIndex() != checklist_tab_index:
            self._set_status(t(self.language, "open_checklist_tab_first"))
            return
        if not trip.checklist_items:
            self._set_status(t(self.language, "no_checklist_items"))
            return
        if row < 0 or row >= len(trip.checklist_items):
            self._set_status(t(self.language, "select_checklist_item_first"))
            return
        item = trip.checklist_items[row]
        item.done = not item.done
        trip.updated_at = now_iso()
        self.store.save_trip(trip)
        self._show_trip(trip)
        self._reload_trip_list()
        self._tabs["checklist"].selectRow(row)
        state_label = t(self.language, "done_label") if item.done else t(self.language, "pending_label")
        self._log(t(self.language, "checklist_item_toggled_log", name=item.name, state=state_label))
        self._set_status(t(self.language, "checklist_item_toggled_status", name=item.name, state=state_label))

    def _on_checklist_item_activated(self, item: QTableWidgetItem) -> None:
        self._toggle_checklist_item_at_row(item.row())

    def _show_trip(self, trip: Trip | None) -> None:
        if trip is None:
            self._clear_detail_tables(t(self.language, "select_trip_first"))
            return
        completed_steps, total_checklist_items = checklist_progress(trip)
        safe_leave_home, timing_total_minutes = estimated_safe_leave_home(trip)
        estimated_home_arrive, arrival_timing_total_minutes = estimated_home_arrival(trip)
        departure_schedule = departure_timing_schedule(trip)
        arrival_schedule = arrival_timing_schedule(trip)
        trip_total_cost = total_trip_cost(trip)

        self._fill_table(
            "overview",
            [
                (make_item(t(self.language, "title_label")), make_item(value_or_none(trip.title))),
                (make_item(t(self.language, "route_label")), make_item(f"{value_or_none(trip.departure_airport)} -> {value_or_none(trip.arrival_airport)}")),
                (make_item(t(self.language, "departure_label")), make_item(value_or_none(trip.departure_datetime))),
                (make_item(t(self.language, "flight_arrival_time_label")), make_item(value_or_none(trip.flight_arrival_time))),
                (make_item(t(self.language, "checklist_progress_label")), make_item(f"{completed_steps}/{total_checklist_items} completed")),
                (make_item(t(self.language, "created_at_label")), make_item(value_or_none(trip.created_at))),
                (make_item(t(self.language, "updated_at_label")), make_item(value_or_none(trip.updated_at))),
            ],
        )
        self._fill_table(
            "ticket",
            [
                (make_item(t(self.language, "passenger_name")), make_item(value_or_none(trip.passenger_name))),
                (make_item(t(self.language, "checkin_done_label")), status_item(t(self.language, "yes_label") if trip.checkin_done else t(self.language, "no_label"), trip.checkin_done)),
                (make_item(t(self.language, "airline_code")), make_item(value_or_none(trip.airline_code))),
                (make_item(t(self.language, "flight_number")), make_item(value_or_none(trip.flight_number))),
                (make_item(t(self.language, "airline")), make_item(value_or_none(trip.airline))),
                (make_item(t(self.language, "booking_reference")), make_item(value_or_none(trip.booking_reference))),
                (make_item(t(self.language, "ticket_number")), make_item(value_or_none(trip.ticket_number))),
                (make_item(t(self.language, "ticket_cost")), make_item(value_or_none(trip.ticket_cost), align_right=True, color=QColor("#0f766e"), bold=True)),
                (make_item(t(self.language, "seat")), make_item(value_or_none(trip.seat))),
                (make_item(t(self.language, "cabin_class")), make_item(value_or_none(trip.cabin_class))),
                (make_item(t(self.language, "notes_label")), make_item(value_or_none(trip.ticket_notes))),
            ],
        )
        self._fill_table(
            "docs",
            [
                (make_item(t(self.language, "required_documentation_label")), make_item(lines_or_none(trip.documentation_required))),
                (make_item(t(self.language, "documents_to_carry")), make_item(lines_or_none(trip.documents_to_carry))),
            ],
        )
        self._fill_table(
            "timing",
            [
                (make_item(t(self.language, "departure_timing_section")), make_item(""), make_item(""), make_item("")),
                (make_item(t(self.language, "departure_label")), make_item(value_or_none(trip.departure_datetime)), make_item(""), make_item("")),
                *[
                    (
                        make_item(step.name),
                        make_item(format_minutes(step.minutes), align_right=True, color=QColor("#0f766e")),
                        make_item(value_or_none(scheduled_at)),
                        make_item(value_or_none(step.cost), align_right=True, color=QColor("#0f766e")),
                    )
                    for step, scheduled_at in departure_schedule
                ],
                (make_item(t(self.language, "total_timing_buffer_label")), make_item(format_minutes(timing_total_minutes), align_right=True, color=QColor("#0f766e"), bold=True) if timing_total_minutes is not None else severity_item("(none)"), make_item(""), make_item("")),
                (make_item(t(self.language, "estimated_safe_leave_home")), status_item(safe_leave_home, True) if safe_leave_home else severity_item(t(self.language, "timing_estimate_unavailable")), make_item(""), make_item("")),
                (make_item(t(self.language, "arrival_timing_section")), make_item(""), make_item(""), make_item("")),
                (make_item(t(self.language, "flight_arrival_time_label")), make_item(value_or_none(trip.flight_arrival_time)), make_item(""), make_item("")),
                *[
                    (
                        make_item(step.name),
                        make_item(format_minutes(step.minutes), align_right=True, color=QColor("#0f766e")),
                        make_item(value_or_none(scheduled_at)),
                        make_item(value_or_none(step.cost), align_right=True, color=QColor("#0f766e")),
                    )
                    for step, scheduled_at in arrival_schedule
                ],
                (make_item(t(self.language, "arrival_total_timing_label")), make_item(format_minutes(arrival_timing_total_minutes), align_right=True, color=QColor("#0f766e"), bold=True) if arrival_timing_total_minutes is not None else severity_item("(none)"), make_item(""), make_item("")),
                (make_item(t(self.language, "estimated_home_arrival")), status_item(estimated_home_arrive, True) if estimated_home_arrive else severity_item(t(self.language, "arrival_timing_estimate_unavailable")), make_item(""), make_item("")),
            ],
        )
        self._fill_table(
            "packing",
            [
                (make_item(t(self.language, "clothes_label")), make_item(lines_or_none(trip.clothes_items))),
                (make_item(t(self.language, "electronics_label")), make_item(lines_or_none(trip.electronics_items))),
                (make_item(t(self.language, "health_label")), make_item(lines_or_none(trip.health_items))),
                (make_item(t(self.language, "other_items_label")), make_item(lines_or_none(trip.other_items))),
            ],
        )
        flight_rows = self._flight_rows(trip)
        self._fill_table("flight", flight_rows)
        self._fill_table(
            "checklist",
            [(make_item(item.name), status_item(t(self.language, "done_label") if item.done else t(self.language, "pending_label"), item.done)) for item in trip.checklist_items]
            or [(make_item(t(self.language, "checklist_items_label")), make_item("(none)"))],
        )
        self._fill_table("notes", [(make_item(t(self.language, "notes_label")), make_item(trip.general_notes.strip() if trip.general_notes.strip() else "(none)"))])
        summary_link_rows = [
            (
                make_item(f"  {item.name.strip() or t(self.language, 'unnamed_link_label')}"),
                make_item(item.url.strip() or "(none)"),
            )
            for item in trip.links_of_interest
        ] or [(make_item(f"  {t(self.language, 'links_of_interest_label')}"), make_item("(none)"))]

        self._fill_table(
            "summary",
            [
                (make_item(t(self.language, "trip_label")), make_item(value_or_none(trip.title))),
                (make_item(t(self.language, "passenger_label")), make_item(value_or_none(trip.passenger_name))),
                (make_item(t(self.language, "route_label")), make_item(f"{value_or_none(trip.departure_airport)} -> {value_or_none(trip.arrival_airport)}")),
                (make_item(t(self.language, "departure_label")), make_item(value_or_none(trip.departure_datetime))),
                (make_item(t(self.language, "flight_arrival_time_label")), make_item(value_or_none(trip.flight_arrival_time))),
                (make_item(t(self.language, "flight_label")), make_item(value_or_none(f"{trip.airline_code}{trip.flight_number}".strip()))),
                (make_item(t(self.language, "airline")), make_item(value_or_none(trip.airline))),
                (make_item(t(self.language, "booking_reference")), make_item(value_or_none(trip.booking_reference))),
                (make_item(t(self.language, "ticket_cost")), make_item(value_or_none(trip.ticket_cost), align_right=True, color=QColor("#0f766e"))),
                (make_item(t(self.language, "total_cost_label")), make_item(format_cost(trip_total_cost), align_right=True, color=QColor("#0f766e"), bold=True) if trip_total_cost is not None else make_item("(none)")),
                (make_item(t(self.language, "checkin_label")), status_item(t(self.language, "done_label") if trip.checkin_done else t(self.language, "pending_label"), trip.checkin_done)),
                (make_item(t(self.language, "checklist_progress_label")), make_item(f"{completed_steps}/{total_checklist_items} completed")),
                (make_item(t(self.language, "required_documentation_label")), make_item(lines_or_none(trip.documentation_required))),
                (make_item(t(self.language, "packing_snapshot_label")), make_item(f"{t(self.language, 'clothes_label')} {len(trip.clothes_items)} | {t(self.language, 'electronics_label')} {len(trip.electronics_items)} | {t(self.language, 'health_label')} {len(trip.health_items)} | {t(self.language, 'other_items_label')} {len(trip.other_items)}")),
                (make_item(t(self.language, "links_of_interest_label"), bold=True), make_item("")),
                *summary_link_rows,
            ],
        )
        if self._status_log:
            self.log_view.setPlainText("\n".join(self._status_log[-300:]))

    def _flight_rows(self, trip: Trip) -> list[tuple[QTableWidgetItem, QTableWidgetItem]]:
        flight_id = f"{trip.airline_code}{trip.flight_number}".strip()
        if trip.id is None:
            lines = [t(self.language, "save_trip_before_status")]
        else:
            cached = self._flight_status_cache.get(trip.id)
            if cached is None:
                lines = [
                    f"{t(self.language, 'provider_label')}: {self.flight_provider.provider_name}",
                    f"{t(self.language, 'lookup_key_label')}: {flight_id or '(missing airline code / flight number)'}",
                    t(self.language, "press_status_hint"),
                    t(self.language, "choose_provider_hint"),
                ]
            else:
                lines = [
                    f"{t(self.language, 'provider_label')}: {self.flight_provider.provider_name}",
                    f"{t(self.language, 'fetched_at_label')}: {cached.fetched_at}",
                    f"{t(self.language, 'result_label')}: {cached.summary}",
                    *cached.lines,
                ]
        return [(make_item(str(index)), severity_item(line)) for index, line in enumerate(lines, start=1)]

    def _refresh_current_trip(self, select_trip_id: int | None = None) -> None:
        self._reload_trip_list()
        if select_trip_id is None:
            return
        for row in range(self.trip_list.count()):
            item = self.trip_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == select_trip_id:
                self.trip_list.setCurrentRow(row)
                self._selected_trip_id = select_trip_id
                self._show_trip(self.store.get_trip(select_trip_id))
                break

    def focus_filter(self) -> None:
        self.filter_edit.setFocus()
        self.filter_edit.selectAll()

    def show_info(self) -> None:
        QMessageBox.information(self, t(self.language, "about_title"), t(self.language, "about_body"))

    def create_trip(self) -> None:
        dialog = TripEditorDialog(self.language, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        trip_id = self.store.save_trip(dialog.trip)
        self._selected_trip_id = trip_id
        self._refresh_current_trip(trip_id)
        self._set_status(t(self.language, "trip_saved"))

    def edit_trip(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            return
        dialog = TripEditorDialog(self.language, trip=trip, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        trip_id = self.store.save_trip(dialog.trip)
        self._refresh_current_trip(trip_id)
        self._set_status(t(self.language, "trip_saved"))

    def duplicate_trip(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            return
        new_trip = trip.duplicate()
        trip_id = self.store.save_trip(new_trip)
        self._refresh_current_trip(trip_id)
        self._log(t(self.language, "trip_duplicated_log", source=trip.title or str(trip.id), target=new_trip.title))
        self._set_status(t(self.language, "trip_duplicated"))

    def delete_trip(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            return
        answer = QMessageBox.question(
            self,
            t(self.language, "confirm_delete_title"),
            t(self.language, "confirm_delete_message", title=trip.title or f"#{trip.id}"),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        assert trip.id is not None
        self.store.delete_trip(trip.id)
        self._reload_all()
        self._set_status(t(self.language, "trip_deleted"))

    def toggle_checklist_item(self) -> None:
        checklist_tab_index = self.tabs.indexOf(self._tab_containers["checklist"])
        if self.tabs.currentIndex() != checklist_tab_index:
            self._set_status(t(self.language, "open_checklist_tab_first"))
            return
        self._toggle_checklist_item_at_row(self._tabs["checklist"].currentRow())

    def refresh_flight_status(self) -> None:
        trip = self._current_trip()
        if trip is None:
            self._set_status(t(self.language, "select_trip_first"))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self.flight_provider.fetch(trip)
        except Exception as exc:
            result = FlightStatusResult(False, str(exc), [str(exc)])
        finally:
            QApplication.restoreOverrideCursor()
        if trip.id is not None:
            self._flight_status_cache[trip.id] = result
        self._show_trip(trip)
        self._log(f"Status lookup via {self.flight_provider.provider_name} for {trip.title or trip.id}: {result.summary}")
        for line in result.lines:
            self._log(line)
        self._set_status(f"{t(self.language, 'flight_status')}: {result.summary}")

    def show_preferences(self) -> None:
        dialog = PreferencesDialog(self.language, self.settings, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.language = dialog.selected_language()
        self.settings.language = self.language
        self.settings.flight_provider = dialog.selected_provider()
        self.settings.save()
        self.flight_provider = self._build_flight_provider()
        QMessageBox.information(
            self,
            t(self.language, "preferences_title"),
            t(self.language, "preferences_saved"),
        )
        self._retranslate_ui()


def main() -> int:
    qt_app = QApplication(sys.argv)
    window = TripAdminGui()
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

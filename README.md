# Air Trip Admin

Airplane trip management in Python with:
- a Textual TUI
- a PySide6 desktop GUI

## Run The TUI

```bash
python app.py
```

## Run The GUI

```bash
python gui_app.py
```

## Features

- Left-side list of trips
- Right-side tabbed trip details
- Create, edit, delete, and search trips
- Duplicate trips
- Select app language: English, Spanish, German
- Query live flight status from the internet
- Store ticket details
- Track required documentation
- Track checklist items dynamically
- Record departure and arrival timing buffers and estimate safe leave-home and home-arrival times
- Maintain packing lists for clothes, electronics, health items, documents, and other items
- Store a dynamic list of useful links of interest for each trip
- Show a grouped trip summary with sections for overview, ticket, docs, timing, packing, checklist, and links

## Shortcuts

- `n`: new trip
- `e`: edit selected trip
- `c`: duplicate selected trip
- `d`: delete selected trip
- `x` or `Enter`: toggle checklist item when the checklist tab is active
- `s`: refresh live flight status
- `r`: refresh list
- `f`: focus the filter box
- `l`: change language
- `i`: show app info
- `q`: quit

## Storage

Trip data is stored in `trips.db` next to `app.py`.

## Date format

Use `YYYY-MM-DD HH:MM`

## Dependencies

Install the app dependencies:

```bash
pip install -r requirements.txt
```

## Live Flight Status

The `Flight` tab can query live status using:

- airline code such as `IB`, `LH`, `AA`
- flight number such as `625`
- departure date from the trip departure timestamp

Select a provider and set its credentials before running the app:

```bash
set FLIGHT_VALIDATION_PROVIDER=opensky
set OPENSKY_CLIENT_ID=your_client_id
set OPENSKY_CLIENT_SECRET=your_client_secret
python app.py
```

Current limitation: the OpenSky free REST API is best for current live aircraft state by callsign. It is not as rich as commercial schedule/status APIs for historical or gate-level airline status.

## Summary View

The TUI summary tab is organized into grouped sections:

- Overview
- Ticket details
- Documentation
- Timing
- Packing
- Checklist
- Links of interest

The links section is interactive. In the TUI, double-click a link row to open it in the system default browser.

## Editing Notes

- Timing steps are editable as dynamic lists.
- Checklist items are editable as dynamic lists.
- Links of interest are editable as a dynamic list of name + URL pairs.
- Ticket notes and general notes support multi-line editing.

## Provider Architecture

The app now uses a pluggable flight validation provider layer.

- `FLIGHT_VALIDATION_PROVIDER=opensky`: current free live-state implementation
- `FLIGHT_VALIDATION_PROVIDER=placeholder`: stub provider for wiring a paid schedule-validation API later

To add a commercial validator later, implement a new provider class and return it from the provider factory in [app.py](C:/Varios/IA/TUI/VIajes/app.py).

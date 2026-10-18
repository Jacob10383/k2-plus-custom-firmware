# Box client API

`printer.box` (the Moonraker status object for the CFS boxes) and the Box
commands clients send. This file is the contract for every client that uses
them. It ships with the firmware as `docs/box-status-api.md`, next to the
`extras/box.py` that implements it.

## Versioning

| Field | Changes when |
|---|---|
| `api_version` | A change clients must handle: a field removed or its meaning changed while a supported client still reads it. Currently `1`. |
| `api_revision` | Any additive change. Clients check `api_revision >= N` before using what revision `N` added. |

Both cover the whole object, print mapping included.

| Revision | Changes |
|---|---|
| 1 | The original schema. `api_revision` is absent; treat a missing value as `1`. Print mapping (`print_info`, `print_mapping`, `BOX_PRINT_INFO`, `BOX_PRINT_START`, `BOX_SELECT_SLOT`) arrived partway through and is present when `print_mapping_version == 1`. |
| 2 | Print mapping is always present. Adds `api_revision`, `boxes[]`, `load_path.buffer.state`. Top-level `status`/`state`, `temp_c`/`humidity_pct`, `load_path.buffer.state_code`, and `print_mapping_version` become legacy. Removes `fluidd_widget_version`, `loaded_mask`, `slot_filament_mask`, `tracking_active`, `load_path.loaded_slot`, `load_path.loaded_mask`, `load_path.slot_filament_mask`, `load_path.buffer.status_code`, `load_path.buffer.active`. |
| 3 | Adds `BOX_PRINT_MAP`. |
| 4 | `BOX_PRINT_MAP` with an empty `MAP=` clears a stored map. |

## Clients

| Client | Ships from | Checks | Reads | Notes |
|---|---|---|---|---|
| Fluidd fork | `Jacob10383/fluidd` releases | `api_version == 1`; print mapping when `api_revision >= 2` or `print_mapping_version == 1` | `slots[]`, `boxes[]`, `load_path`, `print_*`, `materials`, settings flags | v1.37.7 and later use revision 2. v1.37.6 and earlier read the legacy status fields and gate the print dialog on `print_mapping_version`. v1.37.1 and earlier required the removed `fluidd_widget_version` and hide the card. |
| HelixScreen | upstream releases | `api_version == 1` (v0.99.108, 2026-08-09, and later) | `slots[]`, `loaded_slot`, `load_path`, top-level `temp_c`/`humidity_pct` | Attributes the top-level reading to `load_path.box_addr`, else the lowest box. Does not read `boxes[]` yet. v0.99.107 and earlier required `fluidd_widget_version` and lose load/unload controls. |
| OrcaSlicer fork | `Jacob10383/OrcaSlicer` | `api_version == 1` (filament sync); print mapping when `api_version == 1` and `api_revision >= 2`, or `print_mapping_version == 1` | `slots[]`, `data_ready`, `driver_ready`, `print_info`, `print_mapping` | Sends `BOX_PRINT_INFO` and `BOX_PRINT_START MAP=`. Earlier builds check only `print_mapping_version`. |
| `cfs_bridge.py` | Firmware root filesystem (`/usr/bin`) | none | `slots[]` | Updated with the OS image, not with the Klipper extras. |

## Slots and boxes

Slot numbers are global: `(address - 1) * 4 + local`. A missing box leaves a
gap. The external spool is the last physical slot + 1, so it moves when boxes
are added.

`slots[]` is the flat list of these numbers; `SLOT=` and the slot side of
`MAP=` use them. `T<n>` loads slot `n`, except during a mapped print (see
[Print mapping](#print-mapping)). `boxes[]` has one entry per discovered CFS.
Join on `address`, never array position:

| Field | Meaning |
|---|---|
| `address` | RS-485 address 1-4; `Box N` in labels |
| `online` | `false` while the box is not answering |
| `slots` | Global slot numbers of this box |
| `temp_c`, `humidity_pct` | Latest reading; `null` when offline |
| `status`, `status_code` | Last reply status; `NO_RESPONSE`/`null` when offline |
| `state`, `state_code` | Box state (`IDLE`, `PRINT`, ...) |
| `fault` | `null`, or `{category, status, status_code, title, message}` |

`boxes` is `[]` until the first poll. `fault.category` is `fatal` (the print
pauses), `runout`, or `advisory` (the print continues). `title` is a short
headline and `message` the explanation; both are always set. The console shows
`message` with `status`, e.g. `Box 1 fault (OVERTRAVEL): The hub fed 3 m, ...`.

Slots of an offline box report `present: false`.

## Load path

| Field | Meaning |
|---|---|
| `source_slot` | Loaded slot, or `null` |
| `box_addr` | Box feeding the path; `null` when nothing or the external spool is loaded |
| `tracking_active` | The feeding box is tracking the print |
| `buffer.state` | Shared buffer: `middle`, `full`, `empty`, `fault` (both sensors), or `null` |

The buffer is one shared device, so `buffer.state` is reported even when
nothing is loaded.

`data_ready` is `false` before the first poll and while the box feeding the
path is not answering.

## Print mapping

A map sends each tool the file uses to a slot: `MAP=<tool>:<slot>,...`, for
example `MAP=0:4,1:2`. It must list every tool the file uses, including tools
left on their own slot, and nothing else. `BOX_PRINT_INFO FILENAME=` puts that
list in `print_info.tools`.

| Command | Effect |
|---|---|
| `BOX_PRINT_START FILENAME= MAP=` | Checks the map against the file and starts it. |
| `BOX_PRINT_MAP MAP=` | Revision 3. Checks the slots and keeps the map for the next print, however it is started. That start checks the map against its file and is refused if they disagree. The next print file loaded within 10 minutes uses it once; after that it is dropped. `BOX_PRINT_START` and power-loss recovery discard it, and from revision 4 so does an empty `MAP=`. |

Starting needs every mapped slot online and, except the external spool, loaded
with filament. During a mapped print `print_mapping.map` holds the map, `T<n>`
loads the slot mapped to tool `n`, and `BOX_SELECT_SLOT SLOT=` loads a slot
directly. The map is cleared when the print ends, errors, or is cancelled.

## Legacy fields

Revision 1 fields that revision 2 replaces. They come from the box feeding
the path, else the lowest responding box, as in revision 1.

| Field | Relied on by |
|---|---|
| `temp_c`, `humidity_pct` | HelixScreen (environment reading); Fluidd v1.37.6 and earlier (header reading) |
| `status`, `status_code`, `state`, `state_code` | Fluidd v1.37.6 and earlier (status pill, mode chip) |
| `load_path.buffer.state_code` | Fluidd v1.37.6 and earlier (buffer label) |
| `print_mapping_version` (always `1`) | OrcaSlicer fork and Fluidd (print mapping on revision 1 Boxes; earlier Orca builds and Fluidd v1.37.6 and earlier always) |

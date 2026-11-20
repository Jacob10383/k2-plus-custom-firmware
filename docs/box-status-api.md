# Box client API

`printer.box` (the Moonraker status object for the CFS boxes) and the Box
commands clients send. This file is the contract for every client that uses
them. It ships with the firmware as `docs/box-status-api.md`, next to the
`extras/box.py` that implements it.

## Versioning

| Field | Changes when |
|---|---|
| `api_version` | A change clients must handle: a field removed or its meaning changed while a supported client still reads it. Currently `1`. |
| `api_revision` | Any additive change. |

Both cover the whole object, print mapping included.

| Revision | Changes |
|---|---|
| 1 | The original schema. `api_revision` is absent; treat a missing value as `1`. Print mapping (`print_info`, `print_mapping`, `BOX_PRINT_INFO`, `BOX_PRINT_START`, `BOX_SELECT_SLOT`) arrived partway through and is present when `print_mapping_version == 1`. |
| 2 | Print mapping is always present. Adds `api_revision`, `boxes[]`, `load_path.buffer.state`. Top-level `status`/`state`, `temp_c`/`humidity_pct`, `load_path.buffer.state_code`, and `print_mapping_version` become legacy. Removes `fluidd_widget_version`, `loaded_mask`, `slot_filament_mask`, `tracking_active`, `load_path.loaded_slot`, `load_path.loaded_mask`, `load_path.slot_filament_mask`, `load_path.buffer.status_code`, `load_path.buffer.active`. |
| 3 | Adds `BOX_PRINT_MAP`. |
| 4 | `BOX_PRINT_MAP` with an empty `MAP=` clears a stored map. |
| 5 | Adds `runout.leftover_mm`. |
| 6 | Adds `operation`, and the `FILAMENT_SNAPPED` and `PATH_CONFLICT` faults. `data_ready` no longer drops while the feeding box misses a reply. |

## Clients

| Client | Ships from | Checks | Reads | Notes |
|---|---|---|---|---|
| Fluidd fork | `Jacob10383/fluidd` releases | `api_version == 1`; print mapping when `api_revision >= 2` or `print_mapping_version == 1` | `slots[]`, `boxes[]` (with `fault`), `loaded_slot`, `load_path`, `runout`, `operation`, `recovery`, `print_*`, `materials`, `data_ready`, `driver_ready`, settings flags | v1.37.8-k2.2 and later read `operation` and `runout.leftover_mm`, without an `api_revision` check; an absent field reads as none. v1.37.7 and later use revision 2. v1.37.6 and earlier read the legacy status fields and gate the print dialog on `print_mapping_version`. v1.37.1 required the removed `fluidd_widget_version` and hides the card; earlier releases do not read `box`. |
| HelixScreen | upstream releases | `api_version == 1` (v0.99.108, 2026-08-09, and later) | `slots[]`, `loaded_slot`, `load_path`, `runout`, `materials`, `filament_detected`, `runout_swap_enabled`, `driver_ready`; `boxes[]` (`address`, `online`, readings) when `api_revision >= 2`, else top-level `temp_c`/`humidity_pct` | `boxes[]` from v1.1.0-beta.10 (2026-10-18); v1.0.x attributes the top-level reading to `load_path.box_addr`, else the lowest box. v0.99.107 and earlier required `fluidd_widget_version` and lose load/unload controls. |
| OrcaSlicer fork | `Jacob10383/OrcaSlicer` | `api_version == 1` (filament sync); print mapping when `api_version == 1` and `api_revision >= 2`, or `print_mapping_version == 1` | `slots[]`, `data_ready`, `driver_ready`, `print_info` | Sends `BOX_PRINT_INFO` and `BOX_PRINT_START MAP=`. Earlier builds check only `print_mapping_version`. |
| `cfs_bridge.py` | Firmware root filesystem (`/usr/bin`) | none | `slots[]` | Updated with the OS image, not with the Klipper extras. |

What the clients reject:

- Orca (filament sync and mapping) and Fluidd's Start button need `data_ready`
  and `driver_ready` both `true`.
- Orca's filament sync needs 1-17 `slots[]` entries with unique `index`, all
  four slots of every listed box, exactly one external entry at the highest
  physical slot + 1 (`0` with no CFS), and `material`, `color`, `brand` and
  `name` as strings. Mapping allows the strings to be missing. A `null` in any
  of them rejects the whole reply in both.
- `print_info` must be filled before the `BOX_PRINT_INFO` script returns, with
  `filename` equal to the `FILENAME` sent; both clients then query
  `box=print_info`. Orca requires `tools[].tool` to be unique integers 0-255.

## Slots and boxes

Slot numbers are global: `(address - 1) * 4 + local`. A missing box leaves a
gap. The external spool is the last physical slot + 1, so it moves when boxes
are added.

`slots[]` is the flat list of these numbers; `SLOT=` and the slot side of
`MAP=` use them. `T<n>` loads slot `n`, except during a mapped print (see
[Print mapping](#print-mapping)). `boxes[]` has one entry per discovered CFS.
Join on `address`, never array position.

Each `slots[]` entry:

| Field | Meaning |
|---|---|
| `index` | Global slot number |
| `present` | Filament at the slot's inlet; always `true` for the external spool |
| `loaded` | This slot feeds the printhead |
| `material`, `color`, `brand`, `name` | Saved profile; strings, `""` when unset |
| `spoolman_id` | Spoolman spool, or `null` |
| `rfid_percent` | Remaining % from the slot's RFID tag, as its CFS reports it; `null` without a tag record and for the external spool |
| `rfid_reserve` | The tag's reserve field, `""` when none |
| `external` | `true` for the external spool |

Each `boxes[]` entry:

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
them joined, e.g. `Box 1: Filament didn't arrive. The hub fed ...`.
`fault.status` is a CFS status name, or one with `status_code: null`:
`FILAMENT_SNAPPED` when the loaded slot's filament broke between the slot and
the hub and has to be pulled out by hand; `PATH_CONFLICT`, on each box
involved, when filament sits at more than one hub lane (a box already
reporting `FILAMENT_SNAPPED` keeps that). Until a `PATH_CONFLICT` clears,
`loaded_slot` is `-1`, each lit lane's slot reads `loaded`, and loads and
unloads are refused.

Slots of an offline box report `present: false`.

## Top level

| Field | Meaning |
|---|---|
| `loaded_slot` | Slot feeding the printhead (the external spool's number when it is loaded), `-1` for none, `null` before the first poll |
| `data_ready` | See below |
| `driver_ready` | The CFS boxes on the bus have been found |
| `materials` | `{MATERIAL: {target_temp}}`, the material catalog |
| `filament_detected`, `filament_sensor_error` | Printhead filament sensor: `true`/`false`, or `null` with the error when it cannot be read |
| `runout_swap_enabled`, `unload_after_print_enabled`, `rfid_insert_reading_enabled`, `rfid_startup_reading_enabled` | Settings, changed with `_BOX_SET_RUNOUT_SWAP`, `_BOX_SET_UNLOAD_AFTER_PRINT`, `_BOX_SET_RFID_INSERT_READING` and `_BOX_SET_RFID_STARTUP_READING` `ENABLE=0\|1` |

## Load path

| Field | Meaning |
|---|---|
| `source_slot` | Loaded slot, or `null` |
| `box_addr` | Box feeding the path; `null` when nothing or the external spool is loaded |
| `tracking_active` | The feeding box is tracking the print |
| `buffer.state` | Shared buffer: `middle`, `full`, `empty`, `fault` (both sensors), or `null` |
| `encoder` | `{position_mm, active}`: the feeding box's filament odometer while it tracks the print |
| `printhead_sensor` | `{detected, error}`, as `filament_detected` and `filament_sensor_error` |
| `clog_detection` | `state` (`active`, `inactive` while nothing is tracked, `triggered`, `disabled`) and `triggered`; `null` before the first status poll and after Klipper disconnects |

The buffer is one shared device, so `buffer.state` is reported even when
nothing is loaded.

`data_ready` is `false` before the first poll, after Klipper disconnects, and
while the Box cannot build its status (the other fields then hold the last
good status).

## Runout

`runout` is `null` unless a CFS slot is loaded or has just run out.

| Field | Meaning |
|---|---|
| `loaded_slot` | The slot the path is fed from |
| `chain` | Slots the print would swap to when it runs out, in order |
| `leftover_mm` | Revision 5. After that slot's spool ran out: roughly how much filament is still in the tube. `null` before a runout and when the amount is not known |

## Operation

Revision 6. `operation` is set while `T<n>`, `BOX_SELECT_SLOT`, `BOX_UNLOAD`,
a runout swap or the Box's `RESUME` preparation runs, and after one of them
stopped a print; otherwise it is `null`. `BOX_LOAD`, `BOX_CUT` and the other
commands run without one.

| Field | Meaning |
|---|---|
| `kind` | `load` (nothing was loaded, or the target already was), `change`, `unload`, `runout` or `resume` |
| `state` | `running`; `waiting` when the print paused for the user; `failed` when it stopped on an error and the print is paused |
| `source`, `target` | Slots it changes from and to; `source` is `null` for a `load` or `resume`, `target` for an `unload` |
| `source_tool`, `target_tool` | File tools of a mapped print, else `null` |
| `elapsed` | Whole seconds since it started; while `waiting` or `failed`, since it stopped |
| `message` | While `waiting` or `failed`: what it needs, the same text RESUME gives. Else `null` |
| `steps[]` | Its steps in order, see below |

Each step has `name`, `state` (`done`, `active` or `pending`), `done_mm` and
`total_mm` (`null` when not measured or not known), `estimated` and
`temperature` (the nozzle target it heats to, else `null`). Names: `heat`,
`cut`, `unload`, `pull` (the user pulls the External filament out), `insert`
(the user inserts it), `load`, `purge`, `temperature` (the print temperature),
`return` (back to the print). A step can appear or drop out while it runs.
`estimated` is `true` while `done_mm` is worked out from the usual pull speed
because the CFS cannot report it (the unload pull back to the spool). Load
progress is the CFS odometer; purge and feed progress follow the commanded
extruder move.
The `operation` stays after a `waiting` or `failed` stop until the print
resumes, is cancelled, or another operation starts.

## Recovery

What `RESUME` needs after a Box pause.

| Field | Meaning |
|---|---|
| `blocked` | `true` while a Box pause is unresolved |
| `automatic` | `RESUME` retries loading `target` itself |
| `target` | Slot it loads, or `null` |
| `step` | The step that stopped, or `null` |
| `reason` | Why the print paused, or `null` |

## Print mapping

A map sends each tool the file uses to a slot: `MAP=<tool>:<slot>,...`, for
example `MAP=0:4,1:2`; clients send it quoted, `MAP="0:4,1:2"`. It must list every tool the file uses, including tools
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

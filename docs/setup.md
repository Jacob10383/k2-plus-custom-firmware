# OrcaSlicer

## Machine G-code

Open **Printer Settings → Machine G-code** and use the following fields.

### Start G-code

```gcode
START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] BED_TEMP=[bed_temperature_initial_layer_single] MATERIAL={filament_type[initial_tool]} CHAMBER_TEMP=[overall_chamber_temperature] MIN_CHAMBER_TEMP=[chamber_minimal_temperature]
T[initial_no_support_extruder]
;LINE_PURGE
```

Remove the semicolon from `;LINE_PURGE` to enable the KAMP purge line.

### Before Layer Change G-code

```gcode
;BEFORE_LAYER_CHANGE
TIMELAPSE_TAKE_FRAME
G92 E0
```

Leave out `TIMELAPSE_TAKE_FRAME` if you never use timelapse. Rendering after a
print can be CPU-intensive on the printer.

### Change Filament G-code

Leave this field empty.

## Filament chamber temperature

The **Target** and **Minimal** values under **Filament Settings → Print chamber
temperature** are passed into `START_PRINT` by the machine start G-code:

```gcode
CHAMBER_TEMP=[overall_chamber_temperature]
MIN_CHAMBER_TEMP=[chamber_minimal_temperature]
```

**Target** selects the chamber behavior:

- `0°C`: disables the chamber heater and automatic exhaust target; printing does not wait.
- `1–40°C`: disables the heater and sets the exhaust fans to cool toward Target; printing does not wait for the chamber to cool.
- Above `40°C`: disables automatic exhaust cooling and heats toward Target.

In heater mode, **Minimal** is the temperature at which printing may begin.
The heater continues toward Target after that point. When the bed target is
hotter than Minimal, the auxiliary fans automatically circulate bed heat
during warmup and stop after the chamber wait.

Leave **Activate temperature control** unchecked. Orca's checkbox would add
its own `M191` before `START_PRINT`.

## CFS synchronization

My [OrcaSlicer fork](https://github.com/Jacob10383/OrcaSlicer) improves CFS
synchronization so slots can resolve to the actual Orca filament preset instead
of collapsing to a generic material profile.

See [CFS and RFID](cfs.md) for the Filament Box widget and CFS controls in
Fluidd.

### Slot numbering

Slots are numbered across CFS units:

- One CFS: slots 1–4 are CFS 1; slot 5 is the external spool.
- Two CFS units: slots 1–4 are CFS 1, slots 5–8 are CFS 2, and slot 9 is the external spool.

Flushing volumes from OrcaSlicer are respected. Set the flush multiplier and
material flush volumes there.

### Preset matching

The three CFS filament fields work together:

| Field | Use |
| --- | --- |
| **Name** | Optional. A preset name containing this value gets 20 points. On my fork, an exact case-insensitive match selects that preset immediately. |
| **Material** | Material family, such as `PLA`, `PETG`, or `ASA`. Only presets with this material are scored, and it selects the generic fallback. |
| **Brand** | Optional. A preset name containing this value gets 10 points. |

For a preset named `Polymaker PLA Pro @K2`, use:

```text
Name: PLA Pro
Material: PLA
Brand: Polymaker
```

That preset scores 30 points. The highest score wins; on my fork, user presets
win ties. If nothing scores, Orca falls back to Generic `<Material>`. To select
a preset directly on my fork, set Name to its full visible preset name.

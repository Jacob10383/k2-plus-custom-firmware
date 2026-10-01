# OrcaSlicer

Mainline OrcaSlicer works with this firmware. My
[OrcaSlicer fork](https://github.com/Jacob10383/OrcaSlicer) adds:

- **Reliable sync with better preset matching.** Sync picks your own tuned
  presets and exact preset names. See [Preset matching](#preset-matching).
- **Slot choices when printing.** Upload and Print lets you pick the slot for
  each filament, filled in from matching filament. See
  [CFS slots and sync](#cfs-slots-and-sync).

## Connecting OrcaSlicer

Open the physical printer dialog from the Wi-Fi icon next to the printer:

| Setting | Value |
| --- | --- |
| **Host Type** | Moonraker (Klipper) |
| **Hostname, IP or URL** | The printer's IP address **only** |

If you enabled **(Experimental) Use printer agents instead of print hosts** in
Preferences, turn it off. It removes the slot choices.

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

## CFS slots and sync

There are two ways to choose which slot a print uses.

### Sync, then assign

1. Press the sync button above the filament list. Filament N is now slot N.
2. Set each object to the filament in the slot you want.
3. Slice and print.

If objects are already on the plate, Orca asks how to sync:

- **Overwriting** does the above.
- **Mapping** changes the filaments your objects already use to match the
  slots you pick, so nothing needs reassigning. Filament N is no longer
  slot N, so choose the slots when printing, as below.

On mainline, sync can be unreliable. It also picks Orca's generic presets over
your own tuned ones.

### Add what you use, then choose at print

1. Add only the filaments the print needs and set each one's preset.
2. Slice.
3. Choose the slot for each filament when you start the print:
    - **My fork:** press **Upload and Print**.
    - **Mainline:** upload only, then start the file from Fluidd.

Several filaments can share one slot.

### Slot numbering

- One CFS: slots 1–4 are CFS 1; slot 5 is the external spool.
- Two CFS units: slots 1–4 are CFS 1, slots 5–8 are CFS 2, and slot 9 is the external spool.
- Three or four units continue the same way: the external spool is slot 13 or 17.

### Flushing

Flushing volumes from OrcaSlicer are respected. Set the flush multiplier and
material flush volumes there.

## Filament chamber temperature

Set **Target** and **Minimal** under **Filament Settings → Print chamber
temperature**. **Target** selects the chamber behavior:

- `0°C`: disables the chamber heater and automatic exhaust target; printing does not wait.
- `1–40°C`: disables the heater and sets the exhaust fans to cool toward Target; printing does not wait for the chamber to cool.
- Above `40°C`: disables automatic exhaust cooling and heats toward Target.

In heater mode, **Minimal** is the temperature at which printing may begin.
The heater continues toward Target after that point. When the bed target is
hotter than Minimal, the auxiliary fans automatically circulate bed heat
during warmup and stop after the chamber wait.

Leave **Activate temperature control** unchecked. Orca's checkbox would add
its own `M191` before `START_PRINT`.

## Preset matching

Each slot's fields, set in Fluidd's [Filament Box](cfs.md) widget, score
Orca's filament presets:

| Field | Use |
| --- | --- |
| **Name** | Optional. A preset name containing this value gets 20 points. |
| **Material** | Material family, such as `PLA`, `PETG`, or `ASA`. Only presets with this material are scored, and it selects the generic fallback. |
| **Brand** | Optional. A preset whose name contains this value, or that comes from this brand's Orca profiles, gets 10 points. |

For a preset named `Polymaker PLA Pro @K2`, use:

```text
Name: PLA Pro
Material: PLA
Brand: Polymaker
```

That preset scores 30 points. Each word in a preset's name that the slot does
not mention costs 5 points, except `Generic` and anything after `@`. A plain
`PLA` slot therefore prefers `Generic PLA` over `Generic PLA Matte`. The
highest score wins. If nothing scores, Orca falls back to Generic `<Material>`.

On my fork:

- Set Name to a preset's full visible name (any letter case) to select it
  directly. For a spool linked to Spoolman, put the preset name in the
  Spoolman filament's name.
- User presets win ties.
- The matched preset is used as is, including your own tuned copy.

On mainline, system presets win ties, and your own copy of a system preset is
never selected. Sync picks the original instead.

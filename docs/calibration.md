# Calibration

The default configuration expects the 5.8 mm Y-endstop spacer and the
Cartographer mount used by K2 Improvements.

## Required after installation

These are the only calibration steps required for the default setup.

### 1. Calibrate Cartographer

```gcode
G28
CARTOGRAPHER_TOUCH_CALIBRATE METHOD=touch
CARTOGRAPHER_SCAN_CALIBRATE
```

The default probe mode is `carto`. If you changed modes, use the matching
instructions under [Probe modes](#probe-modes) instead.

### 2. Calibrate the cutter

```gcode
CALIBRATE_CUT_POS
```

### 3. Calibrate input shaper

```gcode
SHAPER_CALIBRATE
SAVE_CONFIG
```

The analysis step can take a while because the printer's SoC is slow at
processing resonance data.

## Other calibration and maintenance

Nothing below is part of the normal initial setup.

### Probe modes

Check the current mode:

```sh
bootstrap --probe
```

Switch modes:

```sh
bootstrap --probe carto
bootstrap --probe mix
bootstrap --probe prtouch
```

- `carto`: Cartographer only
- `mix`: Cartographer scans the bed; PRTouch establishes nozzle Z=0
- `prtouch`: PRTouch only

Bootstrap updates the probe configuration and restarts Klipper automatically.
Cartographer and mixed modes expect the 5.8 mm Y-endstop spacer. PRTouch-only
mode does not.

#### Mixed-mode calibration

```gcode
G28
PRTOUCH_SCAN_CALIBRATE
```

This uses PRTouch to calibrate the Cartographer scan model. It does not
calibrate PRTouch itself.

#### PRTouch-only mode

No probe calibration is required.

### Motor calibration

The firmware will tell you when a motor is uncalibrated and needs
`MOTOR_CALIBRATE`. It is also worth trying when troubleshooting print or motor
issues.

For X, Y, Z, or Z1:

```gcode
MOTOR_CALIBRATE AXIS=X
```

The first run does not calibrate anything. It disables the motors and asks you
to place the printhead near the middle and the bed at the bottom. Move them
there by hand, then run the same command again.

Extruder calibration is two-stage and must be run with filament unloaded:

```gcode
MOTOR_CALIBRATE AXIS=E STAGE=encoder
# Power cycle the printer.
MOTOR_CALIBRATE AXIS=E STAGE=offset
```

### Belt tension

Automatic tensioning is normal belt maintenance:

```gcode
BELT_TENSION
```

Use `BELT_TENSION AXES=X` or `BELT_TENSION AXES=Y` to tension one axis.

#### Belt tension sensor recalibration

Do not run this as routine setup. It is only for incorrect tension readings or
specific troubleshooting, and requires the [printed calibration jig](https://www.crealitycloud.com/model-detail/belt-tensioning-module-calibration-tool). Normal
automatic belt tensioning does not require it.

Home and park the carriage where the middle of the selected belt is accessible,
then capture the normal and jig loads:

```gcode
BELT_TENSION_CALIBRATE AXIS=X POINT=LOW
# Install the jig.
BELT_TENSION_CALIBRATE AXIS=X POINT=HIGH
# Remove the jig, then home before moving the printer.
```

Repeat for `AXIS=Y`.

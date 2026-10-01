# Calibration

## Probe modes

| Mode | What it does |
| --- | --- |
| Mixed (default) | Cartographer scans the bed, PRTouch does touch operations (Z offset) |
| Cartographer | Cartographer does both |
| PRTouch | PRTouch does both |

The default configuration of Mixed and Cartographer modes expects the K2
Improvements Cartographer mount and the 5.8 mm Y-endstop spacer. Change it if
your setup differs.

Check the current mode:

```sh
bootstrap --probe
```

Switch modes:

```sh
bootstrap --probe mix
bootstrap --probe carto
bootstrap --probe prtouch
```

Bootstrap restarts Klipper after switching.

## Required after installation

### 1. Calibrate the probe

Run only the commands for your probe mode.

=== "Mixed (default)"

    First, home the printer:

    ```gcode
    G28
    ```

    Wait for homing to finish, then run:

    ```gcode
    PRTOUCH_SCAN_CALIBRATE
    ```

=== "Cartographer"

    First, home the printer:

    ```gcode
    G28
    ```

    Wait for homing to finish, then run:

    ```gcode
    CARTOGRAPHER_TOUCH_CALIBRATE
    SAVE_CONFIG RESTART=0
    CARTOGRAPHER_SCAN_CALIBRATE METHOD=touch
    SAVE_CONFIG RESTART=0
    ```

=== "PRTouch"

    No probe calibration is required.

### 2. Calibrate the cutter

```gcode
CALIBRATE_CUT_POS
```

### 3. Calibrate input shaper

```gcode
SHAPER_CALIBRATE
SAVE_CONFIG RESTART=0
```

## Other calibration and maintenance

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

Do **not** run this as routine setup. It **will** redefine what your tensioners
treat as "normal" tension. It is only for incorrect tension readings or
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

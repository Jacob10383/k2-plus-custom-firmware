# Command Reference

Commands added or overridden by this firmware are listed here. Standard
Klipper and Cartographer commands remain in their own documentation.

## Maintenance macros

| Command | Behavior |
| --- | --- |
| `LUBRICATE_RAILS [ITERATIONS=1] [SPEED=500]` | Homes and runs the full rail-lubrication motion pattern. |
| `BELT_TENSION [AXES=XY]` | Automatically tensions X, Y, or both to 140. |
| `BEDPID` | Calibrates the bed PID at 100°C and saves the result. |
| `NOZZLE_PID` | Calibrates the nozzle PID at 230°C and saves the result. |
| `NOZZLE_PID_HIGH` | Calibrates the nozzle PID at 320°C and saves the result. |

## CFS and tool changes

| Command | Behavior |
| --- | --- |
| `T<n> [FLUSH=1]` | Changes to zero-based slot `<n>`. During a print, `<n>` is the file's tool number and changes to the slot chosen for it. `FLUSH=0` skips the purge. |
| `BOX_GO_TO_WASTEBIN` | Parks the printhead at the waste bin. |
| `NOZZLE_CLEAN` | Runs the configured nozzle-cleaning sequence. |
| `BOX_LOAD [SLOT=0]` | Loads a physical zero-based CFS slot without the full tool-change policy. |
| `BOX_UNLOAD [MANUAL=0\|1]` | Fully unloads the active filament. |
| `BOX_CUT [FORCE=0\|1]` | Runs the cutter directly. |
| `BOX_BUFFER_RETRACT` | Runs the CFS buffer retract phase. |
| `BOX_DEBUG [RAW=0\|1]` | Prints complete Box, topology, slot, sensor, and RFID diagnostics. |

## RFID and slot metadata

| Command | Behavior |
| --- | --- |
| `RFID_READER_READ` | Reads the most recent record from the external RFID reader. |
| `_BOX_RFID_MAP_SET CODE= MATERIAL= BRAND= NAME= [TARGET_TEMP=]` | Saves an unknown-tag mapping. |
| `_BOX_RFID_MAP_DELETE CODE=` | Deletes an RFID mapping. |

## Probe commands

| Command | Behavior |
| --- | --- |
| `PRTOUCH_HOME [TRAVEL_SPEED=] [Z_HOP=] [PRINT_TEMP=]` | Runs a multi-sample PRTouch Z home; optional print temperature applies thermal compensation. |
| `PRTOUCH_SCAN_CALIBRATE [MODEL=default]` | Uses PRTouch to establish nozzle Z=0 for the Cartographer scan model. |
| `PRTOUCH_AXIS_TWIST_COMPENSATION [AXIS=X\|Y] [USE_TOUCH_BOUNDARIES=0\|1] [SAMPLE_COUNT=] [START=] [END=] [LINE=]` | Uses Cartographer scans and PRTouch contacts to calibrate axis twist. Run `SAVE_CONFIG`, then regenerate the bed mesh. |
| `PRTOUCH_SCRUB` | Homes if needed, detects the rear tab, and wipes the nozzle across it. |

`PRTOUCH_HOME`, `PRTOUCH_SCAN_CALIBRATE` and `PRTOUCH_AXIS_TWIST_COMPENSATION`
also accept `SAMPLES=`, `MAX_SAMPLES=`, `MAX_NOISY=`, `SAMPLE_RANGE=`,
`PROBE_SPEED=`, `LIFT_SPEED=` and `SAMPLE_RETRACT_DIST=` to override the
`[prtouch]` sampling settings for one run.

## Motor control

| Command | Behavior |
| --- | --- |
| `CALIBRATE_CUT_POS` | Homes XY if needed, measures the cutter hit, and saves `cut_pos_x`. |
| `MOTOR_CALIBRATE AXIS=XYZZ1 [DETAIL=raw]` | Calibrates one or more kinematic motors after a positioning confirmation pass. |
| `MOTOR_CALIBRATE AXIS=E STAGE=encoder\|offset\|1\|2 [DETAIL=raw]` | Runs the selected extruder calibration stage. |
| `MOTOR_STATUS [VERBOSE=1]` | Reports motor startup, fault, calibration, and runtime state. |
| `MOTOR_CLEAR_ERROR` | Queries and attempts to clear active motor faults and warnings. |
| `MOTOR_QUERY_FAULTS` | Queries live fault, warning, and status codes for every motor. |
| `MOTOR_CFG_OVERRIDE_STATUS [AXIS=XYZZ1E] [DETAIL=raw]` | Compares active config overrides with current board values. |
| `MOTOR_RETRY_STARTUP` | Re-runs motor-control startup. |
| `MOTOR_READ_PARAM PARAM=<config-key>` | Reads a motor parameter's runtime and flash values. |
| `MOTOR_FLASH_PARAM PARAM=<config-key> [VALUE=] [COMMIT=1]` | Reads or changes a motor parameter; `COMMIT=1` writes motor-board flash. |

## Belt tension modules

Normal belt maintenance should use `BELT_TENSION`. These commands operate the
individual tension modules directly.

| Command | Behavior |
| --- | --- |
| `BELT_TENSION_STATUS [AXIS=X\|Y]` | Reports live position, ADC, calibration, and tension state. |
| `BELT_TENSION_SET AXIS=X\|Y [TENSION=]` | Settles and adjusts one belt to a target tension. |
| `BELT_TENSION_CALIBRATE AXIS=X\|Y POINT=LOW\|HIGH` | Captures the two strain-gauge calibration points. |
| `BELT_TENSION_MOVE_TO AXIS=X\|Y POSITION=` | Moves a tensioner to a raw stored position. |
| `BELT_TENSION_MOVE_BY AXIS=X\|Y DISTANCE= [UPDATE_POSITION=0\|1]` | Performs a raw relative tensioner move. |
| `BELT_TENSION_ZERO AXIS=X\|Y` | Overwrites the module's stored position with zero. |

## Power-loss recovery

| Command | Behavior |
| --- | --- |
| `PLR_STATUS` | Reports checkpoint and recovery state. |
| `PLR_RECOVER CONFIRM=1` | Validates and resumes the saved print checkpoint. |
| `PLR_DISCARD` | Deletes the saved recovery checkpoint. |

## Hardware and runtime utilities

| Command | Behavior |
| --- | --- |
| `SERIAL_STATUS` | Reports shared RS-485 transport counters and errors. |
| `FAN_FEEDBACK_STATUS` | Reports live fan tachometer feedback. |
| `SET_LED_IDLE_MANAGER [ENABLE=0\|1]` | Enables or disables automatic LED idle control for this session. |
| `LED_IDLE_MANAGER_ON` | Enables LED idle control for this session. |
| `LED_IDLE_MANAGER_OFF` | Disables LED idle control for this session. |
| `LED_IDLE_MANAGER_STATUS` | Reports LED idle-manager state. |
| `EXTENDED_ZONE_TRANSFORM_STATUS` | Reports rear-zone transform and Y-envelope state. |

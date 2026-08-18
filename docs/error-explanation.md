# Error explanation

## CFS

Errors reported by the CFS.

| Error | Meaning | What to check |
| --- | --- | --- |
| `STAGE0_SENSOR_TIMEOUT` | The chosen slot started feeding, but filament never reached the path sensor on the way into the hub (7 s). | If filament is seated in the slot gears and not tangled. |
| `SLOT_EMPTY` | The chosen slot has no filament at the inlet. | |
| `STAGE0_ODOMETER_TIMEOUT` | Load started, but the hub odometer did not move for 2 s. | For a tangle, or whether the hub is turning filament. |
| `FEED_TIMEOUT` | The hub fed for 25 s without filament finishing the path through the buffer to the printhead. | |
| `OVERTRAVEL` | The hub measured 3 m of motion without the load completing. Filament left the hub but did not arrive. | For PTFE disconnections. |
| `ODOMETER_STALLED` | During the feed, the hub odometer stopped changing for 500 ms. | For a jam or resistance in the filament path. |
| `BUFFER_NOT_FULL` | After feeding, the buffer between never showed full. | |
| `UNLOAD_BUFFER_TIMEOUT` | The hub reversed to empty the buffer, and the buffer did not go empty in 5 s. | For filament stuck in the buffer, or the PTFE between buffer and hub. |
| `UNLOAD_HUB_PE_TIMEOUT` | Retract waited for the hub path to clear, and it never did. | For filament stuck in the hub. |
| `UNLOAD_NO_FILAMENT` | The chosen slot is not showing filament present, so there is nothing to unload. | |
| `UNLOAD_INLET_CLEAR` | Retract from the hub started while the slot inlet still showed filament there. | |
| `UNLOAD_MOTOR_BLOCKED` | The hub motor stalled while pulling filament back. | For a jam on retract. |
| `UNLOAD_ODOMETER_TIMEOUT` | The hub pulled back for 40 s without completion. | |
| `RUNOUT` | The spool ran out. | |
| `BUFFER_REFILL_STALLED` | During a print the hub is moving filament, but the buffer stays empty. | The PTFE from the hub into the buffer. |
| `BUFFER_REFILL_NO_MOTION` | During a print the hub tried to refill the buffer, but the odometer moved less than 5 mm. | For a jam or tangle in the filament path. |

## Motor control

Protection bits from the motor boards. Several can be set at once. X/Y/Z/Z1 shut the printer down; the extruder pauses and can be cleared.

| Error | Meaning |
| --- | --- |
| `encoder mutation` | The encoder reading jumped farther than the protect threshold between samples. |
| `encoder read error` | The motor MCU failed to read the encoder. |
| `software peak overcurrent` | Phase current spiked over the peak limit. |
| `software continuous overcurrent` | Phase current stayed over the continuous limit long enough to trip. |
| `speed feedback continuously over limit` | Measured speed stayed above the protect limit. |
| `position feedback over limit` | Encoder position went past the software position limit. |
| `position command mutation` | The motion command jumped farther/faster than the protect rate. |
| `excessive position tracking error` | Commanded position and encoder position stayed too far apart. |
| `motor check value abnormal` | Startup phase check measured a nonsense value. |
| `motor check unstable` | Startup phase check readings would not settle. |
| `motor phase not connected` | Phase check saw an open phase. |
| `motor phase resistance mismatch` | Phase resistances disagree by more than the tolerance. |
| `motor check other error` | Startup phase check failed for some other reason. |
| `motor power supply too low` | Motor-board supply voltage is below the minimum. |
| `MCU overheating` | The motor MCU temperature is above the protect limit. |

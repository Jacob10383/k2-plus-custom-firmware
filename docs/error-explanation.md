# Error explanation

## CFS

Errors reported by the CFS.

<!-- generated: cfs-errors -->
| Error | What it means | Code |
| --- | --- | --- |
| Communication errors | Check the box's cable. | `BAD_CRC` (0x02) |
| Filament didn't reach the hub | Check that it's seated in the slot gears and not tangled. | `STAGE0_SENSOR_TIMEOUT` (0x05) |
| Slot is empty | Insert filament into the slot. | `SLOT_EMPTY` (0x08) |
| Filament isn't moving | Check for a tangle, or that the hub is gripping the filament. | `STAGE0_ODOMETER_TIMEOUT` (0x09) |
| Feed timed out | The filament never reached the printhead. Check for resistance in the PTFE tube. | `FEED_TIMEOUT` (0x0A) |
| Filament didn't arrive | The hub fed more than the full tube length. Check for a disconnected PTFE tube. | `OVERTRAVEL` (0x0B) |
| Filament stopped moving | Check for a jam or resistance between the box and the printhead. | `ODOMETER_STALLED` (0x0C) |
| Buffer didn't fill | Check the PTFE tube into the buffer. | `BUFFER_NOT_FULL` (0x0D) |
| Filament couldn't pull back | Check for a jam between the box and the printhead. | `UNLOAD_BUFFER_TIMEOUT` (0x13) |
| Filament stuck in the hub | The retract never cleared it. Check for a jam inside the box. | `UNLOAD_HUB_PE_TIMEOUT` (0x14) |
| Nothing to unload | The slot has no filament for the box to pull back. | `UNLOAD_NO_FILAMENT` (0x16) |
| Slot inlet already empty | The filament was gone from the slot before the retract started. | `UNLOAD_INLET_CLEAR` (0x17) |
| Hub motor stalled | It stalled while pulling filament back. Check for a jam. | `UNLOAD_MOTOR_BLOCKED` (0x19) |
| Retract timed out | The hub kept pulling back but the path never cleared. Check for a jam. | `UNLOAD_ODOMETER_TIMEOUT` (0x1A) |
| Spool ran out | Put a new spool in the slot. | `RUNOUT` (0x50) |
| Buffer isn't refilling | The hub is feeding but the buffer stays empty. Check the PTFE tube from the hub into the buffer. | `BUFFER_REFILL_STALLED` (0x51) |
| Buffer refill didn't move | The filament barely moved when the hub tried to feed. Check for a jam or tangle. | `BUFFER_REFILL_NO_MOTION` (0x52) |
| Unknown status | The box reported an unknown status. | other |
<!-- end generated: cfs-errors -->

## Motor control

Error bits from the motor boards. Several can be set at once. X/Y/Z/Z1 shut the printer down; the extruder pauses and can be cleared.

| Error | Meaning |
| --- | --- |
| `encoder mutation` | The encoder reading jumped farther than the protect threshold between samples. |
| `encoder read error` | The motor MCU failed to read the encoder. |
| `software peak overcurrent` | Phase current spiked over the peak limit. |
| `software continuous overcurrent` | Phase current stayed over the continuous limit long enough to trip. |
| `hardware overcurrent` | The motor board's hardware current protection tripped. |
| `speed feedback continuously over limit` | Measured speed stayed above the protect limit. |
| `position feedback over limit` | Encoder position went past the software position limit. |
| `position command mutation` | The motion command jumped farther/faster than the protect rate. |
| `excessive position tracking error` | Commanded position and encoder position stayed too far apart. |
| `motor check value abnormal` | Startup phase check measured a nonsense value. |
| `motor check unstable` | Startup phase check readings would not settle. |
| `motor phase not connected` | Phase check saw an open phase. |
| `motor phase resistance mismatch` | Phase resistances disagree by more than the tolerance. |
| `motor check other error` | Startup phase check failed for some other reason. |

Warning bits from the motor boards:

| Warning | Meaning |
| --- | --- |
| `communication error` | The motor board reported a communication error. |
| `motor power supply too low` | Motor-board supply voltage is below the minimum. |
| `MCU overheating` | The motor MCU temperature is above the protect limit. |

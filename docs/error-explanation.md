# Error explanation

## CFS

Errors reported by the CFS.

<!-- generated: cfs-errors -->
| Error | What it means | Code |
| --- | --- | --- |
| Communication errors | The box keeps receiving corrupted messages. Check its cable. | `BAD_CRC` (0x02) |
| Filament didn't reach the hub | The box fed for 7 seconds but the hub never detected filament. Check for a tangled spool or filament caught before the hub. | `STAGE0_SENSOR_TIMEOUT` (0x05) |
| Slot is empty | The slot doesn't detect any filament. | `SLOT_EMPTY` (0x08) |
| Filament isn't clearing the hub | The filament moved less than 20 mm in 2 seconds after reaching the hub. Check for a tangled spool or grinded filament. | `STAGE0_ODOMETER_TIMEOUT` (0x09) |
| Feed timed out | The filament never reached the printhead. Check for resistance in the PTFE tube. | `FEED_TIMEOUT` (0x0A) |
| Filament didn't arrive | The box fed 3 m of filament without the printhead detecting it. Check for a disconnected PTFE tube. | `OVERTRAVEL` (0x0B) |
| Filament stopped moving | The filament stopped moving on its way to the printhead. Check for a tight bend or snag in the PTFE tube where the tip stopped. | `ODOMETER_STALLED` (0x0C) |
| Buffer didn't fill | The filament reached the printhead, but the buffer didn't fill within 3 seconds. Check that the buffer slides freely. | `BUFFER_NOT_FULL` (0x0D) |
| Buffer didn't release | The box pulled back for 5 seconds but the buffer never emptied. Check for a stuck buffer or a jam between it and the box. | `UNLOAD_BUFFER_TIMEOUT` (0x13) |
| Filament stuck in the hub | The box pulled back, but the hub still detects filament. Check for a jam or a broken piece at the hub. | `UNLOAD_HUB_PE_TIMEOUT` (0x14) |
| Nothing to unload | The slot doesn't detect any filament, so it cannot be unloaded. | `UNLOAD_NO_FILAMENT` (0x16) |
| Slot inlet already empty | The slot's sensor no longer detects filament, so the box couldn't pull it back. The filament may have broken between the slot and the hub. | `UNLOAD_INLET_CLEAR` (0x17) |
| Box motor stalled | A box motor stalled for 10 seconds while pulling filament back. Check for a jam. | `UNLOAD_MOTOR_BLOCKED` (0x19) |
| Retract timed out | The box pulled back for 40 seconds but the filament never cleared the hub. | `UNLOAD_ODOMETER_TIMEOUT` (0x1A) |
| Spool ran out | The box no longer detects filament in this slot. The spool is empty or the filament snapped. | `RUNOUT` (0x50) |
| Buffer isn't refilling | The box keeps feeding but the buffer never fills back up. Check for a disconnected PTFE tube. | `BUFFER_REFILL_STALLED` (0x51) |
| Buffer refill didn't move | The box tried to refill the buffer but the filament barely moved. Check for a tangled spool or obstruction between box and buffer. | `BUFFER_REFILL_NO_MOTION` (0x52) |
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

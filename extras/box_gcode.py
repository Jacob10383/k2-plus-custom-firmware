# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Read Orca's tool usage, filament profiles, temperatures, and purge matrix."""

import math
import os
import re


def read_metadata(path):
    limit = 1024 * 1024
    with open(path, "rb") as stream:
        offset = max(0, os.fstat(stream.fileno()).st_size - limit)
        stream.seek(offset)
        footer = stream.read(limit)
    if offset:
        # Ignore a partial line at the beginning of the read.
        footer = footer.partition(b"\n")[2]
    fields = dict(re.findall(
        r"^;[ \t]*(filament used \[mm\]|filament_colour|filament_type|filament_settings_id|"
        r"flush_volumes_matrix|nozzle_temperature|nozzle_temperature_initial_layer)"
        r"[ \t]*=[ \t]*([^\r\n]*)", footer.decode("utf-8", errors="replace"), re.M))

    def numbers(key):
        try:
            values = [float(value) for value in fields.get(key, "").split(",")]
        except ValueError:
            return None
        return values if all(math.isfinite(value) and value >= 0 for value in values) else None

    profiles = {key: [value.strip().strip('"') for value in fields.get(key, "").split(";")]
                for key in ("filament_colour", "filament_type", "filament_settings_id")}

    def profile(key, tool):
        values = profiles[key]
        return values[tool] if tool < len(values) else ""

    lengths = numbers("filament used [mm]") or []
    if len(lengths) > 256:
        lengths = []
    tools = [{"tool": tool,
              "color": profile("filament_colour", tool),
              "material": profile("filament_type", tool),
              "name": profile("filament_settings_id", tool)}
             for tool, length in enumerate(lengths) if length > 0]
    volumes = numbers("flush_volumes_matrix") or []
    size = math.isqrt(len(volumes))
    matrix = ([volumes[i * size:(i + 1) * size] for i in range(size)]
              if size and size * size == len(volumes) else None)
    return {"tools": tools, "matrix": matrix,
            "temp_print": numbers("nozzle_temperature"),
            "temp_initial_layer": numbers("nozzle_temperature_initial_layer")}

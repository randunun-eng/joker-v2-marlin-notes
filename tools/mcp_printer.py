#!/usr/bin/env python3
"""
MCP server for the MagicMaker SLuban (JOKER V2, ESP32).

This is deliberately a THIN client over printerd's HTTP API. It holds no serial
handle of its own -- the serial port is exclusive, and printerd owns it. That
separation is what lets a browser tab, the CLI and an assistant all see the same
printer without fighting over /dev/cu.usbserial-2120.

Start printerd first:
    tools/printerd.py

Register (example, Claude Code):
    claude mcp add joker -- /path/to/tools/mcp_printer.py

Safety posture, on purpose:
  * No raw-G-code tool. Motion and heaters are not scriptable from a prompt.
  * start_print refuses anything outside the 125x125x145 bed (printerd enforces
    it server-side; this cannot be bypassed from here).
  * Default mode is "stream". SD upload is implemented but resets this board
    after ~8-9 lines (see docs/USB-PRINTING.md), so it is not used.
  * abort_print is NOT an emergency stop. EMERGENCY_PARSER is impossible on
    ESP32, so commands queue behind buffered moves. The power switch is the
    only real stop, and every abort response says so.
"""

import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

DAEMON = os.environ.get("PRINTERD_URL", "http://127.0.0.1:8765")
mcp = FastMCP("joker-v2-printer")


def _call(path, payload=None, timeout=20):
    url = f"{DAEMON}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return json.loads(body) if body.strip().startswith(("{", "[")) else body
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode() or f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"cannot reach printerd at {DAEMON} ({e.reason}). "
            "Start it with tools/printerd.py -- it owns the serial port."
        )


@mcp.tool()
def printer_status() -> dict:
    """
    Current printer state: status, phase, percent complete, elapsed, ETA,
    live nozzle/bed temperatures, and the recent event log.
    """
    return _call("/api/status")


@mcp.tool()
def list_gcode() -> list:
    """
    List sliced G-code files the printer can see, each with its size, the 8.3
    SD filename it would be uploaded as, and whether every move fits the
    125x125x145 bed. Files marked fits=false were sliced for another printer
    and cannot be printed.
    """
    return _call("/api/files")


@mcp.tool()
def start_print(path: str, mode: str = "stream") -> dict:
    """
    Start a print.

    mode="stream" (default) streams over USB. The job depends on the cable and
    on the host staying awake for its whole duration.

    mode="sd" would upload to the printer's SD card first, but SD upload is
    BROKEN on this board -- it resets after ~8-9 written lines. Do not use it.
    For a job that must survive a cable or host failure, copy the file to the
    card with a card reader and start it from the LCD.

    Refuses any file whose moves fall outside the bed.

    Args:
        path: absolute path to a .gcode file
        mode: "sd" or "stream"
    """
    if mode not in ("sd", "stream"):
        raise ValueError('mode must be "sd" or "stream"')
    return _call("/api/print", {"path": path, "mode": mode})


@mcp.tool()
def abort_print() -> dict:
    """
    Stop the current job and turn off heaters, fan and steppers.

    NOT an emergency stop. M112 cannot be immediate on this ESP32 board, so
    these commands queue behind already-buffered moves. If the nozzle is
    crashing into the bed, the physical power switch is the only real stop.
    """
    r = _call("/api/abort")
    return {"result": r,
            "warning": "Abort is queued, not instant. Use the power switch if "
                       "the head is crashing."}


if __name__ == "__main__":
    mcp.run()

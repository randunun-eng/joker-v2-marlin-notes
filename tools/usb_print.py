#!/usr/bin/env python3
"""
Stream a G-code file to the MagicMaker SLuban (JOKER V2.0, ESP32) over USB serial.

OrcaSlicer cannot print over USB -- its print-host factory only builds network
hosts (OctoPrint/Duet/ESP3D/Moonraker/...), so USB streaming has to happen
outside the slicer. Orca's job is: slice -> Export G-code. This script does the
rest.

Usage
    ./usb_print.py FILE.gcode                 # stream to the printer
    ./usb_print.py FILE.gcode --dry-run       # parse + validate, no serial
    ./usb_print.py --list-ports

Protocol notes specific to this board
  * BAUDRATE is 250000 (Marlin/Configuration.h).
  * Opening the port asserts DTR/RTS and RESETS the ESP32 (esptool-style
    auto-reset circuit). That is normally what you want at the start of a print
    -- it guarantees a clean state -- so we do it and wait for Marlin's banner.
    Use --no-reset to attach without rebooting (e.g. to watch an SD print).
  * During heating Marlin floods `echo:busy: processing`. That is liveness, NOT
    completion. Treating it as an error is how an earlier debugging session
    concluded a working G28 had "crashed".
  * M112 is NOT immediate on this board: EMERGENCY_PARSER is impossible on
    ESP32 (Marlin HAL limitation), so any abort command queues behind buffered
    moves. THE PHYSICAL POWER SWITCH IS THE ONLY REAL EMERGENCY STOP.

Line numbering + checksums are used so that a corrupted line can be resent
rather than silently mangling the print.
"""

import argparse
import os
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is required:  python3 -m pip install pyserial")

DEFAULT_PORT = "/dev/cu.usbserial-2120"
DEFAULT_BAUD = 250000

# No 'ok' for this long => something is wrong. Marlin emits busy every ~2 s
# while working, and we reset this timer on ANY received byte, so a generous
# value here only catches a truly dead link.
QUIET_TIMEOUT = 90.0


def checksum(line: str) -> int:
    """Marlin's checksum: XOR of every character before the '*'."""
    cs = 0
    for ch in line:
        cs ^= ord(ch)
    return cs & 0xFF


def frame(n: int, cmd: str) -> str:
    body = f"N{n}{cmd}"
    return f"{body}*{checksum(body)}"


def load_gcode(path):
    """Strip comments/blank lines, returning the executable commands only."""
    cmds = []
    with open(path, "r", errors="replace") as fh:
        for raw in fh:
            line = raw.split(";", 1)[0].strip()
            if line:
                cmds.append(line)
    return cmds


def human_time(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


class Printer:
    def __init__(self, port, baud, reset=True, verbose=False):
        self.verbose = verbose
        self.temps = ""
        # dsrdtr/rtscts left default; we control the reset explicitly below.
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = 0.2
        if not reset:
            # Holding both low across open() avoids the auto-reset pulse.
            self.ser.dtr = False
            self.ser.rts = False
        self.ser.open()
        if reset:
            time.sleep(0.1)
            self.ser.dtr = False
            self.ser.rts = False

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def wait_boot(self, timeout=15.0):
        """Consume the boot banner. Returns the firmware line if seen."""
        deadline = time.time() + timeout
        fw = None
        while time.time() < deadline:
            line = self.readline()
            if line is None:
                continue
            if self.verbose:
                print(f"  < {line}")
            if "Marlin" in line and fw is None:
                fw = line
            if line.startswith("start"):
                # Give the LCD/EEPROM init a moment to settle before streaming.
                time.sleep(1.0)
                return fw
        return fw

    def readline(self):
        try:
            raw = self.ser.readline()
        except Exception as exc:
            raise RuntimeError(f"serial read failed: {exc}")
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace").strip()

    def send_raw(self, text):
        self.ser.write((text + "\n").encode("ascii", errors="replace"))
        self.ser.flush()
        if self.verbose:
            print(f"  > {text}")

    def send_and_wait(self, n, cmd):
        """
        Send one numbered command and block until Marlin acks it.
        Returns 'ok', or an int line number if Marlin asked for a resend.
        """
        self.send_raw(frame(n, cmd))
        last_data = time.time()
        while True:
            line = self.readline()
            if line is None:
                if time.time() - last_data > QUIET_TIMEOUT:
                    raise RuntimeError(
                        f"no response for {QUIET_TIMEOUT:.0f}s while sending: {cmd}"
                    )
                continue
            last_data = time.time()
            low = line.lower()

            if self.verbose:
                print(f"  < {line}")

            # Temperature telemetry can ride along with ok or arrive alone.
            if "t:" in low:
                self.temps = line.replace("ok ", "").strip()

            if low.startswith("ok"):
                return "ok"

            if low.startswith("rs") or "resend" in low:
                digits = "".join(c if c.isdigit() else " " for c in line).split()
                if digits:
                    return int(digits[0])
                raise RuntimeError(f"unparseable resend request: {line}")

            if line.startswith("!!") or "kill" in low:
                raise RuntimeError(f"printer reported a fatal error: {line}")

            if low.startswith("error"):
                # Checksum/line-number errors are recoverable via the resend
                # that Marlin sends next; anything else is not.
                if "checksum" in low or "line number" in low:
                    continue
                raise RuntimeError(f"printer error: {line}")

            if line.startswith("start"):
                raise RuntimeError(
                    "printer RESET mid-stream (saw 'start') -- print aborted"
                )
            # echo:busy, //, echo:, mesh output, etc: keep waiting.


def stream(path, port, baud, reset, verbose):
    cmds = load_gcode(path)
    if not cmds:
        sys.exit(f"{path}: no executable G-code found")

    total = len(cmds)
    print(f"File     : {path}")
    print(f"Commands : {total}")
    print(f"Port     : {port} @ {baud}")
    print()

    pr = Printer(port, baud, reset=reset, verbose=verbose)
    started = time.time()
    aborted = False
    try:
        if reset:
            print("Resetting board and waiting for Marlin...")
            fw = pr.wait_boot()
            print(f"  {fw or 'no banner seen (continuing)'}")
        else:
            time.sleep(0.5)
            pr.ser.reset_input_buffer()

        # Reset the line counter so our numbering starts from a known point.
        pr.send_and_wait(0, "M110 N0")

        for idx, cmd in enumerate(cmds, start=1):
            result = pr.send_and_wait(idx, cmd)
            while result != "ok":
                # Marlin wants an earlier line; we only ever need the current
                # one because we never run ahead of the ack.
                result = pr.send_and_wait(idx, cmd)

            if idx % 20 == 0 or idx == total:
                frac = idx / total
                elapsed = time.time() - started
                eta = elapsed / frac - elapsed if frac > 0 else 0
                sys.stdout.write(
                    f"\r  {frac*100:5.1f}%  {idx}/{total}  "
                    f"elapsed {human_time(elapsed)}  eta {human_time(eta)}  "
                    f"{pr.temps[:40]:<40}"
                )
                sys.stdout.flush()

        print("\n\nPrint stream complete.")

    except KeyboardInterrupt:
        aborted = True
        print("\n\n*** ABORT REQUESTED ***")
        print("Sending shutdown. NOTE: these queue behind buffered moves --")
        print("EMERGENCY_PARSER is impossible on ESP32, so this is NOT instant.")
        print("If the nozzle is driving into the bed, USE THE POWER SWITCH.")
        try:
            for cmd in ("M104 S0", "M140 S0", "M107", "M84"):
                pr.send_raw(cmd)
                time.sleep(0.2)
        except Exception:
            pass
    except Exception as exc:
        aborted = True
        print(f"\n\nFAILED: {exc}")
        print("Heaters are NOT automatically off after a link failure.")
        print("Power the printer down if you cannot re-establish control.")
    finally:
        pr.close()

    return 1 if aborted else 0


def dry_run(path):
    cmds = load_gcode(path)
    print(f"File     : {path}")
    print(f"Commands : {len(cmds)}")
    if not cmds:
        sys.exit("no executable G-code found")

    # Surface the things that actually bite on this machine.
    joined = "\n".join(cmds)
    print("\nPre-flight checks:")
    print(f"  first command : {cmds[0]}")
    print(f"  last command  : {cmds[-1]}")
    has_home = any(c.startswith("G28") for c in cmds)
    print(f"  homes (G28)   : {'yes' if has_home else 'NO -- unusual, check start G-code'}")
    if "M420 S1" in joined:
        print("  WARNING: M420 S1 present. Do not enable bed levelling while")
        print("           M851 Z is 0.00 -- the nozzle will print in air or crash.")
    print("\n  sample framing (what goes on the wire):")
    for i, c in enumerate(cmds[:3], start=1):
        print(f"    {frame(i, c)}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Stream G-code to the JOKER V2 over USB serial."
    )
    ap.add_argument("gcode", nargs="?", help="path to a .gcode file")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--no-reset", action="store_true",
                    help="attach without rebooting the board")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and validate only; never opens the port")
    ap.add_argument("--list-ports", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="echo every line to/from the printer")
    args = ap.parse_args()

    if args.list_ports:
        for p in list_ports.comports():
            print(f"{p.device}\t{p.description}")
        return 0

    if not args.gcode:
        ap.error("a .gcode file is required (or use --list-ports)")
    if not os.path.isfile(args.gcode):
        sys.exit(f"no such file: {args.gcode}")

    if args.dry_run:
        return dry_run(args.gcode)

    if not os.path.exists(args.port):
        sys.exit(f"port not found: {args.port}\nTry --list-ports.")

    return stream(args.gcode, args.port, args.baud,
                  reset=not args.no_reset, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())

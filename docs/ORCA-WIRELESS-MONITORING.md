# OrcaSlicer wireless monitoring — restored 2026-10-20

Use **Joker V2 Wireless → Device** in MagicMaker SLuban OrcaSlicer. The embedded original ESP3D dashboard is at [http://192.168.1.20](http://192.168.1.20). The Mac is on 192.168.1.14; both are on the local network. The printer rejoined its saved network after restoring STA mode.

- Host type: ESP3D (inherited from MM slb 0.4 nozzle).
- Hostname/IP: `192.168.1.20`.
- Device UI: `http://192.168.1.20`.
- Temperature auto-check enabled at 10 seconds; nozzle and bed displayed, both around 29 °C with heaters off during verification.
- Use **SD Files → Progress** to query SD print status. This original WebUI sends M27 and shows its reply in the Commands panel. Idle returned `Not SD printing`. A moving SD print and changing progress have not been tested in this session; no test print was started.
- If automatic temperature checks are off when reopening the page, tick **auto-check every**. The saved refresh interval is 10 seconds.

## What was fixed

The first cable did not enumerate. A replacement exposed CH340 at `/dev/cu.usbserial-2110`. Reading settings showed Wi-Fi was OFF while HTTP was ON. Set `[ESP110]STA`, verified readback, then restarted the idle controller with `[ESP444]RESTART`. It joined at 192.168.1.20. Orca still stored 192.168.8.109 and selected the wired profile; both were corrected with backups.

The firmware was not reflashed. Live M115 identified Marlin 2.0.8.1, build Aug 9 2026 17:26:07, thermal protection enabled. The served WebUI was already the original vendor version 2.1.2b0, byte-identical to the local saved file and the vendor Git blob `4b7051604a0e3db93c423cc4aa28164fe95beb75`. No replacement WebUI or external monitoring server was needed.

Original source: [MagicMaker vendor repository](https://github.com/magicmaker3/magicmaker), WebUI at `ESP32主控板JOKER SPARROW/index.html.gz`. [ESP3DLib](https://github.com/luc-github/ESP3DLib) supplies the printer's embedded web and feedback services.

## Verification and limits

The HTTP root returned status 200 and served the original gzip. Independent verification passed for idle wireless monitoring. WebSocket feedback on port 81 returned actual M105 nozzle/bed temperatures and M27 idle status. Orca's actual Device tab displayed successive temperature updates and the idle response; bed display and 10 second refresh preference were saved through the original UI. Actual printing, uploads, long-duration stability and recovery after disconnect remain untested here. USB was used only to restore Wi-Fi; wireless monitoring does not need an open serial connection.

DHCP can change the printer address. Reserve the printer's MAC (read it from `[ESP420]` or your router's client list) if a stable address is needed; router settings were not changed. Avoid opening serial during a print: this session observed a board restart even with DTR/RTS set false.

Evidence and recoverable pre-edit configuration copies: `../../.codex/runs/2026-10-20-orca-wifi/`. Earlier USB-monitor work is parked under the previous run; the separate WiFi SD transfer client is not required for Device-tab monitoring.

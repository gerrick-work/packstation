#!/bin/bash
# Shows how macOS sees the AiYin printers (plug them in by USB and switch them on first).
echo "== USB devices =="
system_profiler SPUSBDataType | grep -i -B2 -A8 -E "aiyin|printer|e40" || echo "(no AiYin found on USB)"
echo
echo "== Printer addresses macOS can use =="
lpinfo -v 2>/dev/null | grep -i usb || echo "(none)"
echo
echo "== Existing print queues =="
lpstat -v 2>/dev/null || echo "(none)"

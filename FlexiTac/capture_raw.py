#!/usr/bin/env python3
"""Record unchanged FlexiTac ADC8/ADC12 payloads, timestamps and a wire-stream copy."""

import argparse
from collections import Counter
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time

import serial

from flexitac_protocol import FrameParser, ROWS, COLS, crc16


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="e.g. /dev/ttyACM0 or COM3")
    parser.add_argument("--baud", type=int, default=2_000_000)
    parser.add_argument("--output", type=Path,
                        help="New recording directory; existing directories are never overwritten")
    parser.add_argument("--duration", type=float, default=0,
                        help="Recording seconds; 0 means until Ctrl+C")
    parser.add_argument("--protocol", choices=("mini", "legacy"), default="mini",
                        help="mini: CRC-checked ADC8/ADC12 packets; legacy: AA55+512 bytes, no CRC")
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or args.duration < 0 or args.baud <= 0:
        parser.error("duration must be finite and non-negative; baud must be positive")
    if args.output is None:
        args.output = Path("captures") / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    args.output = args.output.expanduser().resolve()
    return args


def record(args):
    parser = FrameParser(args.protocol)
    counts = Counter()
    total_frames = total_bytes = raw_bytes = 0
    frame_bytes = None
    settings = {"exclusive": True} if os.name == "posix" else {}
    # Open the serial device before creating output files; do not send commands or reset buffers.
    with serial.Serial(args.port, args.baud, timeout=0.05, **settings) as port:
        args.output.mkdir(parents=True, exist_ok=False)
        metadata = {
            "format_version": 2,
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "port": args.port, "baud_setting": args.baud, "protocol": args.protocol,
            "rows": ROWS, "cols": COLS, "dtype": None, "frame_bytes": None,
            "layout": "raw_adc.bin: frames in frames.csv order; C-order [frame, row, col]",
            "processing": "none: no baseline subtraction, threshold, normalization or smoothing",
            "adc_note": "Format detected from first valid packet; ADC12 uses uint16 little-endian",
            "host_time_note": "host clocks are sampled once per read chunk; batched frames share times",
            "device_time_note": "device_timestamp_ms is node-local uint32, not synchronized between boards",
            "status": "recording",
        }
        meta_path = args.output / "metadata.json"
        meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        started = last_report = time.monotonic()
        error = None
        print(f"Recording unchanged ADC frames (auto-detect uint8 or uint16 little-endian) to {args.output}", flush=True)
        try:
            with ExitStack() as stack:
                raw_file = stack.enter_context((args.output / "raw_adc.bin").open("xb"))
                wire_file = stack.enter_context((args.output / "serial_stream.bin").open("xb"))
                index_file = stack.enter_context(
                    (args.output / "frames.csv").open("x", newline="", encoding="utf-8"))
                writer = csv.writer(index_file)
                writer.writerow(("frame_index", "node_id", "node_frame_index", "device_timestamp_ms",
                                 "host_unix_ns", "host_monotonic_ns", "byte_offset", "crc_verified"))
                while not args.duration or time.monotonic() - started < args.duration:
                    chunk = port.read(max(1, min(port.in_waiting, 8192)))
                    host_unix_ns, host_monotonic_ns = time.time_ns(), time.monotonic_ns()
                    if chunk:
                        wire_file.write(chunk)
                        total_bytes += len(chunk)
                        for node, timestamp_ms, payload in parser.feed(chunk):
                            if frame_bytes is None:
                                frame_bytes = len(payload)
                                metadata.update({
                                    "frame_bytes": frame_bytes,
                                    "dtype": "uint8" if frame_bytes == ROWS * COLS else "<u2",
                                    "adc_bits": 8 if frame_bytes == ROWS * COLS else 12,
                                    "adc_note": ("Firmware right-shifted ADC by 4; low bits are lost"
                                                 if frame_bytes == ROWS * COLS else
                                                 "Full 12-bit ADC counts in little-endian uint16; no shift"),
                                })
                                meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
                                print(f"Detected {metadata['adc_bits']}-bit data: "
                                      f"{frame_bytes} bytes/frame, dtype={metadata['dtype']}", flush=True)
                            elif len(payload) != frame_bytes:
                                raise ValueError("Mixed ADC8/ADC12 frames in one recording. "
                                                 "Use the same firmware on all connected boards, "
                                                 "or record each board separately.")
                            raw_file.write(payload)
                            writer.writerow((total_frames, node, counts[node], timestamp_ms,
                                             host_unix_ns, host_monotonic_ns,
                                             raw_bytes, int(args.protocol == "mini")))
                            raw_bytes += len(payload)
                            total_frames += 1
                            counts[node] += 1
                    now = time.monotonic()
                    if now - last_report >= 1:
                        for file in (raw_file, wire_file, index_file):
                            file.flush()
                        print(f"{now - started:.1f}s frames={total_frames} nodes={dict(counts)} "
                              f"bad_crc={parser.bad_crc} discarded_bytes={parser.discarded_bytes}",
                              flush=True)
                        last_report = now
        except KeyboardInterrupt:
            metadata["status"] = "stopped_by_user"
        except Exception as exc:
            metadata["status"] = "error"
            error = str(exc)
            raise
        else:
            metadata["status"] = "completed" if total_frames else "no_valid_frames"
        finally:
            metadata.update({
                "ended_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": time.monotonic() - started,
                "valid_frames": total_frames, "node_frames": dict(counts),
                "received_bytes": total_bytes, "bad_crc_candidates": parser.bad_crc,
                "bad_header_candidates": parser.bad_header,
                "discarded_bytes": parser.discarded_bytes,
                "trailing_unparsed_bytes": len(parser.buffer), "error": error,
            })
            meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            print(f"Saved {total_frames} frames ({dict(counts)}) to {args.output}", flush=True)
    return 0 if total_frames else 2


def main(argv=None):
    args = parse_args(argv)
    try:
        return record(args)
    except (OSError, serial.SerialException, ValueError) as exc:
        print(f"Recording failed: {exc}", file=sys.stderr)
        print("Check the port, serial permissions, free disk space, and close other serial readers. "
              "Use a new --output directory for each run.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

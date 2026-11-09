#!/usr/bin/env python3
"""Measure FlexiTac frame rate and serial throughput without rendering a heatmap."""

import argparse
import statistics
import time
from collections import defaultdict, deque

import serial


from flexitac_protocol import FrameParser as PacketParser

UINT32_MASK = 0xFFFFFFFF
UINT32_HALF = 0x80000000


class FrameParser(PacketParser):
    def feed(self, chunk):
        return [(node, timestamp) for node, timestamp, _ in super().feed(chunk)]


class NodeStats:
    def __init__(self):
        self.host_events = deque()
        self.firmware_events = deque()
        self.firmware_intervals_ms = deque()
        self.last_timestamp = None
        self.cumulative_timestamp_ms = 0
        self.total_frames = 0
        self.timestamp_resets = 0

    def add(self, now, timestamp_ms, window_s):
        self.total_frames += 1
        self.host_events.append(now)
        while self.host_events and self.host_events[0] < now - window_s:
            self.host_events.popleft()

        if timestamp_ms is None:
            return

        if self.last_timestamp is None:
            self.cumulative_timestamp_ms = 0
            self.firmware_events.clear()
            self.firmware_intervals_ms.clear()
            self.firmware_events.append((now, 0))
        else:
            delta = (timestamp_ms - self.last_timestamp) & UINT32_MASK
            if 0 < delta < UINT32_HALF:
                self.cumulative_timestamp_ms += delta
                self.firmware_events.append((now, self.cumulative_timestamp_ms))
                self.firmware_intervals_ms.append((now, delta))
            else:
                self.timestamp_resets += 1
                self.cumulative_timestamp_ms = 0
                self.firmware_events.clear()
                self.firmware_intervals_ms.clear()
                self.firmware_events.append((now, 0))
        self.last_timestamp = timestamp_ms

        cutoff = now - window_s
        while self.firmware_events and self.firmware_events[0][0] < cutoff:
            self.firmware_events.popleft()
        while self.firmware_intervals_ms and self.firmware_intervals_ms[0][0] < cutoff:
            self.firmware_intervals_ms.popleft()

    def rates(self):
        host_fps = 0.0
        if len(self.host_events) > 1:
            elapsed = self.host_events[-1] - self.host_events[0]
            if elapsed > 0:
                host_fps = (len(self.host_events) - 1) / elapsed

        firmware_fps = 0.0
        if len(self.firmware_events) > 1:
            elapsed_ms = self.firmware_events[-1][1] - self.firmware_events[0][1]
            if elapsed_ms > 0:
                firmware_fps = (len(self.firmware_events) - 1) * 1000.0 / elapsed_ms

        intervals = [interval for _, interval in self.firmware_intervals_ms]
        median_ms = statistics.median(intervals) if intervals else 0.0
        p95_ms = statistics.quantiles(intervals, n=20)[18] if len(intervals) >= 2 else median_ms
        long_gaps = sum(interval > 1.8 * median_ms for interval in intervals) if median_ms else 0
        return host_fps, firmware_fps, median_ms, p95_ms, long_gaps


def parse_args():
    parser = argparse.ArgumentParser(
        description="Measure per-node FlexiTac frame rate and received byte throughput."
    )
    parser.add_argument("--port", required=True, help="Serial device, e.g. /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=2_000_000,
                        help="USB CDC host setting (default: 2000000)")
    parser.add_argument("--window", type=float, default=5.0,
                        help="Rolling measurement window in seconds (default: 5)")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Stop after this many seconds; 0 runs until Ctrl+C")
    parser.add_argument("--protocol", choices=("mini", "legacy"), default="mini")
    args = parser.parse_args()
    if args.window <= 0 or args.duration < 0:
        parser.error("--window must be positive and --duration cannot be negative")
    return args


def main():
    args = parse_args()
    parser = FrameParser(args.protocol)
    nodes = defaultdict(NodeStats)
    byte_events = deque()
    total_bytes = 0
    total_frames = 0
    start = time.monotonic()
    last_report = start

    try:
        with serial.Serial(args.port, args.baud, timeout=0.1) as port:
            port.reset_input_buffer()
            print(f"Measuring {args.port} at USB CDC setting {args.baud}; "
                  f"window={args.window:g}s. Press Ctrl+C to stop.")
            while True:
                chunk = port.read(8192)
                now = time.monotonic()
                if chunk:
                    total_bytes += len(chunk)
                    byte_events.append((now, len(chunk)))
                    for node_id, timestamp_ms in parser.feed(chunk):
                        nodes[node_id].add(now, timestamp_ms, args.window)
                        total_frames += 1

                while byte_events and byte_events[0][0] < now - args.window:
                    byte_events.popleft()

                if now - last_report >= 1.0:
                    elapsed = now - start
                    rate_window_s = max(min(args.window, elapsed), 1e-9)
                    rx_bps = sum(size for _, size in byte_events) / rate_window_s
                    node_text = []
                    for node_id, stats in sorted(nodes.items()):
                        host_fps, fw_fps, median_ms, p95_ms, long_gaps = stats.rates()
                        fw_text = f"fw={fw_fps:.1f}fps" if node_id != "legacy" else "fw=n/a"
                        node_text.append(
                            f"{node_id}: host={host_fps:.1f}fps {fw_text} "
                            f"dt_med={median_ms:.1f}ms dt_p95={p95_ms:.1f}ms "
                            f"long_gaps={long_gaps}"
                        )
                    summary = " | ".join(node_text) if node_text else "waiting for valid frames"
                    print(
                        f"t={elapsed:6.1f}s rx={rx_bps / 1024:7.1f}KiB/s "
                        f"bytes={total_bytes} frames={total_frames} "
                        f"bad_crc={parser.bad_crc} bad_header={parser.bad_header} "
                        f"discarded={parser.discarded_bytes} | {summary}",
                        flush=True,
                    )
                    last_report = now

                if args.duration and now - start >= args.duration:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        runtime = max(time.monotonic() - start, 1e-9)
        print(f"\nStopped after {runtime:.1f}s: {total_frames} valid frames, "
              f"{total_bytes / runtime / 1024:.1f}KiB/s average received data.")
        for node_id, stats in sorted(nodes.items()):
            host_fps, fw_fps, median_ms, p95_ms, long_gaps = stats.rates()
            fw_text = f"{fw_fps:.2f}fps" if node_id != "legacy" else "unavailable (legacy frame)"
            print(f"Node {node_id}: total={stats.total_frames}, "
                  f"firmware-timestamp rate={fw_text}, current host rate={host_fps:.2f}fps, "
                  f"median interval={median_ms:.2f}ms, p95 interval={p95_ms:.2f}ms, "
                  f"long gaps={long_gaps}, timestamp resets={stats.timestamp_resets}")
        print(f"Invalid CRC frames: {parser.bad_crc}; invalid headers: {parser.bad_header}; "
              f"discarded bytes: {parser.discarded_bytes}")


if __name__ == "__main__":
    main()

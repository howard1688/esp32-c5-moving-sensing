"""Measure live CSI delivery without GUI processing. Close the viewer first."""
import argparse
import json
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from protocol import decode, SequenceTracker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=15)
    parser.add_argument('--port', type=int, default=5500)
    args = parser.parse_args()
    if args.seconds < 1 or not 1 <= args.port <= 65535:
        parser.error('seconds must be positive; port must be 1..65535')
    counts = [0] * args.seconds
    nodes = {}
    invalid = 0
    tracker = SequenceTracker()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        try:
            sock.bind(('0.0.0.0', args.port))
        except OSError as exc:
            print(f'Cannot listen on UDP {args.port}: {exc}. Close the CSI viewer first.', file=sys.stderr)
            return 1
        sock.settimeout(.2)
        start = time.monotonic()
        while time.monotonic() - start < args.seconds:
            try:
                packet, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            elapsed = time.monotonic() - start
            if elapsed >= args.seconds:
                break
            try:
                if packet.startswith(b'{'):
                    status = json.loads(packet)
                    if status.get('type') == 'status' and status.get('name') in ('esp32c5-1', 'esp32c5-2'):
                        rows = nodes.setdefault(status['name'], [])
                        if rows and rows[-1][1].get('boot') != status.get('boot'):
                            rows.clear()
                        rows.append((elapsed, status))
                elif tracker.accept(decode(packet)):
                    counts[int(elapsed)] += 1
            except (ValueError, TypeError, KeyError, UnicodeError):
                invalid += 1
    print(json.dumps(dict(seconds=args.seconds, csi_per_second=counts,
        average_csi_hz=sum(counts) / args.seconds, invalid=invalid,
        post_capture_missing=tracker.missing, late_or_duplicate=tracker.late), indent=2))
    for name, rows in nodes.items():
        first_t, first = rows[0]
        last_t, last = rows[-1]
        duration = ((last['uptime_us'] - first['uptime_us']) / 1e6
                    if 'uptime_us' in first and 'uptime_us' in last else last_t - first_t)
        key = 'sent' if name == 'esp32c5-1' else 'captured'
        result = dict(node=name, target_hz=last['target_hz'])
        if duration > 0:
            result['measured_hz'] = round(((last[key] - first[key]) & 0xffffffff) / duration, 2)
        for field in ('queue_drops', 'udp_errors', 'send_errors', 'busy_skips', 'schedule_skips', 'invalid_length'):
            if field in last and field in first:
                result[field + '_delta'] = (last[field] - first[field]) & 0xffffffff
        print(json.dumps(result))
    if not sum(counts):
        print('No valid CSI received. Check hotspot, board roles and UDP 5500.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

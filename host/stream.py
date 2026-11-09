"""UDP/demo/replay producers and lossless raw datagram JSONL recording."""
import base64
import json
from pathlib import Path
import queue
import socket
import threading
import time
import struct
import secrets
import numpy as np
from protocol import Sample, encode


class Source(threading.Thread):
    def __init__(self, bind='0.0.0.0', port=5500, demo=False, replay=None):
        super().__init__(daemon=True)
        self.bind, self.port, self.demo, self.replay = bind, port, demo, replay
        self.items = queue.Queue(maxsize=8192)
        self.commands = queue.Queue(maxsize=16)
        self.stop_event = threading.Event()
        self.drops = 0
        self.error = None
        self.finished = False

    def set_rate(self, address, hz, persist=False, port=5501):
        if self.demo or self.replay:
            raise RuntimeError('只有即時接收能設定發送率')
        token = secrets.randbits(32)
        packet = struct.pack('<4sBBHI', b'RATE', 1, int(bool(persist)), hz, token)
        self.commands.put_nowait((packet, address, port))
        return token

    def emit(self, data, address, received=None):
        try:
            self.items.put_nowait((data, address, time.time() if received is None else received))
        except queue.Full:
            self.drops += 1

    def run(self):
        try:
            if self.demo:
                self.run_demo()
            elif self.replay:
                self.run_replay()
            else:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                    sock.bind((self.bind, self.port))
                    sock.settimeout(.2)
                    while not self.stop_event.is_set():
                        try:
                            while True:
                                packet, address, port = self.commands.get_nowait()
                                sock.sendto(packet, (address, port))
                        except queue.Empty:
                            pass
                        try:
                            data, address = sock.recvfrom(4096)
                            self.emit(data, address[0])
                        except socket.timeout:
                            pass
        except Exception as exc:
            self.error = f'{type(exc).__name__}: {exc}'
        finally:
            self.finished = True

    def run_replay(self):
        first = None
        started = time.monotonic()
        with open(self.replay, encoding='utf-8') as file:
            for line in file:
                if self.stop_event.is_set():
                    break
                row = json.loads(line)
                if row.get('kind') == 'metadata':
                    self.emit(json.dumps(dict(row, type='metadata')).encode(), 'replay')
                    continue
                if row.get('kind') in ('settings', 'motion_event'):
                    if first is not None and 'received' in row:
                        wait = row['received'] - first - (time.monotonic() - started)
                        if wait > 0 and self.stop_event.wait(wait):
                            break
                    self.emit(json.dumps(dict(row, type=row['kind'])).encode(), 'replay')
                    continue
                if row.get('kind') not in ('datagram', 'marker'):
                    continue
                if first is None:
                    first = row['received']
                wait = row['received'] - first - (time.monotonic() - started)
                if wait > 0 and self.stop_event.wait(wait):
                    break
                if row['kind'] == 'datagram':
                    self.emit(base64.b64decode(row['base64'], validate=True), row.get('address', 'replay'), row['received'])
                else:
                    self.emit(json.dumps({'type': 'marker', 'label': row['label']}).encode(), 'replay', row['received'])

    def run_demo(self):
        rng = np.random.default_rng(5)
        start = time.monotonic()
        seq = 0
        bins = np.arange(245)
        while not self.stop_event.is_set():
            t = seq / 200
            motion = 0 if t < 12 else max(0, np.sin((t - 12) * np.pi / 5))
            amp = 30 + 7 * np.sin(bins / 9) + rng.normal(0, .65, len(bins))
            amp += motion * 10 * np.sin(bins / 7 + t * 8)
            iq = np.column_stack((amp * np.sin(bins / 5), amp * np.cos(bins / 5)))
            data = np.clip(np.rint(iq), -127, 127).astype(np.int8).tobytes()
            s = Sample(1, 2, 2, 12345, seq, int(t * 1e6) + 1000000,
                       (int(t * 1e6) + 1000000) & 0xffffffff, 0, 0,
                       int(-48 + motion * 2 + rng.normal(0, .5)), -96, 36, 4,
                       20, 0, seq % 4096, len(data), 0, 1.0,
                       bytes.fromhex('0245534fc501'), 'esp32c5-2', data)
            self.emit(encode(s), 'SIMULATION')
            if seq % 200 == 0:
                for node in (1, 2):
                    self.emit(json.dumps(dict(type='status', name=f'esp32c5-{node}',
                        boot=12345, channel=36, target_hz=200, sent=seq if node == 1 else 0,
                        captured=seq if node == 2 else 0, queue_drops=0,
                        udp_errors=0, send_errors=0)).encode(), 'SIMULATION')
            seq += 1
            self.stop_event.wait(max(0, start + seq / 200 - time.monotonic()))


class Recorder:
    def __init__(self, path, mode):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('x', encoding='utf-8')
        self.path = path
        self.write(dict(kind='metadata', schema=1, mode=mode, created=time.time()))

    def write(self, row):
        self.file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')

    def datagram(self, data, address, received):
        self.write(dict(kind='datagram', received=received, address=address,
                        base64=base64.b64encode(data).decode('ascii')))

    def marker(self, label):
        self.write(dict(kind='marker', received=time.time(), label=label))
        self.file.flush()

    def close(self):
        self.file.close()

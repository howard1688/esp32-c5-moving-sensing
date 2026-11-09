"""Versioned CSI5 datagrams. No native alignment or untrusted allocations."""
from dataclasses import dataclass
import math
import struct

HEADER = struct.Struct('<4sBBHIIQIIIbbBBBbHHHf6s16s')
MAX_CSI = 512


@dataclass(frozen=True)
class Sample:
    version: int
    node: int
    flags: int
    boot: int
    seq: int
    timestamp_us: int
    radio_timestamp_us: int
    queue_drops: int
    udp_errors: int
    rssi: int
    noise_floor: int
    channel: int
    format: int
    agc: int
    fft: int
    rx_seq: int
    csi_len: int
    reserved: int
    gain_compensation: float
    source_mac: bytes
    name: str
    data: bytes

    @property
    def signature(self):
        return (self.boot, self.channel, self.format, self.csi_len, self.source_mac, self.flags & 2)


def decode(packet: bytes) -> Sample:
    if len(packet) < HEADER.size:
        raise ValueError('封包標頭不完整')
    fields = HEADER.unpack_from(packet)
    magic, version, node, flags = fields[:4]
    length = fields[17]
    if magic != b'CSI5' or version != 1 or node != 2:
        raise ValueError('無法辨識協定或設備')
    if flags & ~3 or fields[13] != 4 or fields[12] <= 14:
        raise ValueError('需要 5 GHz HE20 SU CSI 資料')
    if not 4 <= length <= MAX_CSI or length % 2 or len(packet) != HEADER.size + length:
        raise ValueError('CSI 資料長度錯誤')
    if length != 490:
        raise ValueError(f'CSI 格式不符：收到 {length} 位元組，預期 490')
    gain = fields[19]
    if not math.isfinite(gain) or gain <= 0:
        raise ValueError('增益數值錯誤')
    name = fields[21].split(b'\0', 1)[0].decode('ascii', errors='strict')
    if name != 'esp32c5-2' or fields[20] != bytes.fromhex('0245534fc501'):
        raise ValueError('設備識別不符')
    return Sample(*fields[1:21], name, packet[HEADER.size:])


def encode(s: Sample) -> bytes:
    return HEADER.pack(b'CSI5', s.version, s.node, s.flags, s.boot, s.seq,
        s.timestamp_us, s.radio_timestamp_us, s.queue_drops, s.udp_errors,
        s.rssi, s.noise_floor, s.channel, s.format, s.agc, s.fft, s.rx_seq,
        len(s.data), 0, s.gain_compensation, s.source_mac,
        s.name.encode('ascii').ljust(16, b'\0')) + s.data


class SequenceTracker:
    """Counts gaps AFTER CSI callback, not over-air probe loss. Ignore late/duplicate."""
    def __init__(self):
        self.boot = self.last = None
        self.missing = self.late = 0

    def accept(self, sample):
        if self.boot != sample.boot:
            self.boot, self.last = sample.boot, sample.seq
            self.missing = self.late = 0
            return True
        delta = (sample.seq - self.last) & 0xffffffff
        if delta == 0 or delta >= 0x80000000:
            self.late += 1
            return False
        self.missing += delta - 1
        self.last = sample.seq
        return True

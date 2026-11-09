"""Protocol corruption, calibration, loss accounting and real localhost UDP tests."""
import base64
from dataclasses import replace
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import time
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from protocol import Sample, HEADER, encode, decode, SequenceTracker
from processing import Processor
from respiration import Respiration
from stream import Source, Recorder


def sample(seq=0, amp=30, **kwargs):
    iq = np.zeros((245, 2), dtype=np.int8)
    iq[:, 1] = amp
    defaults = dict(version=1, node=2, flags=2, boot=7, seq=seq,
        timestamp_us=1_000_000 + seq * 5000, radio_timestamp_us=seq * 5000,
        queue_drops=0, udp_errors=0, rssi=-50, noise_floor=-95,
        channel=36, format=4, agc=20, fft=-2, rx_seq=seq % 4096,
        csi_len=490, reserved=0, gain_compensation=1.0,
        source_mac=bytes.fromhex('0245534fc501'), name='esp32c5-2', data=iq.tobytes())
    defaults.update(kwargs)
    return Sample(**defaults)


class ProtocolTests(unittest.TestCase):
    def test_wire_roundtrip(self):
        self.assertEqual(HEADER.size, 74)
        expected = sample(123, rssi=-73, fft=-9, timestamp_us=2**40)
        self.assertEqual(decode(encode(expected)), expected)

    def test_reject_corrupt(self):
        packet = encode(sample())
        bad = [packet[:73], packet[:-1], packet + b'0', b'BAD!' + packet[4:],
            encode(sample(version=9)), encode(sample(channel=6)), encode(sample(format=2)),
            encode(sample(gain_compensation=float('nan'))), encode(sample(gain_compensation=0)),
            encode(sample(name='other')), encode(sample(data=bytes(128))),
            encode(sample(source_mac=bytes(6)))]
        for value in bad:
            with self.subTest(value=value[:12]), self.assertRaises(ValueError):
                decode(value)

    def test_sequence_wrap_reboot_and_late(self):
        tracker = SequenceTracker()
        self.assertTrue(tracker.accept(sample(0xfffffffe)))
        self.assertTrue(tracker.accept(sample(1)))
        self.assertEqual(tracker.missing, 2)
        self.assertFalse(tracker.accept(sample(0)))
        self.assertFalse(tracker.accept(sample(1)))
        self.assertEqual(tracker.late, 2)
        self.assertTrue(tracker.accept(sample(0, boot=8)))
        self.assertEqual(tracker.missing, 0)


class ProcessingTests(unittest.TestCase):
    def calibrated(self):
        p = Processor(1)
        for i in range(202):
            p.process(sample(i, amp=30 + (i % 3 - 1)))
        self.assertIsNotNone(p.baseline)
        return p

    def test_motion_exceeds_still_and_dc_excluded(self):
        p = self.calibrated()
        self.assertFalse(p.valid[0])
        still = p.process(sample(202, amp=30))
        motion = p.process(sample(203, amp=45))
        self.assertGreater(motion['deviation'], still['deviation'] + 5)
        self.assertTrue(np.isnan(motion['heat'][0]))

    def test_gain_compensation_removes_scale_jump(self):
        p = self.calibrated()
        r = p.process(sample(202, amp=60, gain_compensation=.5, agc=26))
        self.assertAlmostEqual(r['raw'], 60)
        self.assertAlmostEqual(r['corrected'], 30)
        self.assertLess(r['deviation'], 1)

    def test_invalid_words_keep_bin_positions(self):
        p = Processor(1)
        r = p.process(sample(flags=3))
        self.assertEqual(len(r['heat']), 245)
        self.assertTrue(np.isnan(r['heat'][:2]).all())
        self.assertTrue(np.isnan(r['heat'][-1]))
        self.assertEqual(r['heat'][2], 30)

    def test_boot_and_channel_reset_calibration(self):
        p = self.calibrated()
        p.process(sample(0, boot=8))
        self.assertIsNone(p.baseline)
        p = self.calibrated()
        p.process(sample(202, channel=40))
        self.assertIsNone(p.baseline)

    def test_long_gap_not_an_impulse(self):
        p = self.calibrated()
        r = p.process(sample(800, amp=50))
        self.assertTrue(np.isnan(r['difference']))
        self.assertTrue(np.isnan(r['variation']))

    def test_static_offset_does_not_remain_a_motion_score(self):
        p = self.calibrated()
        for i in range(202, 502):
            result = p.process(sample(i, amp=50))
        self.assertGreater(result['deviation'], 5)
        self.assertAlmostEqual(result['variation'], 0, places=5)
        self.assertLess(np.nanmax(result['motion_std']), 1e-5)

    def test_rolling_variance_matches_full_window_at_500_hz(self):
        p = self.calibrated()
        rng = np.random.default_rng(12)
        timestamp = p.previous_us
        for i in range(420):
            timestamp += int(rng.integers(1500, 3500))
            iq = np.zeros((245, 2), dtype=np.int8)
            iq[:, 1] = rng.integers(20, 50, 245)
            result = p.process(sample(202 + i, timestamp_us=timestamp, data=iq.tobytes()))
            if len(p.window) > 1:
                values = np.asarray([row[1][p.valid] for row in p.window])
                expected = np.sqrt(np.mean(np.var(values, axis=0)))
                self.assertAlmostEqual(result['variation'], expected, places=10)
        # Statistics from before a transport gap must not leak into the new window.
        timestamp += 300000
        self.assertTrue(np.isnan(p.process(sample(700, timestamp_us=timestamp))['variation']))
        result = p.process(sample(701, timestamp_us=timestamp + 2000, amp=40))
        expected = np.sqrt(np.mean(np.var([row[1][p.valid] for row in p.window], axis=0)))
        self.assertAlmostEqual(result['variation'], expected, places=10)

    def test_kalman_compares_against_same_corrected_amplitude(self):
        p = Processor(1, kalman_q=1, kalman_r=100)
        first = p.process(sample(0, amp=30))
        changed = p.process(sample(1, amp=60))
        self.assertEqual(first['kalman'], first['corrected'])
        self.assertGreater(changed['kalman'], 30)
        self.assertLess(changed['kalman'], changed['corrected'])
        p.set_kalman_params(25, 25)
        reset = p.process(sample(2, amp=50))
        self.assertEqual(reset['kalman'], reset['corrected'])

    def test_reordered_time_rejected(self):
        p = Processor(1)
        p.process(sample(3))
        with self.assertRaises(ValueError):
            p.process(sample(2))

    def test_respiration_periodic_he20_subcarrier(self):
        detector = Respiration()
        result = None
        bins = np.arange(245)
        for n in range(310):
            t = n / 10
            values = 30 + 5 * np.sin(bins / 20) + .02 * np.sin(n + bins)
            values[50:70] += 2 * np.sin(2 * np.pi * .25 * t)
            values[[0, 1, -1]] = np.nan
            result = detector.process(t, values)
        self.assertAlmostEqual(result[1], 15, delta=2)
        self.assertTrue(50 <= detector.bin < 70)

    def test_respiration_rejects_uncorrelated_noise(self):
        detector = Respiration()
        rng = np.random.default_rng(7)
        for n in range(310):
            detector.process(n / 10, 30 + rng.normal(0, .3, 245))
        self.assertTrue(np.isnan(detector.bpm))
        self.assertTrue(np.isfinite(detector.candidate_bpm))
        self.assertIsNotNone(detector.bin)


class TransportTests(unittest.TestCase):
    def test_rate_command_wire_format(self):
        source = Source('127.0.0.1', 5500)
        token = source.set_rate('192.0.2.1', 500, True)
        packet, address, port = source.commands.get_nowait()
        self.assertEqual(address, '192.0.2.1')
        self.assertEqual(port, 5501)
        magic, version, persist, hz, parsed_token = struct.unpack('<4sBBHI', packet)
        self.assertEqual((magic, version, persist, hz, parsed_token),
                         (b'RATE', 1, 1, 500, token))

    def test_rate_command_disabled_outside_live_mode(self):
        for source in (Source(demo=True), Source(replay=Path('capture.jsonl'))):
            with self.assertRaises(RuntimeError):
                source.set_rate('127.0.0.1', 200)

    def test_rate_command_sent_over_udp(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            control.bind(('127.0.0.1', 0))
            control.settimeout(1)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
                reserve.bind(('127.0.0.1', 0))
                receive_port = reserve.getsockname()[1]
            source = Source('127.0.0.1', receive_port)
            source.start()
            try:
                token = source.set_rate('127.0.0.1', 250, False, control.getsockname()[1])
                packet, address = control.recvfrom(64)
                self.assertEqual(struct.unpack('<4sBBHI', packet), (b'RATE', 1, 0, 250, token))
                self.assertEqual(address[1], receive_port)
            finally:
                source.stop_event.set()
                source.join(1)
            self.assertIsNone(source.error)

    def test_localhost_udp(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        source = Source('127.0.0.1', port)
        source.start()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                for _ in range(20):
                    sender.sendto(encode(sample(42)), ('127.0.0.1', port))
                    try:
                        packet, address, received = source.items.get(timeout=.05)
                        break
                    except Exception:
                        pass
                else:
                    self.fail(source.error or 'UDP receive timeout')
            self.assertEqual(decode(packet).seq, 42)
            self.assertEqual(address, '127.0.0.1')
        finally:
            source.stop_event.set()
            source.join(1)
        self.assertIsNone(source.error)

    def test_record_replay_preserves_raw_and_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.jsonl'
            r = Recorder(path, 'TEST')
            packet = encode(sample(3))
            r.datagram(packet, '127.0.0.1', time.time())
            r.marker('wave')
            r.close()
            source = Source(replay=path)
            source.start()
            source.join(2)
            self.assertIsNone(source.error)
            self.assertEqual(json.loads(source.items.get_nowait()[0])['mode'], 'TEST')
            self.assertEqual(source.items.get_nowait()[0], packet)
            self.assertEqual(json.loads(source.items.get_nowait()[0])['label'], 'wave')
            with self.assertRaises(FileExistsError):
                Recorder(path, 'TEST')


if __name__ == '__main__':
    unittest.main()

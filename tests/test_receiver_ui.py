import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import tempfile
import time
import queue
import numpy as np
from unittest.mock import patch
from test_host import sample
from protocol import encode

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from PyQt5 import QtWidgets
from receiver import Window, TRAINING_PROMPT
from motion_store import read_profile


class RateSelectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_status_does_not_replace_user_selection(self):
        args = SimpleNamespace(demo=False, replay=None, calibration=10, bind='127.0.0.1',
            port=0, record=None, quit_after=0, screenshot=None)
        window = Window(args)
        status = dict(type='status', name='esp32c5-1', boot=1, channel=36,
            target_hz=500, startup_hz=500, control_port=5501, sent=1,
            captured=0, queue_drops=0, udp_errors=0, send_errors=0)
        packet = json.dumps(status).encode()
        try:
            window.consume(packet, '192.0.2.10', 0)
            self.assertEqual(window.rate.currentText(), '500')
            window.rate.setCurrentText('100')
            window.rate_selected()
            window.consume(packet, '192.0.2.10', 1)
            self.assertEqual(window.rate.currentText(), '100')
        finally:
            window.close()

    def test_start_prompt_and_inactivity_alarm_lifecycle(self):
        args = SimpleNamespace(demo=False, replay=None, calibration=10, bind='127.0.0.1',
            port=0, record=None, quit_after=0, screenshot=None)
        window = Window(args)
        try:
            self.assertEqual(window.motion_status.text(), TRAINING_PROMPT)
            window.draw(time.monotonic())
            self.assertEqual(window.motion_status.text(), TRAINING_PROMPT)
            window.inactivity_seconds.setValue(2)
            window.motion.enter, window.motion.exit = 1.2, .8
            window.motion.result = dict(state='no_motion', score=.2, margin=None)
            for n in range(20):
                window.update_inactivity(10 + n * .1)
            self.assertFalse(window.inactivity_alarm)
            window.last_seen = time.monotonic()
            window.draw(window.last_seen)
            self.assertEqual(window.motion_status.text(), '靜止　1 秒')
            window.update_inactivity(12.1)
            self.assertTrue(window.inactivity_alarm)
            started = window.inactivity_alarm_started
            window.last_seen = started
            window.draw(started + .1)
            self.assertEqual(window.motion_status.text(), '長時間未移動警報')
            bright = window.motion_status.styleSheet()
            window.draw(started + .6)
            self.assertEqual(window.motion_status.text(), '長時間未移動警報')
            self.assertNotEqual(bright, window.motion_status.styleSheet())
            window.motion.active = True
            window.motion.result = dict(state='active', score=2, margin=None)
            window.update_inactivity(12.2)
            window.draw(started + .7)
            self.assertFalse(window.inactivity_alarm)
            self.assertEqual(window.motion_status.text(), '活動')
            window.motion.result = dict(state='no_motion', score=.2, margin=None)
            window.motion.active = False
            for n in range(22):
                window.update_inactivity(13 + n * .1)
            self.assertTrue(window.inactivity_alarm)
            window.inactivity_seconds.setValue(3)
            self.assertFalse(window.inactivity_alarm)
            self.assertEqual(window.inactivity_elapsed, 0)
            window.update_inactivity(16)
            window.update_inactivity(20)  # A missing interval cannot count as stillness.
            self.assertEqual(window.inactivity_elapsed, 0)
            window.motion.start_capture('still', 3)
            window.update_inactivity(20.1)
            self.assertIsNone(window.inactivity_since)
            window.motion.cancel_capture()
            window.motion.enter_since = 20.2
            window.update_inactivity(20.3)
            self.assertIsNone(window.inactivity_since)  # Pending movement cannot count as quiet.
            window.motion.enter_since = None
            for n in range(32):
                window.update_inactivity(21 + n * .1)
            self.assertTrue(window.inactivity_alarm)
            window.draw(started + 5)  # A stale source must cancel an active alarm.
            self.assertFalse(window.inactivity_alarm)
            self.assertEqual(window.motion_status.text(), '')
            window.inactivity_seconds.setValue(0)
            for n in range(50):
                window.update_inactivity(30 + n * .1)
            self.assertFalse(window.inactivity_alarm)
            self.assertAlmostEqual(window.inactivity_elapsed, 4.9)
            window.last_seen = time.monotonic()
            window.draw(window.last_seen)
            self.assertEqual(window.motion_status.text(), '靜止　4 秒')
        finally:
            window.close()

    def test_rate_uses_receipt_time_and_device_counter_time(self):
        args = SimpleNamespace(demo=False, replay=None, calibration=10, bind='127.0.0.1',
            port=0, record=None, quit_after=0, screenshot=None)
        window = Window(args)
        try:
            for i in range(1001):
                window.consume(encode(sample(i, timestamp_us=1000000 + i * 2000)), '192.0.2.11', 100 + i * .002)
            for i in range(2):
                status = dict(type='status', name='esp32c5-1', boot=1, channel=36,
                    target_hz=500, sent=i * 2500, uptime_us=1000000 + i * 5000000,
                    captured=0, queue_drops=0, udp_errors=0, send_errors=0)
                window.consume(json.dumps(status).encode(), '192.0.2.10', 100 + i * 5.3)
            with patch('receiver.time.time', return_value=102):
                window.draw(0)
            self.assertIn('近 1 秒 500 筆', window.status.text())
            self.assertIn('近 5 秒平均 500.0', window.status.text())
            self.assertIn('實際發送 500.0', window.devices.text())
            with patch('receiver.time.time', return_value=108):
                window.draw(10)
            self.assertIn('近 5 秒平均 0.0', window.status.text())
        finally:
            window.close()

    def test_training_validation_storage_replay_and_invalidation(self):
        args = SimpleNamespace(demo=False, replay=None, calibration=1, bind='127.0.0.1',
            port=0, record=None, quit_after=0, screenshot=None)
        with tempfile.TemporaryDirectory() as directory:
            real_path = Path
            def local_path(value):
                path = real_path(value)
                return path if path.is_absolute() else real_path(directory) / path
            with patch('receiver.Path', side_effect=local_path):
                window = Window(args)
                seq = 0
                def feed(seconds, group=None):
                    nonlocal seq
                    bins = np.array_split(np.arange(245), 16)
                    for _ in range(round(seconds * 50)):
                        iq = np.zeros((245, 2), dtype=np.int8)
                        iq[:, 1] = 30
                        if group is not None:
                            value = round(5 * np.sin(2 * np.pi * 2 * seq / 50))
                            iq[bins[group], 1] += value
                            iq[bins[group + 1], 1] += value
                        window.consume(encode(sample(seq, timestamp_us=1000000 + seq * 20000,
                            data=iq.tobytes())), '192.0.2.11', time.time())
                        seq += 1
                status = dict(type='status', name='esp32c5-1', boot=55, channel=36,
                    target_hz=500, sent=0, captured=0, queue_drops=0, udp_errors=0, send_errors=0)
                try:
                    window.consume(json.dumps(status).encode(), '192.0.2.10', time.time())
                    feed(2)
                    self.assertEqual(window.capture_label.count(), 1)
                    self.assertEqual(window.capture_label.currentData(), 'still')
                    for index, group in enumerate((None, 0, None, 11)):
                        if index:
                            window.capture_mode.setCurrentIndex(1)
                        window.capture_label.setCurrentIndex(0 if group is None else 1)
                        window.capture_seconds.setValue(10)
                        delay = (0, 2, 4, 1)[index]
                        window.capture_delay.setValue(delay)
                        with patch('receiver.time.monotonic', return_value=window.last_seen):
                            window.start_motion_capture()
                        self.assertIsNotNone(window.motion.capture, window.motion.note)
                        self.assertAlmostEqual(window.motion.capture['start_t'] - window.last_sample.timestamp_us / 1e6,
                                               delay, places=2)
                        feed(15, group)
                        self.assertIsNone(window.motion.capture)
                    self.assertTrue(window.motion.ready, window.motion.note)
                    window.draw(time.monotonic())
                    self.assertEqual(window.motion_status.text(), '活動')
                    self.assertTrue(window.apply_thresholds_button.isEnabled())
                    self.assertAlmostEqual(window.motion_enter.value(), window.motion.enter, places=4)
                    self.assertAlmostEqual(window.motion_exit.value(), window.motion.exit, places=4)
                    original = window.motion.export()
                    window.motion_enter.setValue(.5)
                    window.motion_exit.setValue(.5)
                    window.apply_thresholds_button.click()
                    self.assertEqual(window.motion.export(), original)
                    self.assertIn('活動觸發 > 活動解除', window.threshold_feedback.text())
                    window.inactivity_alarm = True
                    window.motion_enter.setValue(.95)
                    window.motion_exit.setValue(.5)
                    window.apply_thresholds_button.click()
                    self.assertEqual((window.motion.enter, window.motion.exit), (.95, .5))
                    self.assertFalse(window.inactivity_alarm)
                    profile = window.current_motion_profile()
                    before = window.motion.export()
                    saved_path = real_path(directory) / 'tuned-model.json'
                    with patch('receiver.QtWidgets.QFileDialog.getSaveFileName', return_value=(str(saved_path), 'JSON')):
                        window.save_motion_profile()
                    self.assertEqual(read_profile(saved_path)['model'], before)
                    window.inactivity_seconds.setValue(7)
                    window.capture_mode.setCurrentIndex(1)
                    window.capture_label.setCurrentIndex(1)
                    window.start_motion_capture()
                    self.assertFalse(window.apply_thresholds_button.isEnabled())
                    feed(15, 6)
                    self.assertEqual(before, window.motion.export())
                    self.assertIn('誤判', window.motion.note)
                    raw_path = window.recorder.path
                    window.toggle_recording()
                    events = [json.loads(line) for line in raw_path.read_text(encoding='utf-8').splitlines()]
                    delays = [row['delay_seconds'] for row in events if row.get('action') == 'capture']
                    self.assertEqual(delays[:4], [0, 2, 4, 1])
                    changes = [row for row in events if row.get('action') == 'thresholds']
                    self.assertEqual([(row['enter'], row['exit']) for row in changes], [(.95, .5)])
                    files = list((real_path(directory) / 'captures').glob('motion-session-*.jsonl'))
                    self.assertEqual(len(files), 5)
                    headers = [json.loads(path.read_text(encoding='utf-8').splitlines()[0]) for path in files]
                    self.assertEqual(sum(row['training'] is False for row in headers), 4)
                    self.assertTrue(all(row['label'] in ('still', 'active') for row in headers))
                    window.restore_motion_profile(profile, window.last_sample)
                    self.assertEqual(before, window.motion.export())
                    feed(2)
                    window.draw(time.monotonic())
                    self.assertRegex(window.motion_status.text(), r'^靜止　\d+ 秒$')
                    # Missing packets cannot be treated as fresh stillness.
                    window.draw(time.monotonic() + 5)
                    self.assertEqual(window.motion_status.text(), '')
                    self.assertIn('未收到即時 CSI', window.motion_metrics.text())
                    window.consume(json.dumps(dict(status, target_hz=100)).encode(), '192.0.2.10', time.time())
                    self.assertIsNone(window.motion.enter)
                    self.assertIsNone(window.processor.baseline)
                    with self.assertRaises(ValueError):
                        window.restore_motion_profile(profile, window.last_sample)
                finally:
                    window.close()
                replay_args = SimpleNamespace(**dict(vars(args), replay=raw_path))
                replay = Window(replay_args)
                try:
                    deadline = time.monotonic() + 15
                    while not replay.source.finished or not replay.source.items.empty():
                        if time.monotonic() > deadline:
                            self.fail('Replay did not finish')
                        try:
                            replay.consume(*replay.source.items.get(timeout=.1))
                        except queue.Empty:
                            pass
                    self.assertIsNone(replay.source.error)
                    self.assertEqual(replay.invalid, 0, replay.error_text)
                    self.assertEqual(replay.motion.export(), before)
                    self.assertEqual(replay.capture_delay.value(), 1)
                    self.assertEqual(replay.inactivity_seconds.value(), 7)
                    self.assertEqual((replay.motion_enter.value(), replay.motion_exit.value()), (.95, .5))
                    self.assertFalse(replay.apply_thresholds_button.isEnabled())
                    replay.draw(time.monotonic())
                    self.assertEqual(replay.motion_status.text(), '')
                    self.assertIn('重播結束', replay.motion_metrics.text())
                finally:
                    replay.close()

    def test_threshold_event_before_first_recorded_sample_replays(self):
        args = SimpleNamespace(demo=False, replay=None, calibration=1, bind='127.0.0.1',
            port=0, record=None, quit_after=0, screenshot=None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'thresholds.jsonl'
            window = Window(args)
            try:
                self.assertFalse(window.apply_thresholds_button.isEnabled())
                for seq in range(202):
                    window.consume(encode(sample(seq)), '192.0.2.11', time.time())
                window.motion.restore(dict(schema=2, kind='binary_motion', groups=16, enter=2., exit=1.))
                window.sync_threshold_controls()
                window.start_recording(path)
                window.motion_enter.setValue(1.2345)
                window.motion_exit.setValue(.8765)
                window.apply_thresholds_button.click()
                window.consume(encode(sample(202)), '192.0.2.11', time.time())
                window.motion_enter.setValue(1.8)
                window.motion_exit.setValue(1.1)
                window.apply_thresholds_button.click()
                window.consume(encode(sample(203)), '192.0.2.11', time.time())
                expected = window.motion.export()
                window.toggle_recording()
                window.recalibrate()
                self.assertFalse(window.apply_thresholds_button.isEnabled())
            finally:
                window.close()
            replay = Window(SimpleNamespace(**dict(vars(args), replay=path)))
            try:
                deadline = time.monotonic() + 5
                while not replay.source.finished or not replay.source.items.empty():
                    self.assertLess(time.monotonic(), deadline)
                    try:
                        replay.consume(*replay.source.items.get(timeout=.1))
                    except queue.Empty:
                        pass
                self.assertIsNone(replay.source.error)
                self.assertEqual(replay.invalid, 0, replay.error_text)
                self.assertEqual(replay.motion.export(), expected)
                self.assertEqual((replay.motion_enter.value(), replay.motion_exit.value()), (1.8, 1.1))
                self.assertFalse(replay.apply_thresholds_button.isEnabled())
            finally:
                replay.close()


if __name__ == '__main__':
    unittest.main()

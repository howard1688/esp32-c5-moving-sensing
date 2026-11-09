import copy
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from motion import MotionDetector
from motion_store import make_profile, apply_profile, validate_profile, context_for
from test_host import sample
from processing import Processor


def pattern(index=None):
    values = np.full(245, .01)
    if index is None:
        values[:] = .1
    else:
        groups = np.array_split(np.arange(245), 16)
        values[groups[index]] = 2
        values[groups[(index + 1) % 16]] = 1
    return values


def frame(t, energy=.1, index=None, **extra):
    return dict(t=t, variation=energy, motion_std=pattern(index), motion_span=.5,
                motion_count=251, gain_ready=True, dt_ms=2, clipped=0, **extra)


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.d = MotionDetector()
        self.t = 1

    def feed(self, seconds=2, energy=.1, index=None):
        for _ in range(round(seconds * 10)):
            self.t += .1
            self.d.process(frame(self.t, energy, index))
        return self.d.result['state']

    def train(self, label, energy=.1, index=None):
        self.d.start_capture(label, 3, start_t=self.t + .5)
        self.feed(4, energy, index)
        self.assertTrue(self.d.completed['accepted'], self.d.completed['message'])

    def test_background_alone_enables_binary_motion_and_stop(self):
        self.train('still')
        self.assertTrue(self.d.ready)
        self.assertEqual(self.feed(2, 2, 0), 'active')
        self.assertEqual(self.feed(2, 2, 6), 'active')
        self.assertEqual(self.feed(2, .1), 'no_motion')

    def test_activity_is_independent_of_zone_shape(self):
        self.train('still')
        for index in (0, 6, 11, None):
            self.assertEqual(self.feed(2, 2, index), 'active')

    def test_hysteresis_uses_only_two_valid_motion_states(self):
        self.train('still')
        self.assertEqual(self.feed(.2, 2), 'no_motion')
        self.assertIsNotNone(self.d.enter_since)
        self.assertEqual(self.feed(.5, 2), 'active')
        self.assertEqual(self.feed(.8, .1), 'active')
        self.assertEqual(self.feed(.8, .1), 'no_motion')
        self.assertEqual(self.feed(10, .1), 'no_motion')  # Stillness never means vacancy.

    def test_validation_never_changes_model_and_reports_wrong_label(self):
        self.train('still')
        before = self.d.export()
        self.d.start_capture('still', 3, training=False, start_t=self.t + .5)
        self.feed(4, 2, 6)
        self.assertEqual(before, self.d.export())
        self.assertTrue(self.d.completed['accepted'])
        self.assertGreater(self.d.completed['counts'].get('active', 0), 0)
        self.assertIn('誤判', self.d.completed['message'])
        self.d.start_capture('active', 3, training=False, start_t=self.t + .5)
        self.feed(4, 2, 0)
        self.assertEqual(before, self.d.export())
        self.assertIn('誤判 0 窗', self.d.completed['message'])

    def test_background_contamination_and_activity_training_rejected(self):
        self.d.start_capture('still', 3)
        self.feed(1, .1)
        self.feed(.6, 3, 0)
        self.feed(2, .1)
        self.assertFalse(self.d.completed['accepted'])
        self.assertIsNone(self.d.enter)
        self.train('still')
        with self.assertRaises(ValueError):
            self.d.start_capture('active', 3)

    def test_legacy_models_and_zone_recordings_convert_to_binary(self):
        self.d.restore(dict(schema=1, groups=16, enter=1.2, exit=.8,
                            templates=dict(in_zone=[], left=[], right=[])))
        self.assertTrue(self.d.ready)
        self.assertEqual(self.d.export()['schema'], 2)
        self.assertNotIn('templates', self.d.export())
        self.d.start_capture('left', 3, training=True, start_t=self.t + .5)
        self.assertEqual(self.d.capture['label'], 'active')
        self.assertFalse(self.d.capture['training'])
        self.feed(4, 2, 6)
        self.assertTrue(self.d.completed['accepted'])
        self.assertEqual(self.feed(2, 2, 6), 'active')

    def test_short_window_rejected_but_sufficient_low_rate_window_accepted(self):
        self.train('still')
        self.t += .1
        row = frame(self.t)
        row.update(motion_span=.2, motion_count=100)
        self.assertEqual(self.d.process(row)[0]['state'], 'insufficient')
        for _ in range(30):
            self.t += .02
            row = frame(self.t, 2)
            row.update(motion_span=.5, motion_count=25, dt_ms=20)
            result, _ = self.d.process(row)
        self.assertEqual(result['state'], 'active')

    def test_gap_gain_and_saturation_abstain(self):
        self.train('still')
        self.assertEqual(self.feed(2, 2, 0), 'active')
        self.t += 1
        row = frame(self.t, 2, 0)
        row['dt_ms'] = 1000
        self.assertEqual(self.d.process(row)[0]['state'], 'insufficient')
        self.assertEqual(self.feed(2, 2, 0), 'active')
        for key, value in [('gain_ready', False), ('clipped', .02), ('transport_issue', '延遲')]:
            self.t += .1
            row = frame(self.t, 2, 0)
            row[key] = value
            self.assertEqual(self.d.process(row)[0]['state'], 'insufficient')

    def test_quality_failure_cannot_complete_training(self):
        self.d.start_capture('still', 3)
        self.feed(.5)
        for _ in range(40):
            self.t += .1
            row = frame(self.t)
            row['gain_ready'] = False
            self.d.process(row)
        self.assertIsNone(self.d.capture)
        self.assertFalse(self.d.completed['accepted'])

    def test_threshold_tuning_changes_trigger_and_release_and_preserves_quality(self):
        with self.assertRaises(ValueError):
            self.d.set_thresholds(1.2, .8)
        self.train('still')
        self.d.set_thresholds(3, 1)
        self.assertEqual(self.feed(2, 2), 'no_motion')
        self.d.set_thresholds(1.5, 1)
        self.assertEqual(self.feed(2, 2), 'active')
        self.assertEqual(self.feed(2, 1.2), 'active')
        self.d.set_thresholds(1.5, 1.3)
        self.assertTrue(self.d.active)  # No invented state change on clicking Apply.
        self.assertIsNone(self.d.exit_since)
        self.assertEqual(self.feed(2, 1.2), 'no_motion')
        self.t += .02
        bad = frame(self.t)
        bad['gain_ready'] = False
        self.d.process(bad)
        holdoff = self.d.bad_until
        self.d.set_thresholds(1.2, .8)
        self.assertEqual(self.d.bad_until, holdoff)
        self.assertEqual(self.feed(.2), 'insufficient')

    def test_invalid_or_mid_capture_thresholds_leave_model_unchanged(self):
        self.train('still')
        original = self.d.export()
        for enter, exit_ in [(1, 1), (.8, 1), (1, 0), (1, -1),
                             (float('nan'), .5), (float('inf'), .5), (True, .5), ('1', .5)]:
            with self.subTest(enter=enter, exit=exit_):
                with self.assertRaises(ValueError):
                    self.d.set_thresholds(enter, exit_)
                self.assertEqual(self.d.export(), original)
        self.d.start_capture('still', 3, training=False)
        with self.assertRaises(ValueError):
            self.d.set_thresholds(2, 1)
        self.assertEqual(self.d.export(), original)

    def test_profile_roundtrip_restores_baseline_and_rejects_changed_radio(self):
        self.train('still')
        processor = Processor(1)
        for i in range(202):
            processor.process(sample(i))
        context = context_for(sample(), False, 500, 55)
        profile = make_profile(processor, self.d, context, 'LIVE UDP')
        clone = MotionDetector()
        other = Processor()
        apply_profile(profile, other, clone, context)
        np.testing.assert_array_equal(other.baseline, processor.baseline)
        self.assertEqual(clone.export(), self.d.export())
        for field, value in [('boot', 88), ('channel', 40), ('target_hz', 200), ('tx_boot', 66)]:
            with self.assertRaises(ValueError):
                apply_profile(profile, other, clone, dict(context, **{field: value}))
        corrupt = copy.deepcopy(profile)
        corrupt['model']['enter'] = float('nan')
        with self.assertRaises(ValueError):
            validate_profile(corrupt)
        corrupt = copy.deepcopy(profile)
        corrupt['scale'][5] = 0
        with self.assertRaises(ValueError):
            validate_profile(corrupt)


if __name__ == '__main__':
    unittest.main()

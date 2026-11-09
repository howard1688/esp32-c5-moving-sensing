"""Background-calibrated binary motion detection with time-based hysteresis."""
import json
import uuid
import numpy as np

LABELS = {'still': '靜止／背景', 'active': '活動'}
LEGACY_LABELS = {'in_zone', 'left', 'right'}
STATES = {
    'no_motion': '靜止',
    'active': '活動',
    'untrained': '請先訓練背景',
    'insufficient': '訊號準備中，暫停更新判斷',
}
GROUPS = 16


def business_label(label):
    return 'no_motion' if label == 'still' else 'active'


class MotionDetector:
    def __init__(self):
        self.clear()

    def clear(self):
        self.enter = self.exit = None
        self.capture = None
        self.completed = None
        self.note = ''
        self.reset_stream()

    def reset_stream(self):
        self.last_t = self.last_feature_t = None
        self.bad_until = -np.inf
        self.quality_reason = '等待建立完整資料窗'
        self.bad_reason = ''
        self.active = False
        self.enter_since = self.exit_since = None
        self.result = dict(state='untrained' if self.enter is None else 'insufficient', score=None, margin=None)

    @property
    def ready(self):
        return self.enter is not None

    @staticmethod
    def validate_thresholds(enter, exit_):
        if not (type(enter) in (int, float) and type(exit_) in (int, float)
                and np.isfinite([enter, exit_]).all() and 0 < exit_ < enter):
            raise ValueError('門檻必須符合：活動觸發 > 活動解除 > 0')
        return float(enter), float(exit_)

    def set_thresholds(self, enter, exit_):
        if not self.ready:
            raise ValueError('請先訓練背景或載入模型，再微調門檻')
        if self.capture is not None:
            raise ValueError('訓練／驗證進行中，請完成或取消後再調整門檻')
        enter, exit_ = self.validate_thresholds(enter, exit_)
        self.enter, self.exit = enter, exit_
        # Reconfirm transitions with the new values. Preserve the current
        # binary state, sample clock and quality holdoff until fresh data.
        self.enter_since = self.exit_since = None
        self.last_feature_t = None

    def start_capture(self, label, seconds, training=True, start_t=None):
        # Old recordings remain replayable; old zone demonstrations become
        # binary activity validation and never train a spatial model.
        if label in LEGACY_LABELS:
            label, training = 'active', False
        if label not in LABELS or not np.isfinite(seconds) or not 2 <= seconds <= 120:
            raise ValueError('訓練類別或秒數不合法')
        if self.capture:
            raise ValueError('請先完成或取消目前區段')
        if training and label != 'still':
            raise ValueError('只需訓練靜止背景；活動請使用獨立驗證')
        if label != 'still' and self.enter is None:
            raise ValueError('請先完成背景訓練')
        if not training and not self.ready:
            raise ValueError('請先完成靜止背景訓練，再進行驗證')
        self.capture = dict(id=uuid.uuid4().hex, label=label, seconds=float(seconds),
            training=bool(training), start_t=start_t, begun=None, rows=[], rejected=0)
        self.completed = None
        self.note = ''

    def cancel_capture(self, reason='已取消區段'):
        if self.capture:
            self.completed = dict(id=self.capture['id'], label=self.capture['label'],
                                  training=self.capture['training'], accepted=False, message=reason,
                                  rows=self.capture['rows'], counts={})
        self.capture = None
        self.note = reason

    def progress_text(self):
        c = self.capture
        if c is None:
            return self.note
        mode = '訓練' if c['training'] else '驗證'
        if c['begun'] is None:
            if c['start_t'] is not None and self.last_t is not None and c['start_t'] > self.last_t:
                return f'{mode} {LABELS[c["label"]]}：準備 {c["start_t"] - self.last_t:.0f} 秒'
            return f'{mode} {LABELS[c["label"]]}：等待校正完成與完整資料窗'
        remaining = max(0, c['seconds'] - (self.last_t - c['begun']))
        return f'{mode} {LABELS[c["label"]]}：剩 {remaining:.0f} 秒，已收 {len(c["rows"])} 個有效窗'

    @staticmethod
    def extract(row):
        std = np.asarray(row['motion_std'], dtype=float)
        if std.ndim != 1 or len(std) < GROUPS or np.mean(np.isfinite(std)) < .8:
            return None
        vector = np.array([np.mean(group[np.isfinite(group)]) if np.isfinite(group).any() else np.nan
                           for group in np.array_split(std, GROUPS)])
        if not np.isfinite(vector).all() or not np.isfinite(row['variation']):
            return None
        norm = np.linalg.norm(vector)
        group_std = vector.tolist()
        vector = vector / norm if norm > 1e-9 else np.zeros(GROUPS)
        rssi = row.get('rssi')
        amplitude = row.get('corrected')
        return dict(t=float(row['t']), energy=float(row['variation']), vector=vector.tolist(),
                    group_std=group_std,
                    rssi=float(rssi) if rssi is not None and np.isfinite(rssi) else None,
                    amplitude=float(amplitude) if amplitude is not None and np.isfinite(amplitude) else None)

    def process(self, row):
        """Called for every packet; feature work runs at most 10 Hz."""
        t = float(row['t'])
        dt = row.get('dt_ms', np.nan)
        if self.last_t is not None and t <= self.last_t:
            raise ValueError('移動判斷時間倒退')
        if self.last_t is not None and t - self.last_t > .25:
            self.reset_stream()
        self.last_t = t
        # Keep a bad packet gap/saturation from being missed by the 10 Hz decimator.
        issue = row.get('transport_issue', '')
        if not issue and not row.get('gain_ready', False):
            issue = '增益補償尚未就緒'
        if not issue and row.get('clipped', 0) > .01:
            issue = '訊號截幅，等待恢復'
        if not issue and np.isfinite(dt) and dt > 50:
            issue = '取樣間隔過長，重建資料窗'
        if issue:
            self.bad_until = t + .5
            self.bad_reason = issue
        quality = (t >= self.bad_until and row.get('motion_span', 0) >= .4
                   and row.get('motion_count', 0) >= 10)
        if not quality:
            self.active = False
            self.enter_since = self.exit_since = None
            self.quality_reason = self.bad_reason if t < self.bad_until else '等待建立完整資料窗'
            self.result = dict(state='insufficient', score=None, margin=None)
            if self.capture:
                self.capture['rejected'] += 1
                if self.capture['begun'] is not None and t - self.capture['begun'] >= self.capture['seconds']:
                    self.finish_capture()
            return self.result, None
        if self.last_feature_t is not None and t - self.last_feature_t < .099:
            return self.result, None
        self.last_feature_t = t
        feature = self.extract(row)
        if feature is None:
            self.quality_reason = '有效子載波尚未就緒'
            self.result = dict(state='insufficient', score=None, margin=None)
            if self.capture and self.capture['begun'] is not None and t - self.capture['begun'] >= self.capture['seconds']:
                self.finish_capture()
            return self.result, None
        self.quality_reason = ''
        self.result = self.classify(feature)
        self.collect(feature)
        return self.result, feature

    def classify(self, feature):
        t, energy = feature['t'], feature['energy']
        if self.enter is None:
            return dict(state='untrained', score=energy, margin=None)
        if not self.active:
            if energy > self.enter:
                if self.enter_since is None:
                    self.enter_since = t
                if t - self.enter_since >= .3:
                    self.active = True
            else:
                self.enter_since = None
            if not self.active:
                return dict(state='no_motion', score=energy, margin=None)
        if energy < self.exit:
            if self.exit_since is None:
                self.exit_since = t
            if t - self.exit_since >= 1.2:
                self.active = False
                self.enter_since = None
                return dict(state='no_motion', score=energy, margin=None)
            return dict(state='active', score=energy, margin=None)
        self.exit_since = None
        return dict(state='active', score=energy, margin=None)

    def collect(self, feature):
        c = self.capture
        if c is None:
            return
        t = feature['t']
        if c['start_t'] is not None and t < c['start_t']:
            return
        if c['begun'] is None:
            c['begun'] = t
        if t - c['begun'] >= c['seconds']:
            self.finish_capture()
            return
        c['rows'].append(dict(feature, prediction=self.result['state'], margin=self.result['margin']))

    def finish_capture(self):
        c = self.capture
        self.capture = None
        rows = c['rows']
        counts = {}
        for row in rows:
            counts[row['prediction']] = counts.get(row['prediction'], 0) + 1
        accepted = False
        message = '有效資料不足，請檢查收樣率、缺口或訊號飽和後重做'
        enough = (len(rows) >= 12 and rows[-1]['t'] - rows[0]['t'] >= c['seconds'] * .6
                  and len(rows) >= c['seconds'] * 6)
        if enough and not c['training']:
            accepted = True
            expected = business_label(c['label'])
            correct = counts.get(expected, 0)
            unknown = counts.get('unknown', 0) + counts.get('insufficient', 0) + counts.get('untrained', 0)
            message = f'驗證完成：符合 {correct}/{len(rows)} 窗，拒判 {unknown} 窗，誤判 {len(rows) - correct - unknown} 窗（重疊時間窗，非獨立準確率）'
        elif enough:
            energy = np.array([r['energy'] for r in rows])
            if c['label'] == 'still':
                median = float(np.median(energy))
                mad = max(.01, float(1.4826 * np.median(np.abs(energy - median))))
                if np.mean(energy > median + max(.2, 6 * mad)) > .05:
                    message = '背景出現明顯變動，請保持靜止並重做背景訓練'
                else:
                    self.exit = max(.1, float(np.quantile(energy, .95)) + mad)
                    self.enter = max(self.exit * 1.2 + .05, float(np.quantile(energy, .99)) + 3 * mad)
                    accepted = True
                    message = f'背景完成：活動門檻 {self.enter:.2f}、靜止門檻 {self.exit:.2f}；已可判斷活動／靜止，請進行獨立驗證'
        self.note = message
        self.completed = dict(id=c['id'], label=c['label'], training=c['training'],
                              accepted=accepted, message=message, rows=rows, counts=counts)
        self.reset_stream()

    def export(self):
        return json.loads(json.dumps(dict(schema=2, kind='binary_motion', groups=GROUPS,
                                          enter=self.enter, exit=self.exit), allow_nan=False))

    def restore(self, payload):
        # JSON roundtrip also prevents a caller mutating the active profile.
        obj = json.loads(json.dumps(payload, allow_nan=False))
        if obj.get('schema') not in (1, 2) or obj.get('groups') != GROUPS:
            raise ValueError('不支援的移動模型版本')
        if obj['schema'] == 2 and obj.get('kind') != 'binary_motion':
            raise ValueError('不支援的移動模型種類')
        enter, exit_ = self.validate_thresholds(obj.get('enter'), obj.get('exit'))
        self.clear()
        self.enter, self.exit = float(enter), float(exit_)
        self.reset_stream()
        self.note = '已載入活動／靜止模型；請以相同擺位進行獨立驗證'

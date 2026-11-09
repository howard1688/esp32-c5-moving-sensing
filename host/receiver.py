"""Run from repository root: .venv/Scripts/python.exe host/receiver.py --demo"""
import argparse
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import queue
import sys
import time
import numpy as np
from PyQt5 import QtCore, QtWidgets, QtGui
import pyqtgraph as pg
from processing import Processor
from protocol import decode, SequenceTracker
from stream import Recorder, Source
from motion import MotionDetector, LABELS, STATES
from motion_store import context_for, make_profile, apply_profile, read_profile, write_json, validate_profile

TRAINING_PROMPT = '請先進行訓練或載入已訓練之模型'


class Window(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.mode = 'DEMO / synthetic data' if args.demo else ('REPLAY' if args.replay else 'LIVE UDP')
        self.mode_label = '模擬資料' if args.demo else ('重播' if args.replay else '即時接收')
        self.setWindowTitle(f'CSI 訊號觀察｜{self.mode_label}')
        self.resize(1440, 940)
        self.processor = Processor(args.calibration)
        self.motion = MotionDetector()
        self.motion_target_hz = None
        self.motion_bound_context = None
        self.pending_motion_profile = None
        self.capture_context = None
        self.capture_wall_started = None
        self.capture_wait_seconds = 0
        self.last_motion_drop_count = 0
        self.reset_inactivity()
        self.tracker = SequenceTracker()
        self.history = deque(maxlen=12000)
        self.heat_history = deque(maxlen=600)
        self.last_heat_t = -1e9
        self.arrivals = deque()
        self.arrival_start = None
        self.nodes = {}
        self.node_history = {}
        self.last_sample = None
        self.last_seen = None
        self.invalid = 0
        self.error_text = ''
        self.pending_rate = None
        self.rate_message = ''
        self.rate_user_selected = False
        self.recorder = None
        self.records = 0
        self.markers = []
        self.origin = None
        self.last_draw = 0
        self.replay_markers = []
        self.source = Source(args.bind, args.port, args.demo, args.replay)
        self.build_ui()
        if args.record:
            self.start_recording(args.record)
        self.source.start()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(25)
        if args.quit_after:
            QtCore.QTimer.singleShot(int(args.quit_after * 1000), self.finish_test)

    def build_ui(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        self.setCentralWidget(widget)
        self.banner = QtWidgets.QLabel(f'5 GHz HE20 SU｜{self.mode_label}')
        layout.addWidget(self.banner)
        controls = QtWidgets.QHBoxLayout()
        recalibrate = QtWidgets.QPushButton('靜止校正')
        recalibrate.clicked.connect(self.recalibrate)
        controls.addWidget(recalibrate)
        self.shape = QtWidgets.QCheckBox('振幅正規化')
        self.shape.setToolTip('比較各子載波的相對振幅；切換後重新校正。')
        self.shape.toggled.connect(self.change_shape)
        controls.addWidget(self.shape)
        controls.addWidget(QtWidgets.QLabel('發送率'))
        self.rate = QtWidgets.QComboBox()
        self.rate.addItems(['50', '100', '200', '250', '500', '1000'])
        self.rate.setCurrentText('500')
        self.rate.setMaximumWidth(80)
        self.rate.setEnabled(not self.args.demo and not self.args.replay)
        self.rate.activated.connect(self.rate_selected)
        controls.addWidget(self.rate)
        controls.addWidget(QtWidgets.QLabel('Hz'))
        controls.addWidget(QtWidgets.QLabel('Kalman Q'))
        self.kalman_q = QtWidgets.QDoubleSpinBox()
        self.kalman_q.setRange(.001, 100000)
        self.kalman_q.setDecimals(3)
        self.kalman_q.setValue(self.processor.kalman_q)
        self.kalman_q.setToolTip('程序雜訊：越大越快跟隨變化')
        self.kalman_q.setMaximumWidth(95)
        controls.addWidget(self.kalman_q)
        controls.addWidget(QtWidgets.QLabel('R'))
        self.kalman_r = QtWidgets.QDoubleSpinBox()
        self.kalman_r.setRange(.001, 100000)
        self.kalman_r.setDecimals(3)
        self.kalman_r.setValue(self.processor.kalman_r)
        self.kalman_r.setToolTip('量測雜訊：越大曲線越平滑')
        self.kalman_r.setMaximumWidth(95)
        controls.addWidget(self.kalman_r)
        self.kalman_q.valueChanged.connect(self.change_kalman)
        self.kalman_r.valueChanged.connect(self.change_kalman)
        self.remember_rate = QtWidgets.QCheckBox('記住')
        self.remember_rate.setToolTip('設為板子下次開機的發送率。')
        self.remember_rate.setEnabled(not self.args.demo and not self.args.replay)
        controls.addWidget(self.remember_rate)
        self.apply_rate_button = QtWidgets.QPushButton('套用')
        self.apply_rate_button.setEnabled(not self.args.demo and not self.args.replay)
        self.apply_rate_button.clicked.connect(self.apply_rate)
        controls.addWidget(self.apply_rate_button)
        self.record_button = QtWidgets.QPushButton('開始錄製')
        self.record_button.clicked.connect(self.toggle_recording)
        self.record_button.setEnabled(not self.args.replay)
        controls.addWidget(self.record_button)
        self.marker_text = QtWidgets.QLineEdit('揮手')
        self.marker_text.setMaximumWidth(160)
        controls.addWidget(self.marker_text)
        marker = QtWidgets.QPushButton('標記動作')
        marker.clicked.connect(self.mark)
        controls.addWidget(marker)
        diagnostics = QtWidgets.QCheckBox('詳細資訊')
        controls.addWidget(diagnostics)
        layout.addLayout(controls)
        training = QtWidgets.QHBoxLayout()
        training.addWidget(QtWidgets.QLabel('移動辨識'))
        self.capture_mode = QtWidgets.QComboBox()
        self.capture_mode.addItems(['訓練', '獨立驗證（不更新模型）'])
        self.capture_mode.currentIndexChanged.connect(self.capture_mode_changed)
        training.addWidget(self.capture_mode)
        self.capture_label = QtWidgets.QComboBox()
        self.capture_label.addItem(LABELS['still'], 'still')
        self.capture_label.activated.connect(self.capture_label_changed)
        training.addWidget(self.capture_label)
        training.addWidget(QtWidgets.QLabel('持續'))
        self.capture_seconds = QtWidgets.QSpinBox()
        self.capture_seconds.setRange(10, 120)
        self.capture_seconds.setValue(30)
        self.capture_seconds.setSuffix(' 秒')
        self.capture_seconds.setMaximumWidth(80)
        training.addWidget(self.capture_seconds)
        training.addWidget(QtWidgets.QLabel('開始倒數'))
        self.capture_delay = QtWidgets.QSpinBox()
        self.capture_delay.setRange(0, 300)
        self.capture_delay.setValue(3)
        self.capture_delay.setSuffix(' 秒')
        self.capture_delay.setMaximumWidth(80)
        self.capture_delay.setToolTip('按開始後等候的秒數；0 表示不額外等待。背景仍需完成振幅校正。')
        training.addWidget(self.capture_delay)
        self.capture_button = QtWidgets.QPushButton('開始區段')
        self.capture_button.clicked.connect(self.start_motion_capture)
        training.addWidget(self.capture_button)
        self.cancel_capture_button = QtWidgets.QPushButton('取消區段')
        self.cancel_capture_button.clicked.connect(self.cancel_motion_capture)
        training.addWidget(self.cancel_capture_button)
        self.keep_raw = QtWidgets.QCheckBox('保留原始 CSI')
        self.keep_raw.setChecked(True)
        self.keep_raw.setToolTip('訓練／驗證時自動開始錄製，方便日後增加特徵；結束後可按停止錄製。')
        training.addWidget(self.keep_raw)
        save_model = QtWidgets.QPushButton('儲存模型')
        save_model.clicked.connect(self.save_motion_profile)
        training.addWidget(save_model)
        load_model = QtWidgets.QPushButton('載入模型（相同擺位）')
        load_model.clicked.connect(self.load_motion_profile)
        training.addWidget(load_model)
        for control in (self.capture_button, self.cancel_capture_button, load_model):
            control.setEnabled(not self.args.replay)
        layout.addLayout(training)
        threshold_controls = QtWidgets.QHBoxLayout()
        threshold_controls.addWidget(QtWidgets.QLabel('判斷門檻'))
        threshold_controls.addWidget(QtWidgets.QLabel('活動觸發'))
        self.motion_enter = QtWidgets.QDoubleSpinBox()
        threshold_controls.addWidget(self.motion_enter)
        threshold_controls.addWidget(QtWidgets.QLabel('活動解除（回靜止）'))
        self.motion_exit = QtWidgets.QDoubleSpinBox()
        threshold_controls.addWidget(self.motion_exit)
        for control in (self.motion_enter, self.motion_exit):
            control.setDecimals(4)
            control.setRange(.0001, 1e9)
            control.setSingleStep(.01)
            control.setMaximumWidth(135)
            control.setKeyboardTracking(False)
            control.valueChanged.connect(self.thresholds_edited)
        self.motion_enter.setToolTip('分數高於此值並持續約 0.3 秒時觸發活動。調低更敏感，調高較不易誤觸發。')
        self.motion_exit.setToolTip('活動時，分數低於此值並持續約 1.2 秒後回到靜止。調高較容易解除，必須低於觸發門檻。')
        self.apply_thresholds_button = QtWidgets.QPushButton('套用門檻')
        self.apply_thresholds_button.clicked.connect(self.apply_motion_thresholds)
        threshold_controls.addWidget(self.apply_thresholds_button)
        threshold_controls.addWidget(QtWidgets.QLabel('微調步進 0.01；可直接輸入數值'))
        threshold_controls.addStretch(1)
        layout.addLayout(threshold_controls)
        self.threshold_feedback = QtWidgets.QLabel()
        self.threshold_feedback.setWordWrap(True)
        layout.addWidget(self.threshold_feedback)
        self.sync_threshold_controls()
        alarm_controls = QtWidgets.QHBoxLayout()
        alarm_controls.addWidget(QtWidgets.QLabel('未移動警報'))
        self.inactivity_seconds = QtWidgets.QSpinBox()
        self.inactivity_seconds.setRange(0, 86400)
        self.inactivity_seconds.setValue(60)
        self.inactivity_seconds.setSuffix(' 秒')
        self.inactivity_seconds.setSpecialValueText('關閉')
        self.inactivity_seconds.setMaximumWidth(130)
        self.inactivity_seconds.setToolTip('持續有效靜止達設定秒數時閃爍紅底；0 關閉。修改設定後重新計時。')
        self.inactivity_seconds.valueChanged.connect(self.change_inactivity_seconds)
        alarm_controls.addWidget(self.inactivity_seconds)
        alarm_controls.addWidget(QtWidgets.QLabel('持續靜止超時後警報；0 秒關閉，修改秒數後重新計時'))
        alarm_controls.addStretch(1)
        layout.addLayout(alarm_controls)
        presence_note = QtWidgets.QLabel('靜止表示未偵測到移動，可能有人靜坐或站立；本版不判定有人／無人。')
        presence_note.setWordWrap(True)
        layout.addWidget(presence_note)
        self.motion_status = QtWidgets.QLabel(TRAINING_PROMPT)
        self.motion_status.setAlignment(QtCore.Qt.AlignCenter)
        self.motion_status.setWordWrap(True)
        self.motion_status.setMinimumHeight(110)
        self.motion_display_state = None
        self.motion_status.setStyleSheet('font-size: 56px; font-weight: 700; color: #ffffff; padding: 12px; background: #22334a; border-radius: 8px;')
        layout.addWidget(self.motion_status)
        self.motion_metrics = QtWidgets.QLabel('完成靜止背景訓練後，即可判斷活動／靜止')
        self.motion_metrics.setWordWrap(True)
        layout.addWidget(self.motion_metrics)
        self.motion_training_status = QtWidgets.QLabel('固定兩板與天線。先訓練靜止背景，再分別驗證靜止與活動。')
        self.motion_training_status.setWordWrap(True)
        layout.addWidget(self.motion_training_status)
        self.status = QtWidgets.QLabel('等待資料')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.devices = QtWidgets.QLabel('esp32c5-1：等待連線　｜　esp32c5-2：等待連線')
        self.devices.setWordWrap(True)
        layout.addWidget(self.devices)
        self.details = QtWidgets.QLabel()
        self.details.setWordWrap(True)
        self.details.hide()
        diagnostics.toggled.connect(self.details.setVisible)
        layout.addWidget(self.details)
        self.graph = pg.GraphicsLayoutWidget()
        self.graph.ci.layout.setColumnStretchFactor(0, 1)
        self.graph.ci.layout.setColumnStretchFactor(1, 1)
        layout.addWidget(self.graph, 1)
        self.heat_plot = self.graph.addPlot(row=0, col=0, colspan=2,
            title='CSI 熱圖')
        self.heat_plot.setLabel('left', '子載波索引')
        self.heat_plot.setLabel('bottom', '時間（秒）')
        self.heat = pg.ImageItem(axisOrder='row-major')
        self.heat.setLookupTable(pg.colormap.get('CET-D1').getLookupTable())
        self.heat_plot.addItem(self.heat)
        self.amp_plot = self.graph.addPlot(row=1, col=0, title='CSI 振幅')
        self.amp_plot.setLabel('left', '平均振幅')
        self.amp_plot.addLegend(offset=(10, 10), colCount=4)
        self.amp_curves = {
            'raw': self.amp_plot.plot(pen=pg.mkPen('#778399'), name='原始'),
            'corrected': self.amp_plot.plot(pen=pg.mkPen('#67e8d0'), name='增益補償'),
            'smooth': self.amp_plot.plot(pen=pg.mkPen('#f6bd60', width=2), name='EMA 50 ms'),
            'kalman': self.amp_plot.plot(pen=pg.mkPen('#ff78c6', width=2), name='Kalman')}
        self.rssi_plot = self.graph.addPlot(row=1, col=1, title='訊號強度')
        self.rssi_plot.setLabel('left', 'RSSI', units='dBm')
        self.rssi_curve = self.rssi_plot.plot(pen=pg.mkPen('#a6b7ff', width=2))
        self.activity_plot = self.graph.addPlot(row=2, col=0, title='動作變化')
        self.activity_plot.setLabel('left', '相對分數')
        self.activity_plot.addLegend(offset=(10, 10), colCount=3)
        self.activity_curves = {
            'deviation': self.activity_plot.plot(pen='#67e8d0', name='基線偏差'),
            'variation': self.activity_plot.plot(pen='#f6bd60', name='短時變異'),
            'difference': self.activity_plot.plot(pen='#b39ddb', name='相鄰差分')}
        self.timing_plot = self.graph.addPlot(row=2, col=1, title='取樣間隔')
        self.timing_plot.setLabel('left', '間隔（毫秒）')
        self.timing_curve = self.timing_plot.plot(pen='#ff8f8f')
        for plot in (self.amp_plot, self.rssi_plot, self.activity_plot, self.timing_plot):
            plot.setLabel('bottom', '時間（秒）')
            plot.showGrid(x=True, y=True, alpha=.15)
        self.footer = QtWidgets.QLabel('')
        self.footer.setWordWrap(True)
        layout.addWidget(self.footer)

    def reset_inactivity(self):
        self.inactivity_since = None
        self.inactivity_last_t = None
        self.inactivity_elapsed = 0.0
        self.inactivity_alarm = False
        self.inactivity_alarm_started = None

    def update_threshold_controls(self):
        enabled = self.motion.ready and self.motion.capture is None and not self.args.replay
        for control in (self.motion_enter, self.motion_exit, self.apply_thresholds_button):
            control.setEnabled(enabled)

    def sync_threshold_controls(self):
        for control, value in ((self.motion_enter, self.motion.enter), (self.motion_exit, self.motion.exit)):
            control.blockSignals(True)
            control.setValue(value if value is not None else control.minimum())
            control.blockSignals(False)
        if self.motion.ready:
            prefix = '重播門檻' if self.args.replay else '已套用門檻'
            self.threshold_feedback.setText(f'{prefix}：觸發 {self.motion.enter:.4f}／解除 {self.motion.exit:.4f}')
        else:
            self.threshold_feedback.setText('請先訓練背景或載入模型，再微調觸發／解除門檻')
        self.update_threshold_controls()

    def thresholds_edited(self):
        self.threshold_feedback.setText('尚未套用；請按「套用門檻」。需符合：活動觸發 > 活動解除 > 0')

    def set_motion_thresholds(self, enter, exit_):
        self.motion.set_thresholds(enter, exit_)
        self.reset_inactivity()
        self.sync_threshold_controls()

    def apply_motion_thresholds(self):
        if self.args.replay:
            return
        try:
            self.set_motion_thresholds(self.motion_enter.value(), self.motion_exit.value())
        except ValueError as exc:
            self.threshold_feedback.setText(str(exc))
            return
        self.record_motion_event('thresholds', enter=self.motion.enter, exit=self.motion.exit)

    def change_inactivity_seconds(self, seconds):
        self.reset_inactivity()
        self.record_motion_event('inactivity_setting', seconds=int(seconds))

    def update_inactivity(self, t):
        """Use received sample time, so missing data cannot trigger an alarm."""
        training = self.motion.capture is not None and self.motion.capture['training']
        if (self.motion.enter is None or training
                or self.motion.result['state'] != 'no_motion' or self.motion.enter_since is not None):
            self.reset_inactivity()
            return
        if self.inactivity_last_t is None or not 0 < t - self.inactivity_last_t <= .25:
            self.inactivity_since = t
            self.inactivity_alarm = False
            self.inactivity_alarm_started = None
        self.inactivity_last_t = t
        self.inactivity_elapsed = max(0.0, t - self.inactivity_since)
        if (self.inactivity_seconds.value() and self.inactivity_elapsed >= self.inactivity_seconds.value()
                and not self.inactivity_alarm):
            self.inactivity_alarm = True
            self.inactivity_alarm_started = time.monotonic()

    def recalibrate(self):
        self.reset_motion('振幅基線已重設，請重新訓練移動辨識')
        self.processor.reset()
        self.clear_history()
        if self.recorder:
            self.recorder.marker('CALIBRATE')

    def capture_label_changed(self):
        self.capture_seconds.setValue(30)

    def capture_mode_changed(self):
        previous = self.capture_label.currentData()
        self.capture_label.clear()
        self.capture_label.addItem(LABELS['still'], 'still')
        if self.capture_mode.currentIndex() == 1:
            self.capture_label.addItem(LABELS['active'], 'active')
        index = self.capture_label.findData(previous)
        self.capture_label.setCurrentIndex(max(0, index))

    def motion_context(self, sample=None):
        sample = self.last_sample if sample is None else sample
        tx = self.nodes.get('esp32c5-1')
        return context_for(sample, self.shape.isChecked(), self.motion_target_hz,
                           tx[0].get('boot') if tx else None)

    def current_motion_profile(self):
        return make_profile(self.processor, self.motion, self.motion_context(), self.mode)

    def record_motion_event(self, action, **fields):
        if self.recorder:
            try:
                self.recorder.write(dict(kind='motion_event', received=time.time(), action=action, **fields))
                self.recorder.file.flush()
            except OSError as exc:
                self.error_text = f'錄製中斷：{exc}'
                try:
                    self.recorder.close()
                except OSError:
                    pass
                self.recorder = None
                self.record_button.setText('開始錄製')

    def start_motion_capture(self):
        if self.motion.capture:
            self.motion.note = '請先完成或取消目前區段'
            return
        if self.last_seen is None or time.monotonic() - self.last_seen > 1:
            self.motion.note = '請先確認持續收到即時 CSI'
            return
        if not self.last_sample.flags & 2:
            self.motion.note = '增益補償準備中，請稍後開始'
            return
        label = self.capture_label.currentData()
        training = self.capture_mode.currentIndex() == 0
        if (label != 'still' and self.motion.enter is None) or (not training and not self.motion.ready):
            self.motion.note = '請先完成靜止背景訓練；驗證不會更新模型'
            return
        if training and label == 'still':
            self.recalibrate()
        if self.keep_raw.isChecked() and self.recorder is None:
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            self.start_recording(Path('captures') / f'motion-raw-{stamp}.jsonl')
            if self.recorder is None:
                return
        delay = self.capture_delay.value()
        start_t = self.last_sample.timestamp_us / 1e6 + max(0, time.monotonic() - self.last_seen) + delay
        try:
            self.motion.start_capture(label, self.capture_seconds.value(), training, start_t)
            self.capture_context = self.motion_context()
            self.capture_wall_started = time.monotonic()
            self.capture_wait_seconds = delay
            self.record_motion_event('capture', label=label, seconds=self.capture_seconds.value(),
                                     training=training, start_t=start_t, delay_seconds=delay)
            self.update_threshold_controls()
        except (ValueError, OSError) as exc:
            self.motion.note = str(exc)

    def cancel_motion_capture(self, reason='已取消區段'):
        # clicked(bool) supplies a bool instead of a reason.
        reason = reason if isinstance(reason, str) else '已取消區段'
        self.motion.cancel_capture(reason)
        self.record_motion_event('cancel', reason=reason)
        self.finish_motion_capture()

    def reset_motion(self, reason):
        self.reset_inactivity()
        if self.motion.capture:
            self.cancel_motion_capture(reason)
        self.motion.clear()
        self.motion.note = reason
        self.motion_bound_context = None
        self.pending_motion_profile = None
        self.sync_threshold_controls()

    def finish_motion_capture(self):
        completed = self.motion.completed
        if completed is None:
            return
        self.motion.completed = None
        self.capture_wall_started = None
        self.sync_threshold_controls()
        if self.args.replay:
            return
        try:
            path = Path('captures') / f'motion-session-{completed["id"]}.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as file:
                header = dict(kind='motion_session', schema=1, mode=self.mode,
                    context=self.capture_context, label=completed['label'], training=completed['training'],
                    accepted=completed['accepted'], message=completed['message'], counts=completed['counts'],
                    session_id=completed['id'], feature_version=1,
                    thresholds=dict(enter=self.motion.enter, exit=self.motion.exit),
                    raw_file=str(self.recorder.path) if self.recorder else None)
                file.write(json.dumps(header, ensure_ascii=False, allow_nan=False) + '\n')
                for row in completed['rows']:
                    file.write(json.dumps(dict(kind='feature', **row), allow_nan=False) + '\n')
            self.footer.setText(f'區段資料已保存：{path.name}')
            self.footer.setToolTip(str(path.resolve()))
            if completed['training'] and completed['accepted']:
                profile = self.current_motion_profile()
                self.motion_bound_context = profile['context']
                model_path = Path('captures') / f'motion-profile-{completed["id"]}.json'
                write_json(model_path, profile, exclusive=True)
                self.record_motion_event('profile', profile=profile)
                self.motion.note += f'｜模型已保存：{model_path.name}'
        except (ValueError, OSError) as exc:
            self.motion.note += f'｜儲存失敗：{exc}'

    def save_motion_profile(self):
        try:
            profile = self.current_motion_profile()
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '儲存移動模型', 'captures/motion-model.json', 'JSON (*.json)')
            if path:
                write_json(path, profile)
                self.motion.note = f'模型已保存：{Path(path).name}'
        except (ValueError, OSError) as exc:
            self.motion.note = str(exc)

    def restore_motion_profile(self, profile, sample):
        context = apply_profile(profile, self.processor, self.motion, self.motion_context(sample))
        self.reset_inactivity()
        self.processor.signature = sample.signature
        self.motion_bound_context = context
        self.shape.blockSignals(True)
        self.shape.setChecked(context['shape'])
        self.shape.blockSignals(False)
        self.sync_threshold_controls()
        self.clear_history()

    def load_motion_profile(self):
        if self.motion.capture:
            self.motion.note = '請先完成或取消目前區段'
            return
        if self.last_sample is None or self.last_seen is None or time.monotonic() - self.last_seen > 1:
            self.motion.note = '請先連接兩端；載入模型需要核對設備'
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '載入模型：兩板、天線與環境須相同', 'captures', 'JSON (*.json)')
        if not path:
            return
        try:
            profile = read_profile(path)
            if profile['mode'] != self.mode:
                raise ValueError('模擬與實測模型不能混用')
            self.restore_motion_profile(profile, self.last_sample)
            self.record_motion_event('profile', profile=profile)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self.motion.note = f'載入失敗：{exc}'

    def apply_rate(self):
        node = self.nodes.get('esp32c5-1')
        if not node or time.monotonic() - node[1] >= 3:
            self.rate_message = 'esp32c5-1 尚未連線'
            return
        status, _, address = node
        try:
            hz = int(self.rate.currentText())
            port = status.get('control_port', 5501)
            token = self.source.set_rate(address, hz, self.remember_rate.isChecked(), port)
            self.pending_rate = (token, hz, self.remember_rate.isChecked(), time.monotonic())
            self.rate_message = f'正在設定 {hz} Hz'
        except (ValueError, RuntimeError, queue.Full, OSError) as exc:
            self.pending_rate = None
            self.rate_message = f'設定失敗：{exc}'

    def rate_selected(self):
        self.rate_user_selected = True
        self.rate_message = ''

    def clear_history(self):
        self.history.clear()
        self.heat_history.clear()
        self.last_heat_t = -1e9
        self.origin = None
        self.heat.clear()
        for plot, line in self.markers:
            plot.removeItem(line)
        self.markers.clear()

    def change_shape(self, enabled):
        self.processor.normalize_shape = enabled
        if self.recorder:
            self.recorder.write(dict(kind='settings', received=time.time(),
                shape_normalization=enabled, calibration_seconds=self.args.calibration,
                kalman_q=self.processor.kalman_q, kalman_r=self.processor.kalman_r))
        self.recalibrate()

    def change_kalman(self):
        self.processor.set_kalman_params(self.kalman_q.value(), self.kalman_r.value())
        self.clear_history()
        if self.recorder:
            self.recorder.write(dict(kind='settings', received=time.time(),
                shape_normalization=self.shape.isChecked(),
                calibration_seconds=self.args.calibration,
                kalman_q=self.processor.kalman_q, kalman_r=self.processor.kalman_r))

    def start_recording(self, path):
        try:
            self.recorder = Recorder(path, self.mode)
            profile = self.current_motion_profile() if self.motion.enter is not None and self.processor.baseline is not None else None
            self.recorder.write(dict(kind='settings', shape_normalization=self.shape.isChecked(),
                                     calibration_seconds=self.args.calibration,
                                     kalman_q=self.processor.kalman_q,
                                     kalman_r=self.processor.kalman_r,
                                     motion_profile=profile,
                                     inactivity_seconds=self.inactivity_seconds.value()))
            self.record_button.setText('停止錄製')
            self.footer.setText(f'錄製中：{Path(path).name}')
            self.footer.setToolTip(str(Path(path).resolve()))
            self.records = 0
        except OSError as exc:
            self.error_text = str(exc)
            QtWidgets.QMessageBox.critical(self, '無法錄製', str(exc))

    def toggle_recording(self):
        if self.recorder:
            path = self.recorder.path
            self.recorder.close()
            self.recorder = None
            self.record_button.setText('開始錄製')
            self.footer.setText(f'已儲存 {self.records} 筆：{path.name}')
            self.footer.setToolTip(str(path.resolve()))
        else:
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            self.start_recording(Path('captures') / f'csi-{stamp}.jsonl')

    def add_marker(self, label):
        if self.history:
            for plot in (self.activity_plot, self.rssi_plot):
                line = pg.InfiniteLine(self.history[-1]['t'] - self.origin, angle=90,
                    pen=pg.mkPen('#ffffff', style=QtCore.Qt.DashLine), label=label,
                    labelOpts={'position': .85, 'color': '#ffffff'})
                plot.addItem(line)
                self.markers.append((plot, line))
            while len(self.markers) > 40:
                plot, line = self.markers.pop(0)
                plot.removeItem(line)
        self.footer.setText(f'已標記：{label}' + ('（已儲存）' if self.recorder else '（未錄製）'))

    def mark(self):
        label = self.marker_text.text().strip()[:100] or '動作'
        if self.recorder:
            self.recorder.marker(label)
        self.add_marker(label)

    def consume(self, data, address, received):
        if self.recorder:
            try:
                self.recorder.datagram(data, address, received)
                self.records += 1
                if self.records % 200 == 0:
                    self.recorder.file.flush()
            except OSError as exc:
                self.error_text = f'錄製中斷：{exc}'
                self.recorder.close()
                self.recorder = None
                self.record_button.setText('開始錄製')
        if data.startswith(b'{'):
            try:
                status = json.loads(data)
                if status.get('type') == 'status' and status.get('name') in ('esp32c5-1', 'esp32c5-2'):
                    # Restrict fields used by UI; malformed JSON must not break the event loop.
                    for key in ('channel', 'target_hz', 'sent', 'captured', 'queue_drops', 'udp_errors', 'send_errors'):
                        if not isinstance(status.get(key), int):
                            raise ValueError('bad status fields')
                    for key in ('uptime_us', 'busy_skips', 'schedule_skips'):
                        if key in status and (not isinstance(status[key], int) or status[key] < 0):
                            raise ValueError('bad rate diagnostics')
                    if status['name'] == 'esp32c5-1':
                        previous = self.nodes.get('esp32c5-1')
                        bound = self.motion_bound_context or {}
                        changed = ((self.motion_target_hz is not None and self.motion_target_hz != status['target_hz'])
                            or (previous is not None and previous[0].get('boot') != status.get('boot'))
                            or (bound.get('target_hz') is not None and bound['target_hz'] != status['target_hz'])
                            or (bound.get('tx_boot') is not None and bound['tx_boot'] != status.get('boot')))
                        self.motion_target_hz = status['target_hz']
                        if changed:
                            self.recalibrate()
                            self.motion.note = '發射端重開機或發送率改變，請重新訓練'
                    self.nodes[status['name']] = (status, time.monotonic(), address)
                    history = self.node_history.setdefault(status['name'], deque(maxlen=6))
                    if history and history[-1][0].get('boot') != status.get('boot'):
                        history.clear()
                    history.append((status, received))
                    if (status['name'] == 'esp32c5-1' and self.pending_rate is None
                            and not self.rate_user_selected):
                        rate = str(status.get('target_hz', ''))
                        if self.rate.findText(rate) >= 0:
                            self.rate.setCurrentText(rate)
                elif status.get('type') == 'rate_ack' and not self.args.replay:
                    if status.get('name') != 'esp32c5-1' or not isinstance(status.get('token'), int):
                        raise ValueError('bad rate acknowledgement')
                    if self.pending_rate and status['token'] == self.pending_rate[0]:
                        requested_save = self.pending_rate[2]
                        if status.get('ok') is True:
                            hz = int(status['target_hz'])
                            if self.motion_target_hz is not None and hz != self.motion_target_hz:
                                self.recalibrate()
                                self.motion.note = '發送率改變，請重新訓練'
                            self.motion_target_hz = hz
                            self.rate.setCurrentText(str(hz))
                            self.rate_user_selected = False
                            self.rate_message = f'已套用 {hz} Hz'
                            if requested_save:
                                self.rate_message += '，已設為開機值' if status.get('saved') is True else '，開機值儲存失敗'
                        else:
                            self.rate_message = '發送率不支援'
                        self.pending_rate = None
                elif status.get('type') == 'marker' and self.args.replay:
                    label = str(status.get('label', ''))[:100]
                    if label == 'CALIBRATE':
                        self.recalibrate()
                    else:
                        self.add_marker(label)
                elif status.get('type') == 'settings' and self.args.replay:
                    if 'inactivity_seconds' in status:
                        seconds = status['inactivity_seconds']
                        if type(seconds) is not int or not 0 <= seconds <= 86400:
                            raise ValueError('invalid inactivity setting')
                        self.inactivity_seconds.setValue(seconds)
                    seconds = float(status.get('calibration_seconds', 10))
                    if not 1 <= seconds <= 60:
                        raise ValueError('invalid replay settings')
                    self.processor.calibration_seconds = seconds
                    q = float(status.get('kalman_q', self.processor.kalman_q))
                    r = float(status.get('kalman_r', self.processor.kalman_r))
                    self.processor.set_kalman_params(q, r)
                    self.kalman_q.blockSignals(True)
                    self.kalman_r.blockSignals(True)
                    self.kalman_q.setValue(q)
                    self.kalman_r.setValue(r)
                    self.kalman_q.blockSignals(False)
                    self.kalman_r.blockSignals(False)
                    self.shape.setChecked(bool(status.get('shape_normalization', False)))
                    if status.get('motion_profile') is not None:
                        self.pending_motion_profile = validate_profile(status['motion_profile'])
                elif status.get('type') == 'motion_event' and self.args.replay:
                    action = status.get('action')
                    if action == 'capture':
                        start_t = float(status['start_t'])
                        delay = status.get('delay_seconds', 3)
                        if (not np.isfinite(start_t) or start_t < 0 or type(status['training']) is not bool
                                or type(delay) is not int or not 0 <= delay <= 300):
                            raise ValueError('invalid motion capture')
                        self.motion.start_capture(status['label'], float(status['seconds']), status['training'], start_t)
                        self.capture_delay.setValue(delay)
                        self.capture_seconds.setValue(int(status['seconds']))
                        self.capture_mode.setCurrentIndex(0 if self.motion.capture['training'] else 1)
                        self.capture_label.setCurrentIndex(self.capture_label.findData(self.motion.capture['label']))
                    elif action == 'cancel':
                        self.motion.cancel_capture(str(status.get('reason', '已取消區段'))[:200])
                        self.finish_motion_capture()
                    elif action == 'profile':
                        profile = validate_profile(status['profile'])
                        if self.last_sample is None:
                            self.pending_motion_profile = profile
                        else:
                            self.restore_motion_profile(profile, self.last_sample)
                    elif action == 'inactivity_setting':
                        seconds = status['seconds']
                        if type(seconds) is not int or not 0 <= seconds <= 86400:
                            raise ValueError('invalid inactivity setting')
                        self.inactivity_seconds.setValue(seconds)
                    elif action == 'thresholds':
                        if self.pending_motion_profile is not None:
                            # A recording can start with a saved profile and
                            # adjustments before its first CSI datagram.
                            trial = MotionDetector()
                            trial.restore(self.pending_motion_profile['model'])
                            trial.set_thresholds(status['enter'], status['exit'])
                            self.pending_motion_profile = dict(self.pending_motion_profile, model=trial.export())
                        else:
                            self.set_motion_thresholds(status['enter'], status['exit'])
                elif status.get('type') == 'metadata' and self.args.replay:
                    recorded_mode = str(status.get('mode', ''))
                    source_label = {'DEMO / synthetic data': '模擬資料', 'LIVE UDP': '實測資料'}.get(recorded_mode, '來源未註明')
                    self.banner.setText(f'重播｜{source_label}｜{self.args.replay.name}')
                    self.banner.setToolTip(str(self.args.replay.resolve()))
            except (ValueError, TypeError, AttributeError, KeyError):
                self.invalid += 1
            return
        try:
            sample = decode(data)
            if not self.tracker.accept(sample):
                return
            pending_profile = self.pending_motion_profile
            self.pending_motion_profile = None
            if sample.signature != self.processor.signature:
                self.clear_history()
                if self.processor.signature is not None:
                    self.reset_motion('設備、頻道或增益條件改變，請重新訓練')
            if pending_profile is not None:
                self.restore_motion_profile(pending_profile, sample)
            result = self.processor.process(sample)
            if self.origin is None:
                self.origin = result['t']
            self.last_sample = sample
            self.last_seen = time.monotonic() - (0 if self.args.replay else max(0, time.time() - received))
            motion_row = result
            # Usability follows the actual window span/count and packet gaps,
            # rather than a percentage of the requested transmitter rate.
            if not self.args.replay and time.time() - received > 1:
                motion_row = dict(result, transport_issue='資料處理延遲超過 1 秒')
            elif self.source.drops != self.last_motion_drop_count:
                motion_row = dict(result, transport_issue='電腦接收佇列丟棄資料，重建資料窗')
            self.last_motion_drop_count = self.source.drops
            self.motion.process(motion_row)
            self.finish_motion_capture()
            self.update_inactivity(result['t'])
            # LIVE rates follow socket receipt, not delayed UI processing.
            arrival = time.time() if self.args.replay else received
            if self.arrival_start is None:
                self.arrival_start = arrival
            self.arrivals.append(arrival)
            self.history.append(result)
            heat_t = np.floor((result['t'] + 1e-9) / .04) * .04
            if heat_t > self.last_heat_t + .001:
                self.heat_history.append((heat_t, result['heat'].copy(), self.processor.baseline is not None))
                self.last_heat_t = heat_t
        except (ValueError, UnicodeError, TypeError, KeyError) as exc:
            self.invalid += 1
            self.error_text = str(exc)

    def tick(self):
        deadline = time.monotonic() + .018
        for _ in range(1000):
            try:
                self.consume(*self.source.items.get_nowait())
            except queue.Empty:
                break
            if time.monotonic() >= deadline:
                break
        now = time.monotonic()
        if (self.motion.capture and not self.args.replay and self.capture_wall_started is not None
                and now - self.capture_wall_started > self.capture_wait_seconds + self.motion.capture['seconds'] + self.processor.calibration_seconds + 15):
            self.cancel_motion_capture('有效資料等待逾時，請檢查收樣率與訊號後重做')
        elif self.motion.capture and self.source.finished and self.source.items.empty():
            self.cancel_motion_capture('重播結束，區段未完成' if self.args.replay else '收樣停止，區段已取消')
        elif (self.motion.capture and not self.args.replay
                and (self.source.finished or self.last_seen is None or now - self.last_seen > 2)):
            self.cancel_motion_capture('收樣停止，區段已取消；請檢查連線後重做')
        if now - self.last_draw < .1:
            return
        self.last_draw = now
        self.draw(now)

    def draw(self, now):
        self.update_threshold_controls()
        state = self.motion.result['state']
        signal_hint = self.motion.quality_reason
        if self.source.error:
            state = 'insufficient'
            signal_hint = f'接收錯誤：{self.source.error}'
        elif self.source.finished and self.source.items.empty():
            state = 'insufficient'
            signal_hint = '重播結束' if self.args.replay else '收樣已停止'
        elif self.last_seen is None or now - self.last_seen > 1:
            state = 'insufficient'
            signal_hint = '等待連線' if self.last_seen is None else '超過 1 秒未收到即時 CSI，請檢查連線'
        if (state != 'no_motion' or self.motion.enter_since is not None
                or (self.motion.capture is not None and self.motion.capture['training'])):
            self.reset_inactivity()
        score = self.motion.result['score']
        thresholds = '' if self.motion.enter is None else f'｜進入 {self.motion.enter:.2f}／退出 {self.motion.exit:.2f}'
        display = None
        if state == 'active':
            display = 'active'
        elif state == 'no_motion':
            display = 'still'
        if self.motion.enter is None:
            display = 'prompt'
        elif display == 'still' and self.inactivity_alarm:
            elapsed = max(0, now - self.inactivity_alarm_started)
            display = 'alarm_bright' if int(elapsed * 2) % 2 == 0 else 'alarm_dim'
        self.motion_status.setText({'active': '活動', 'still': f'靜止　{int(self.inactivity_elapsed)} 秒', 'prompt': TRAINING_PROMPT,
            'alarm_bright': '長時間未移動警報', 'alarm_dim': '長時間未移動警報'}.get(display, ''))
        if display != self.motion_display_state:
            self.motion_display_state = display
            background = {'active': '#16803c', 'still': '#b91c1c',
                          'alarm_bright': '#ff2020', 'alarm_dim': '#7f1d1d'}.get(display, '#22334a')
            self.motion_status.setStyleSheet(f'font-size: 56px; font-weight: 700; color: #ffffff; padding: 12px; background: {background}; border-radius: 8px;')
        hint = (signal_hint if state == 'insufficient' else STATES[state]) if display is None else ''
        metrics = f'移動分數 {score:.2f}' if score is not None and display not in (None, 'prompt') else ''
        inactivity = (f'｜靜止計時 {int(self.inactivity_elapsed)}/{self.inactivity_seconds.value()} 秒'
                      if self.motion.enter is not None and self.inactivity_seconds.value() else '')
        self.motion_metrics.setText(hint + metrics + thresholds + inactivity)
        summary = '活動／靜止模型已就緒' if self.motion.ready else '尚未建立靜止背景模型'
        self.motion_training_status.setText((self.motion.progress_text() or '先訓練靜止背景，再獨立驗證靜止與活動') + '\n' + summary)
        self.capture_button.setEnabled(not self.args.replay and self.motion.capture is None)
        self.cancel_capture_button.setEnabled(not self.args.replay and self.motion.capture is not None)
        if self.pending_rate and now - self.pending_rate[3] > 3:
            self.pending_rate = None
            self.rate_message = '設定逾時'
        arrival_now = time.time()
        while self.arrivals and self.arrivals[0] <= arrival_now - 5:
            self.arrivals.popleft()
        rate_1s = sum(ts > arrival_now - 1 for ts in self.arrivals)
        duration = 0 if self.arrival_start is None else arrival_now - self.arrival_start
        average = ((len(self.arrivals) - int(duration < 5)) / min(5, duration)
                   if duration >= 1 else None)
        lines = []
        device_details = []
        for name in ('esp32c5-1', 'esp32c5-2'):
            if name not in self.nodes:
                lines.append(f'{name}：等待連線')
            else:
                n, seen, address = self.nodes[name]
                state = '已連線' if now - seen < 3 else '未更新'
                if self.args.replay:
                    state = '錄製設備'
                elif self.args.demo:
                    state = '模擬'
                rate_text = f'，{n["target_hz"]} Hz' if name == 'esp32c5-1' else ''
                history = self.node_history.get(name, ())
                actual = ''
                if len(history) >= 2:
                    first, first_received = history[0]
                    elapsed = ((n['uptime_us'] - first['uptime_us']) / 1e6
                               if 'uptime_us' in n and 'uptime_us' in first
                               else history[-1][1] - first_received)
                    if elapsed > 0:
                        key = 'sent' if name == 'esp32c5-1' else 'captured'
                        measured = ((n[key] - first[key]) & 0xffffffff) / elapsed
                        actual = f'，實際{"發送" if key == "sent" else "擷取"} {measured:.1f} 筆/秒'
                rate_text += actual
                lines.append(f'{name}：{state}，頻道 {n["channel"]}{rate_text}')
                device_details.append(f'{name}｜{address if address != "SIMULATION" else "模擬"}｜發送 {n["sent"]} '
                    f'接收 {n["captured"]}｜發送錯誤 {n["send_errors"]}｜佇列丟棄 {n["queue_drops"]}｜UDP 錯誤 {n["udp_errors"]}')
                if name == 'esp32c5-1':
                    device_details.append(f'發射端忙碌跳過 {n.get("busy_skips", "需更新韌體")}｜排程跳過 {n.get("schedule_skips", "需更新韌體")}')
        self.devices.setText('　｜　'.join(lines))
        sample = self.last_sample
        freshness = '等待資料' if self.last_seen is None or now - self.last_seen > 2 else '接收中'
        if self.source.finished:
            freshness = '重播結束' if self.args.replay and not self.source.error else '接收停止'
        details = '' if sample is None else (
            f'AGC {sample.agc}｜FFT {sample.fft}｜增益補償 {sample.gain_compensation:.3g} 倍'
            + ('（就緒）' if sample.flags & 2 else '（準備中）'))
        rate_text = f'近 1 秒 {rate_1s} 筆｜近 5 秒平均 {average:.1f} 筆/秒' if average is not None else '接收率累積中'
        self.status.setText(f'{freshness}｜{rate_text}｜{self.processor.state}'
            + (f'｜RSSI {sample.rssi} dBm' if sample else '')
            + (f'｜缺包 {self.tracker.missing}' if self.tracker.missing else '')
            + (f'｜{self.rate_message}' if self.rate_message else '')
            + ('｜發生錯誤，請查看詳細資訊' if self.source.error or self.error_text else ''))
        self.details.setText('\n'.join(device_details + [details,
            f'UDP 埠 {self.args.port}｜接收後缺包 {self.tracker.missing}｜晚到或重複 {self.tracker.late}'
            f'｜電腦丟棄 {self.source.drops}｜無效資料 {self.invalid}'])
            + (f'\n錯誤：{self.source.error or self.error_text}' if self.source.error or self.error_text else ''))
        if not self.history:
            return
        rows = list(self.history)
        end = rows[-1]['t']
        rows = [r for r in rows if r['t'] >= end - 20]
        t = np.array([r['t'] - self.origin for r in rows])
        for key, curve in self.amp_curves.items():
            curve.setData(t, [r[key] for r in rows], connect='finite')
        self.rssi_curve.setData(t, [r['rssi'] for r in rows])
        for key, curve in self.activity_curves.items():
            curve.setData(t, [r[key] for r in rows], connect='finite')
        self.timing_curve.setData(t, [r['dt_ms'] for r in rows], connect='finite')
        intervals = np.array([r['dt_ms'] for r in rows])
        self.timing_plot.setYRange(0, max(10, float(np.nanmax(intervals)) * 1.1) if np.isfinite(intervals).any() else 10, padding=0)
        for plot in (self.amp_plot, self.rssi_plot, self.activity_plot, self.timing_plot, self.heat_plot):
            plot.setXRange(max(0, t[-1] - 20), max(1, t[-1]), padding=.01)
        calibrated = self.processor.baseline is not None
        heats = [(ts, a) for ts, a, ready in self.heat_history if ready == calibrated and ts >= end - 20]
        if len(heats) > 1:
            # Fixed 40 ms display bins. Missing intervals stay blank; no invented interpolation.
            times = np.array([p[0] for p in heats])
            begin = times[0]
            count = max(2, int(np.ceil((times[-1] - begin) / .04)) + 1)
            matrix = np.full((len(heats[0][1]), count), np.nan)
            for ts, values in heats:
                matrix[:, min(count - 1, int(round((ts - begin) / .04)))] = values
            levels = (-8, 8) if calibrated else (0, max(1, float(np.nanpercentile(matrix, 98))))
            self.heat.setImage(matrix, autoLevels=False, levels=levels)
            self.heat.setRect(QtCore.QRectF(begin - self.origin, 0, count * .04, matrix.shape[0]))
            self.heat_plot.setYRange(0, matrix.shape[0], padding=0)
            self.heat_plot.setTitle('CSI 熱圖｜' + ('基線偏差（-8 至 +8）' if calibrated else '振幅'))

    def finish_test(self):
        if self.args.screenshot:
            path = Path(self.args.screenshot)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not self.grab().save(str(path)):
                print('Screenshot save failed', file=sys.stderr)
        print(json.dumps(dict(mode=self.mode, samples=self.tracker.last,
            invalid=self.invalid, source_error=self.source.error,
            calibrated=self.processor.baseline is not None,
            pc_drops=self.source.drops), ensure_ascii=False))
        self.close()

    def closeEvent(self, event):
        self.timer.stop()
        self.source.stop_event.set()
        self.source.join(timeout=1)
        # Flush already-received packets before closing the recording.
        while not self.source.items.empty():
            self.consume(*self.source.items.get_nowait())
        if self.motion.capture:
            self.cancel_motion_capture('視窗關閉，區段未完成')
        if self.recorder:
            self.recorder.close()
            self.recorder = None
        event.accept()


def main():
    parser = argparse.ArgumentParser(description='ESP32-C5 5 GHz CSI + RSSI UDP viewer')
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5500)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--demo', action='store_true')
    mode.add_argument('--replay', type=Path)
    parser.add_argument('--record', type=Path)
    parser.add_argument('--calibration', type=float, default=10)
    parser.add_argument('--quit-after', type=float, default=0, help='UI smoke-test duration')
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.calibration <= 60:
        parser.error('port must be 1..65535; calibration must be 1..60 seconds')
    if args.record and args.replay:
        parser.error('--record cannot be combined with --replay')
    pg.setConfigOptions(antialias=False, background='#101827', foreground='#d9e4f2')
    app = QtWidgets.QApplication(sys.argv)
    # Explicit font also supports headless QA on Windows where Qt has no default font.
    if sys.platform == 'win32':
        font_path = Path('C:/Windows/Fonts/msjh.ttc')
        if font_path.exists():
            font_id = QtGui.QFontDatabase.addApplicationFont(str(font_path))
            families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families:
                app.setFont(QtGui.QFont(families[0], 9))
    app.setStyle('Fusion')
    app.setStyleSheet('QWidget { background: #101827; color: #d9e4f2; } '
        'QPushButton, QLineEdit { background: #22334a; padding: 7px; border: 1px solid #405471; border-radius: 4px; } '
        'QPushButton:hover { background: #304966; } QLabel { padding: 2px; }')
    window = Window(args)
    window.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())

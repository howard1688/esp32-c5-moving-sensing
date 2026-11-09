"""Amplitude-first diagnostics; scores are relative changes, not action labels."""
from collections import deque
import numpy as np


class Processor:
    def __init__(self, calibration_seconds=10.0, kalman_q=25.0, kalman_r=25.0):
        self.calibration_seconds = calibration_seconds
        self.normalize_shape = False
        self.kalman_q = float(kalman_q)
        self.kalman_r = float(kalman_r)
        self.signature = None
        self.reset()

    def reset(self):
        self.baseline = self.scale = self.valid = None
        self.calibration = []
        self.calibration_start = None
        self.previous = self.previous_t = self.smooth = None
        self.kalman = self.kalman_covariance = None
        self.previous_us = None
        self.window = deque()
        self.window_sum = self.window_squares = self.window_count = None
        self.progress = 0.0
        self.state = '尚未校正'

    def set_kalman_params(self, q, r):
        q, r = float(q), float(r)
        if not np.isfinite(q) or not np.isfinite(r) or q <= 0 or r <= 0:
            raise ValueError('Kalman Q and R must be finite and positive')
        self.kalman_q, self.kalman_r = q, r
        self.kalman = self.kalman_covariance = None

    def process(self, s):
        if self.signature != s.signature:
            self.signature = s.signature
            self.reset()
        t = s.timestamp_us / 1e6
        iq = np.frombuffer(s.data, dtype=np.int8).astype(np.float64).reshape(-1, 2)
        amplitude = np.hypot(iq[:, 0], iq[:, 1])
        # C5 HE20 SU layout: 0..122,-122..-1. DC and adjacent guard bins
        # (-1, 0, +1) are not usable channel estimates.
        amplitude[[0, 1, -1]] = np.nan
        # Keep bin positions stable. Do NOT shift the array on invalid first words.
        if s.flags & 1:
            amplitude[:2] = np.nan
        corrected = amplitude * (s.gain_compensation if s.flags & 2 else 1.0)
        finite = np.isfinite(corrected)
        if not finite.any():
            raise ValueError('沒有有效子載波資料')
        feature = corrected.copy()
        if self.normalize_shape:
            feature /= max(float(np.nanmedian(feature)), 1e-9)
        dt = None if self.previous_us is None else (s.timestamp_us - self.previous_us) / 1e6
        if dt is not None and dt <= 0:
            raise ValueError('取樣時間重複或倒退')
        # A long gap must not look like an instantaneous impulse.
        if dt is not None and dt > 0.25:
            self.previous = self.smooth = None
            self.kalman = self.kalman_covariance = None
            self.window.clear()
            self.window_sum = self.window_squares = self.window_count = None
            if self.baseline is None:
                self.calibration.clear()
                self.calibration_start = None
        if self.calibration_start is None:
            self.calibration_start = t
        if self.baseline is None:
            self.calibration.append(feature)
            self.progress = min(1.0, (t - self.calibration_start) / self.calibration_seconds)
            self.state = f'校正 {self.progress:.0%}，請保持靜止'
            if self.progress >= 1 and len(self.calibration) >= 50:
                matrix = np.asarray(self.calibration)
                # Exclude bins absent/zero in baseline; physical carrier mapping is not assumed.
                finite_columns = np.all(np.isfinite(matrix), axis=0)
                self.baseline = np.zeros(matrix.shape[1])
                self.scale = np.ones(matrix.shape[1])
                if finite_columns.any():
                    values = matrix[:, finite_columns]
                    base = np.median(values, axis=0)
                    noise = 1.4826 * np.median(np.abs(values - base), axis=0)
                    self.baseline[finite_columns] = base
                    self.scale[finite_columns] = np.maximum(noise, np.maximum(np.abs(base) * .03, 1e-6))
                self.valid = finite_columns & (self.baseline > 1e-6)
                if not self.valid.any():
                    self.reset()
                    raise ValueError('無法建立基線，請檢查 CSI 格式與訊號')
                self.calibration.clear()
                self.state = '校正完成'
        mean_raw = float(np.nanmean(amplitude))
        mean_corrected = float(np.nanmean(corrected))
        alpha = 1.0 if dt is None else 1 - np.exp(-max(dt, 0) / .05)
        self.smooth = mean_corrected if self.smooth is None else self.smooth + alpha * (mean_corrected - self.smooth)
        if self.kalman is None:
            self.kalman = mean_corrected
            self.kalman_covariance = self.kalman_r
        else:
            # Random-walk model: Q is process variance per second and R is
            # measurement variance, both in squared mean-amplitude units.
            self.kalman_covariance += self.kalman_q * max(dt, 0)
            gain = self.kalman_covariance / (self.kalman_covariance + self.kalman_r)
            self.kalman += gain * (mean_corrected - self.kalman)
            self.kalman_covariance *= 1.0 - gain
        deviation = variation = difference = float('nan')
        motion_std = np.full_like(feature, np.nan)
        heat = feature.copy()
        if self.baseline is not None:
            mask = self.valid & np.isfinite(feature)
            relative = np.full_like(feature, np.nan)
            relative[mask] = (feature[mask] - self.baseline[mask]) / self.scale[mask]
            heat = relative
            deviation = float(np.sqrt(np.nanmean(relative ** 2)))
            if self.previous is not None:
                difference = float(np.sqrt(np.nanmean(((feature[mask] - self.previous[mask]) / self.scale[mask]) ** 2)))
            self.window.append((t, relative))
            if self.window_sum is None:
                self.window_sum = np.zeros_like(relative)
                self.window_squares = np.zeros_like(relative)
                self.window_count = np.zeros(relative.shape, dtype=np.int64)
            present = np.isfinite(relative)
            values = np.where(present, relative, 0.0)
            self.window_sum += values
            self.window_squares += values * values
            self.window_count += present
            while self.window and self.window[0][0] < t - .5:
                old = self.window.popleft()[1]
                old_present = np.isfinite(old)
                old_values = np.where(old_present, old, 0.0)
                self.window_sum -= old_values
                self.window_squares -= old_values * old_values
                self.window_count -= old_present
            if len(self.window) > 1:
                # Population variance of the same 0.5 s window in O(bins),
                # without rebuilding a packets x bins matrix for every packet.
                selected = mask & (self.window_count > 0)
                counts = self.window_count[selected]
                means = self.window_sum[selected] / counts
                variances = self.window_squares[selected] / counts - means * means
                motion_std[selected] = np.sqrt(np.maximum(variances, 0.0))
                variation = float(np.sqrt(np.mean(np.maximum(variances, 0.0))))
        self.previous, self.previous_t = feature, t
        self.previous_us = s.timestamp_us
        return dict(t=t, raw=mean_raw, corrected=mean_corrected, smooth=self.smooth,
                    kalman=self.kalman,
                    rssi=s.rssi, deviation=deviation, variation=variation,
                    difference=difference, dt_ms=np.nan if dt is None else dt * 1000,
                    motion_std=motion_std, motion_count=len(self.window),
                    motion_span=0 if not self.window else t - self.window[0][0],
                    gain_ready=bool(s.flags & 2),
                    heat=heat, agc=s.agc, fft=s.fft,
                    clipped=float(np.mean(np.abs(iq) >= 127)))

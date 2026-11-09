"""Exploratory respiration trace from HE20 SU amplitude, not a medical monitor."""
from collections import deque

import numpy as np


class Respiration:
    def __init__(self):
        self.reset()

    def reset(self):
        self.samples = deque(maxlen=400)  # About 40 s at 10 samples/s.
        self.slow = self.fast = None
        self.last_t = self.last_estimate_t = None
        self.bin = None
        self.bpm = float('nan')
        self.candidate_bpm = float('nan')
        self.quality = float('nan')

    def process(self, t, amplitude):
        if self.last_t is not None and t - self.last_t > .5:
            self.reset()
        # Decimate before filtering and spectral analysis. Missing time slots
        # remain missing; do not invent samples through a long packet gap.
        if self.last_t is not None and t - self.last_t < .095:
            return self._last_value()
        values = amplitude / max(float(np.nanmedian(amplitude)), 1e-9)
        if self.slow is None:
            self.slow = values.copy()
            self.fast = np.zeros_like(values)
        else:
            dt = t - self.last_t
            self.slow += (1 - np.exp(-dt / 1.6)) * (values - self.slow)
            highpass = values - self.slow
            self.fast += (1 - np.exp(-dt / .35)) * (highpass - self.fast)
        self.last_t = t
        self.samples.append((t, self.fast.copy()))
        if len(self.samples) >= 180 and (self.last_estimate_t is None or t - self.last_estimate_t >= 1):
            self._estimate(t)
        return self._last_value()

    def _last_value(self):
        if self.bin is None or not self.samples:
            return float('nan'), self.bpm, self.quality
        return float(self.samples[-1][1][self.bin]), self.bpm, self.quality

    def _estimate(self, t):
        self.last_estimate_t = t
        rows = [(ts, value) for ts, value in self.samples if ts >= t - 30]
        times = np.array([ts for ts, _ in rows])
        if len(times) < 180 or times[-1] - times[0] < 18 or np.max(np.diff(times)) > .5:
            return
        values = np.asarray([value for _, value in rows])
        grid = np.arange(times[0], times[-1], .1)
        if len(grid) < 180:
            return
        # Interpolate only short timing jitter from the 10 Hz decimator.
        series = np.column_stack([np.interp(grid, times, values[:, i])
                                  for i in range(values.shape[1])])
        valid = np.all(np.isfinite(series), axis=0)
        if not np.any(valid):
            return
        series = np.nan_to_num(series - np.mean(series, axis=0))
        spectrum = np.abs(np.fft.rfft(series * np.hanning(len(grid))[:, None], axis=0)) ** 2
        frequencies = np.fft.rfftfreq(len(grid), .1)
        band = (frequencies >= .1) & (frequencies <= .5)
        background = (frequencies >= .05) & (frequencies <= 1.5)
        if not np.any(band):
            return
        noise = np.mean(spectrum[background], axis=0)
        normalized = spectrum / np.maximum(noise, 1e-12)
        common = np.mean(normalized[band][:, valid], axis=1)
        common_index = int(np.argmax(common))
        common_quality = common[common_index] / max(float(np.median(common)), 1e-12)
        band_indices = np.flatnonzero(band)
        peak_index = band_indices[common_index]
        score = normalized[peak_index].copy()
        score[~valid] = 0
        chosen = int(np.argmax(score))
        # Keep the selected subcarrier unless another is clearly better.
        if self.bin is not None and valid[self.bin] and score[self.bin] >= score[chosen] * .8:
            chosen = self.bin
        self.bin = chosen
        self.candidate_bpm = float(60 * frequencies[peak_index])
        self.quality = float(common_quality)
        self.bpm = self.candidate_bpm if common_quality >= 1.7 and score[chosen] >= 4 else float('nan')

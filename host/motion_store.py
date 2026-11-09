"""Versioned, bounded JSON profiles; restore only with the same radio context."""
import json
from pathlib import Path
import numpy as np
from motion import MotionDetector


def context_for(sample, shape, target_hz, tx_boot=None):
    return dict(boot=sample.boot, channel=sample.channel, format=sample.format,
                csi_len=sample.csi_len, source_mac=sample.source_mac.hex(),
                gain_flags=sample.flags & 2, shape=bool(shape), target_hz=target_hz, tx_boot=tx_boot)


def make_profile(processor, detector, context, mode):
    if processor.baseline is None or detector.enter is None:
        raise ValueError('請先完成背景訓練')
    return dict(schema=1, mode=mode, context=context, model=detector.export(),
                baseline=processor.baseline.tolist(), scale=processor.scale.tolist(),
                valid=processor.valid.tolist())


def validate_profile(payload):
    obj = json.loads(json.dumps(payload, allow_nan=False))
    if obj.get('schema') != 1 or not isinstance(obj.get('context'), dict):
        raise ValueError('不支援的模型檔案')
    context = obj['context']
    for key in ('boot', 'channel', 'format', 'csi_len', 'gain_flags'):
        if type(context.get(key)) is not int:
            raise ValueError('模型的設備設定不合法')
    if (context['csi_len'] != 490 or context['format'] != 4 or context['channel'] <= 14
            or context['gain_flags'] != 2 or type(context.get('shape')) is not bool
            or context.get('source_mac') != '0245534fc501'
            or context.get('target_hz') not in (None, 50, 100, 200, 250, 500, 1000)):
        raise ValueError('模型的無線設定不合法')
    baseline, scale = np.asarray(obj['baseline'], dtype=float), np.asarray(obj['scale'], dtype=float)
    valid = np.asarray(obj['valid'])
    if (baseline.shape != (245,) or scale.shape != (245,) or valid.shape != (245,)
            or valid.dtype != np.bool_ or not valid.any() or not np.isfinite(baseline).all()
            or not np.isfinite(scale).all() or np.any(scale <= 0) or np.any(baseline[valid] <= 0)):
        raise ValueError('模型的振幅基線不合法')
    trial = MotionDetector()
    trial.restore(obj['model'])
    return obj


def apply_profile(payload, processor, detector, context):
    obj = validate_profile(payload)
    expected = obj['context']
    for key in ('boot', 'channel', 'format', 'csi_len', 'source_mac', 'gain_flags'):
        if expected[key] != context[key]:
            raise ValueError('模型與目前設備不符；重開板子或換頻道後請重新訓練')
    if (expected['target_hz'] is not None and context['target_hz'] is not None
            and expected['target_hz'] != context['target_hz']):
        raise ValueError('發送率不同，請重新訓練')
    if (expected.get('tx_boot') is not None and context.get('tx_boot') is not None
            and expected['tx_boot'] != context['tx_boot']):
        raise ValueError('發射端已重開機，請重新訓練')
    if (processor.baseline is None or processor.normalize_shape != expected['shape']
            or not np.array_equal(processor.baseline, obj['baseline'])
            or not np.array_equal(processor.scale, obj['scale'])
            or not np.array_equal(processor.valid, obj['valid'])):
        processor.reset()
    processor.normalize_shape = expected['shape']
    processor.baseline = np.asarray(obj['baseline'], dtype=float)
    processor.scale = np.asarray(obj['scale'], dtype=float)
    processor.valid = np.asarray(obj['valid'], dtype=bool)
    processor.progress = 1.0
    processor.state = '模型基線已載入'
    detector.restore(obj['model'])
    return expected


def read_profile(path):
    path = Path(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('模型檔案過大')
    return validate_profile(json.loads(path.read_text(encoding='utf-8')))


def write_json(path, payload, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x' if exclusive else 'w', encoding='utf-8') as file:
        json.dump(payload, file, ensure_ascii=False, allow_nan=False, indent=2)

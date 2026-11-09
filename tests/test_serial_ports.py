import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('serial_listing',
    Path(__file__).resolve().parents[1] / 'scripts' / 'list_serial_ports.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def port(device, vid=None, pid=None, hwid='USB'):
    return SimpleNamespace(device=device, vid=vid, pid=pid, hwid=hwid,
        description='test', serial_number='serial')


class SerialDiscoveryTests(unittest.TestCase):
    def test_bluetooth_and_legacy_ports_excluded(self):
        self.assertEqual(module.usb_ports([port('COM5', hwid='BTHENUM'),
            port('COM6', 0x303a, 0x1001, 'BTHENUM'), port('COM1', hwid='ACPI')]), [])

    def test_espressif_recognized_and_other_usb_not_hidden(self):
        result = module.usb_ports([port('COM3', 0x303a, 0x1001), port('COM4', 0x10c4, 0xea60)])
        self.assertEqual([p['port'] for p in result], ['COM3', 'COM4'])
        self.assertEqual(result[0]['kind'], 'Espressif USB')
        self.assertIn('not verified', result[1]['kind'])

    def test_no_devices(self):
        self.assertEqual(module.usb_ports([]), [])

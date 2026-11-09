"""Read USB serial metadata only; never open/reset/probe a board."""
import json


def usb_ports(ports):
    result = []
    for p in ports:
        hwid = p.hwid or ''
        if 'BTHENUM' in hwid.upper() or 'BLUETOOTH' in hwid.upper():
            continue
        if p.vid is None or p.pid is None:
            continue
        result.append(dict(port=p.device, description=p.description or '',
            vid=f'{p.vid:04X}', pid=f'{p.pid:04X}', serial=p.serial_number or '',
            kind='Espressif USB' if p.vid == 0x303A else 'USB serial (chip not verified)'))
    return sorted(result, key=lambda p: p['port'])


if __name__ == '__main__':
    from serial.tools import list_ports
    print(json.dumps(usb_ports(list_ports.comports()), ensure_ascii=True))

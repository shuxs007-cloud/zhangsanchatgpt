"""User-maintained report mapping; never changes backend device configuration."""
import hashlib
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[3] / 'scripts/opensms_room_settings.json'
CONFIG_BYTES = CONFIG_PATH.read_bytes()
CONFIG = json.loads(CONFIG_BYTES)
CONFIG_SHA256 = hashlib.sha256(CONFIG_BYTES).hexdigest()
DEVICE_ROOMS = {}
LABELS = {}
for room in CONFIG['rooms']:
    if room['name'] in LABELS or room['label'] in LABELS.values():
        raise ValueError('机房配置名称重复')
    LABELS[room['name']] = room['label']
    devices = set()
    for start, end in room['deviceRanges']:
        if type(start) is not int or type(end) is not int or start < 0 or start > end:
            raise ValueError('机房设备范围无效')
        for device in range(start, end + 1):
            if device in DEVICE_ROOMS:
                raise ValueError('机房设备范围重叠')
            DEVICE_ROOMS[device] = room['name']
            devices.add(device)
    if len(devices) != room['deviceCount']:
        raise ValueError('机房配置设备数量不符')
if len(DEVICE_ROOMS) != CONFIG['totalDeviceCount']:
    raise ValueError('机房配置设备总数不符')


def room_for_device(device):
    value = str(device or '').strip()
    return DEVICE_ROOMS.get(int(value), '未配置') if value.isascii() and value.isdigit() else '未配置'


def range_text(room):
    return '、'.join(str(a) if a == b else f'{a}–{b}' for a, b in room['deviceRanges'])


def summary_items():
    return [
        ('机房配置生效日期', CONFIG['effectiveDate']),
        ('机房配置SHA256', CONFIG_SHA256),
        *((f"{r['name']}设备配置", f"{range_text(r)}（{r['deviceCount']}台）") for r in CONFIG['rooms']),
        ('配置设备总数', CONFIG['totalDeviceCount']),
        ('机房配置口径', '配置数不代表当前在线或有数据设备数；未列设备为未配置；字母前缀设备不并入数字设备'),
    ]

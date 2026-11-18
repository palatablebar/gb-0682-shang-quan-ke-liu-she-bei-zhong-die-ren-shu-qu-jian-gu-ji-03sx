"""时钟校准。

裁决：校准后的真实时刻是分桶与交集判断的正式口径；原始采集时刻保留在
bucket_raw 中只读留痕。校准证据无效时整批不生效，调用方回退原始时刻，
且不得产生新的归档版本（store.save 对 rejected 状态硬拒绝）。
"""
import hashlib
import json
from datetime import datetime, timedelta

from app.errors import DomainError

# 单设备时钟偏差超过 24 小时视为证据不可信，整批拒绝。
MAX_OFFSET_MINUTES = 24 * 60
CALIBRATION_KEY = 'clock_rules'


def _parse_offset(text):
    """解析带符号分钟偏移，如 '13'、'-7'、'+13'。"""
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        raise DomainError('CLOCK', '时钟偏移必须是带符号整数分钟')
    if not -MAX_OFFSET_MINUTES <= value <= MAX_OFFSET_MINUTES:
        raise DomainError('CLOCK', '时钟偏移超出可信范围(±24小时)')
    return value


def validate_rules(rules):
    """校验校准证据。证据结构有任何缺陷即整体拒绝，不做部分采纳。"""
    if not isinstance(rules, list) or not rules:
        raise DomainError('CLOCK', '校准证据必须是非空规则数组')
    seen_devices = set()
    evidence_ids = set()
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise DomainError('CLOCK', '校准规则必须是对象')
        device = str(rule.get('device_id', '')).strip()
        if not device:
            raise DomainError('CLOCK', '校准规则缺少设备标识')
        if device in seen_devices:
            raise DomainError('CLOCK', '同一设备存在多条校准规则')
        seen_devices.add(device)
        offset = _parse_offset(rule.get('offset_minutes'))
        evidence = str(rule.get('evidence_id', '')).strip()
        if not evidence:
            raise DomainError('CLOCK', '校准规则缺少证据编号')
        evidence_ids.add(evidence)
        normalized.append({'device_id': device, 'offset_minutes': offset,
                           'evidence_id': evidence})
    if len(evidence_ids) != len(normalized):
        raise DomainError('CLOCK', '校准证据编号不一致')
    return normalized


def batch_id(rules):
    """同一批校准证据的确定性指纹，校准依据/重算/归档共用。"""
    payload = json.dumps(rules, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()[:20]


def calibrate_instant(raw_iso, offset_minutes):
    """真实时刻 = 设备时钟读数 + 偏移（慢钟 offset 为正）。"""
    moment = datetime.fromisoformat(raw_iso)
    return (moment + timedelta(minutes=offset_minutes)).isoformat()


def device_key(row):
    """设备标识：显式 device_id 列优先，否则取业务 ID 的下划线前缀。"""
    explicit = str(row.get('device_id', '')).strip()
    if explicit:
        return explicit
    return str(row.get('id', '')).split('_', 1)[0]


def apply_clock_calibration(state, parameters):
    """在 M2 归一结果上挂接双时刻与校准摘要。

    成功：bucket_calibrated 为正式口径，bucket_raw 保留采集原貌。
    失败（CLOCK）：不修改任何时刻，state 记录失败原因并回退原始口径，
    异常向上抛由接口层拒绝，归档流程不会被触发。
    """
    rules_payload = (parameters or {}).get(CALIBRATION_KEY)
    state['clock_calibration'] = {
        'status': 'not_applied', 'batch_id': None, 'rules': [],
        'matched': 0, 'unmatched': 0, 'rejected_reason': None,
    }
    if not rules_payload:
        for row in state['rows']:
            row['bucket_raw'] = row['bucket']
            row['bucket_calibrated'] = row['bucket']
            row['clock_offset_minutes'] = None
            row['clock_evidence_id'] = None
        return state
    try:
        rules = validate_rules(rules_payload)
    except DomainError as error:
        state['clock_calibration']['status'] = 'rejected'
        state['clock_calibration']['rejected_reason'] = error.message
        state['clock_calibration']['rules'] = rules_payload
        for row in state['rows']:
            row['bucket_raw'] = row['bucket']
            row['bucket_calibrated'] = row['bucket']
            row['clock_offset_minutes'] = None
            row['clock_evidence_id'] = None
        raise
    by_device = {rule['device_id']: rule for rule in rules}
    matched = 0
    for row in state['rows']:
        row['bucket_raw'] = row['bucket']
        rule = by_device.get(device_key(row))
        if rule is None:
            # 无校准证据的设备：保持原时刻，但同样留双时刻痕迹。
            row['bucket_calibrated'] = row['bucket']
            row['clock_offset_minutes'] = None
            row['clock_evidence_id'] = None
            continue
        row['bucket_calibrated'] = calibrate_instant(row['bucket'], rule['offset_minutes'])
        row['clock_offset_minutes'] = rule['offset_minutes']
        row['clock_evidence_id'] = rule['evidence_id']
        matched += 1
    state['clock_calibration'] = {
        'status': 'applied', 'batch_id': batch_id(rules), 'rules': rules,
        'matched': matched, 'unmatched': len(state['rows']) - matched,
        'rejected_reason': None,
    }
    return state

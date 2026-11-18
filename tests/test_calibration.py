import copy
import csv
import io

import pytest

from app import store
from app.errors import DomainError
from app.modules.m2_mapping.calibration import (
    apply_clock_calibration, batch_id, validate_rules,
)
from app.modules.m7_archive.export_out import bundle
from app.workflow import load, run

RULES = [{'device_id': 'D1', 'offset_minutes': 13, 'evidence_id': 'EVD-1'}]
PARAMS = {'clock_rules': RULES}


def _drift_state(stage=7, params=None):
    return run(load('drift.csv'), PARAMS if params is None else params, stage)


def test_dual_timestamp_keeps_raw_and_calibrated():
    s = _drift_state(2)
    row = next(r for r in r_rows(s) if r['id'] == 'D1_07')
    assert row['bucket_raw'] == '2026-11-16T02:47:00+00:00'
    assert row['bucket_calibrated'] == '2026-11-16T03:00:00+00:00'
    assert row['clock_offset_minutes'] == 13
    assert row['clock_evidence_id'] == 'EVD-1'


def r_rows(state):
    return state['rows']


def test_calibrated_bucket_is_official_bucket():
    s = _drift_state(3)
    buckets = {r['bucket'] for r in s['observations']}
    # 10:47+08(=02:47Z) 经 +13 分回到 03:00Z，两组观测同桶；原始时刻不参与分桶。
    assert buckets == {'2026-11-16T01:00:00+00:00', '2026-11-16T03:00:00+00:00'}
    for r in s['observations']:
        assert r['bucket'].endswith(':00:00+00:00')
        assert r['bucket_raw'] is not None and r['bucket_calibrated'] is not None


def test_without_calibration_observations_split_across_hours():
    # 错误口径对照：不校准时第二组落在 02:00Z 桶，联合区间被拆小时。
    s = run(load('drift.csv'), {}, 3)
    assert len({r['bucket'] for r in s['observations']}) == 2
    assert '2026-11-16T02:00:00+00:00' in {r['bucket'] for r in s['observations']}


def test_calibration_changes_bounds_bucket_at_hour_boundary():
    # 两组记录相差不到一小时，错误选择让上下界落到不同小时。
    raw = run(load('drift.csv'), {})
    fixed = _drift_state()
    raw_buckets = {c['id'] for c in raw['calculations']}
    fixed_buckets = {c['id'] for c in fixed['calculations']}
    assert '2026-11-16T02:00:00+00:00' in raw_buckets
    assert '2026-11-16T02:00:00+00:00' not in fixed_buckets
    assert '2026-11-16T03:00:00+00:00' in fixed_buckets
    # 校准后每个真实小时桶仍输出完整三方上下界：09:00 为 230..250，11:00 为 230..251。
    by_id = {c['id']: c for c in fixed['calculations']}
    assert (by_id['2026-11-16T01:00:00+00:00']['lower'],
            by_id['2026-11-16T01:00:00+00:00']['upper']) == (230, 250)
    assert (by_id['2026-11-16T03:00:00+00:00']['lower'],
            by_id['2026-11-16T03:00:00+00:00']['upper']) == (230, 251)


def test_calibration_batch_id_deterministic():
    assert batch_id(validate_rules(RULES)) == batch_id(validate_rules(copy.deepcopy(RULES)))


def test_same_calibration_rerun_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'c.db'))
    first, second = _save_pair()
    assert first['run_id'] == second['run_id']
    assert len(store.history()) == 1


def _full():
    return _drift_state()


def _save_pair(tmp_path=None):
    state = _full()
    return store.save(state['run_id'], state), store.save(state['run_id'], state)


def test_failed_calibration_keeps_old_archive(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'f.db'))
    good = _full()
    store.save(good['run_id'], good)
    before = len(store.history())
    bad_rules = [{'device_id': 'D1', 'offset_minutes': 'not-a-number', 'evidence_id': 'EVD-X'}]
    # run() 在校准阶段即拒绝，调用方拿不到可归档的新 state。
    with pytest.raises(DomainError) as e:
        run(load('drift.csv'), {'clock_rules': bad_rules})
    assert e.value.code == 'CLOCK'
    # 即便构造出 rejected 状态，存储闸门也拒绝写入，旧归档原样保留。
    poisoned = copy.deepcopy(good)
    poisoned['clock_calibration']['status'] = 'rejected'
    with pytest.raises(DomainError) as gate:
        store.save('new-run-id', poisoned)
    assert gate.value.code == 'CLOCK'
    assert len(store.history()) == before
    assert store.get(good['run_id'])['archive']['sha256'] == good['archive']['sha256']


def test_invalid_rules_rejected_as_whole():
    for bad in [
        [],
        [{'device_id': 'D1', 'offset_minutes': 99999, 'evidence_id': 'E'}],
        [{'device_id': '', 'offset_minutes': 1, 'evidence_id': 'E'}],
        [{'device_id': 'D1', 'offset_minutes': 1}],
        [{'device_id': 'D1', 'offset_minutes': 1, 'evidence_id': 'E'},
         {'device_id': 'D1', 'offset_minutes': 2, 'evidence_id': 'F'}],
    ]:
        with pytest.raises(DomainError) as e:
            validate_rules(bad)
        assert e.value.code == 'CLOCK'


def test_rejected_calibration_falls_back_to_raw_instants():
    state = run(load('drift.csv'), {}, 2)
    before = {r['id']: r['bucket'] for r in state['rows']}
    with pytest.raises(DomainError):
        apply_clock_calibration(state, {'clock_rules': [
            {'device_id': 'D1', 'offset_minutes': 'x', 'evidence_id': 'E'}]})
    assert state['clock_calibration']['status'] == 'rejected'
    assert all(r['bucket_calibrated'] == before[r['id']] for r in state['rows'])


def test_archive_export_cites_same_calibration_batch():
    state = _full()
    text = bundle(state).lstrip('﻿')
    bid = state['clock_calibration']['batch_id']
    assert bid and bid in text and '时钟校准' in text and '校准证据' in text
    rows = list(csv.reader(io.StringIO(text)))
    clock_rows = [r for r in rows if r[0] in ('时钟校准', '校准证据')]
    # 校准依据、重算区间、归档页面引用同一批观测指纹。
    assert any(bid in r[5] for r in clock_rows)
    assert state['archive']['clock_calibration_batch'] == bid
    assert state['archive']['sha256']


def test_unmatched_device_keeps_raw_instant():
    rules = [{'device_id': 'OTHER', 'offset_minutes': 13, 'evidence_id': 'EVD-2'}]
    s = run(load('drift.csv'), {'clock_rules': rules}, 2)
    assert s['clock_calibration']['matched'] == 0
    assert all(r['bucket_calibrated'] == r['bucket_raw'] for r in s['rows'])


def test_explicit_device_id_column_wins(tmp_path):
    header = 'id,bucket,version,kind,left,right,count,unit,device_id'
    csv_text = (header + '\nZ9_1,2026-11-16T10:47:00+08:00,1,zone,东门,,10,人,Z9\n')
    s = run(csv_text.encode(), {'clock_rules': [
        {'device_id': 'Z9', 'offset_minutes': 13, 'evidence_id': 'E'}]}, 2)
    row = s['rows'][0]
    assert row['bucket_calibrated'] == '2026-11-16T03:00:00+00:00'

"""Offline checks for Dunstan's supervision-qc-contract.md (no production access)."""
import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'gateway'))
import gateway as api
import writeback as qc


def payload(**updates):
    data=dict(operation_key='11111111-1111-4111-8111-111111111111',
              recording_id=896,contributor_id='P0026',reviewer_id='4',
              reviewer_name='Supervisor Gloria',decision='accept',
              expected_qc_tier3=None,reason_code=None,note=None)
    data.update(updates)
    return data


def test_accept_without_reason():
    data=qc._validate(payload())
    assert data['decision']=='accept' and data['reason_code'] is None


def test_rejection_with_valid_reason():
    assert qc._validate(payload(decision='reject',reason_code='noise'))['reason_code']=='noise'


@pytest.mark.parametrize('overrides',[dict(decision='approve'),dict(decision='accepted'),dict(decision='reject'),
                                     dict(decision='reject',reason_code='other'),
                                     dict(decision='reject',reason_code='unknown'),
                                     dict(decision='accept',reason_code='noise'),
                                     dict(expected_qc_tier3='pending'),
                                     dict(reviewer_id=''),dict(operation_key='abc')])
def test_malformed_review_rejected(overrides):
    with pytest.raises(HTTPException):qc._validate(payload(**overrides))


def test_reversal_expected_state():
    assert qc._validate(payload(expected_qc_tier3='reject',decision='accept'))['expected_qc_tier3']=='reject'
    assert qc._validate(payload(decision='reject',reason_code='other',note='Different speaker'))['note']=='Different speaker'


def test_write_disabled_by_default(monkeypatch):
    monkeypatch.delenv('SUPERVISION_WRITES_ENABLED',raising=False)
    monkeypatch.setenv('SUPERVISION_REVIEW_TOKEN','secret' * 10)
    with pytest.raises(HTTPException) as error:
        qc._write_access(Request({'type':'http','headers':[]}))
    assert error.value.status_code==503


def test_progress_not_capped_at_ten_hours():
    assert api._aggregate_ms(500*3600)==500*3600000


def test_changelog_row_has_all_tiers():
    from datetime import datetime,timezone
    t=datetime(2026,11,11,tzinfo=timezone.utc)
    raw=(27,'P0001',t,t,8.8,'stored','pending','',None,
         'active',False,'round1',4,'accept','', 'silver','clear',
         'sentence','accept','11111111-1111-4111-8111-111111111111',t)
    entry=api._changed_record(raw)
    assert entry['qc_status']=='pending'
    assert entry['qc_tier3']=='accept' and entry['qc_final']=='silver'
    assert entry['duration_ms']==8800


def test_owner_migration_not_shipped():
    assert not (ROOT/'collector/migrations/versions/9b4d0e72c1f0_supervisor_human_reviews.py').exists()


def test_no_collector_qc_mutations_in_writer():
    source=(ROOT/'gateway/writeback.py').read_text()
    assert 'UPDATE public.recordings' not in source
    assert 'DELETE FROM public.recordings' not in source
    assert 'INSERT INTO public.qc_human_reviews' in source
    assert 'ON CONFLICT (recording_id)' not in source

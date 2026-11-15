"""Dunstan contract: insert-only supervisor decisions in qc_human_reviews.

The production owner controls Alembic, table permissions and triggers. This
module cannot change arrival QC or final QC directly. Writes default OFF.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

DECISIONS = ('accept', 'reject')
REJECTION_REASONS = ('wrong_sentence', 'noise', 'not_own_voice', 'unclear', 'not_chichewa', 'other')


def _write_access(request: Request) -> tuple[str, str]:
    if os.getenv('SUPERVISION_WRITES_ENABLED', '0') != '1':
        raise HTTPException(503, 'Supervisor writes are disabled pending owner approval')
    secret = os.getenv('SUPERVISION_REVIEW_TOKEN', '')
    write_dsn = os.getenv('SUPERVISION_WRITE_DSN', '')
    read_dsn = os.getenv('SUPERVISION_READONLY_DSN', '')
    if len(secret) < 48 or not write_dsn or not read_dsn:
        raise HTTPException(503, 'Supervisor write connection incomplete')
    supplied = request.headers.get('authorization', '')
    if not supplied.startswith('Bearer ') or not hmac.compare_digest(
        hashlib.sha256(supplied[7:].encode()).digest(),
        hashlib.sha256(secret.encode()).digest(),
    ):
        raise HTTPException(401, 'Unauthorized', headers={'WWW-Authenticate': 'Bearer'})
    return write_dsn, read_dsn


def _validate(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(422, 'Invalid review payload')
    try:
        key = str(uuid.UUID(str(payload.get('operation_key', ''))))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(422, 'Invalid operation key')
    rid = payload.get('recording_id')
    decision = payload.get('decision')
    contributor = payload.get('contributor_id')
    reviewer_id = payload.get('reviewer_id')
    reviewer_name = payload.get('reviewer_name')
    reason = payload.get('reason_code') or None
    note = payload.get('note') or None
    expected = payload.get('expected_qc_tier3')
    if type(rid) is not int or not (0 < rid < 2**63):
        raise HTTPException(422, 'Invalid recording ID')
    if decision not in DECISIONS:
        raise HTTPException(422, 'Invalid supervisor decision')
    if not isinstance(contributor, str) or not contributor.isascii() or not (1 <= len(contributor) <= 80) or not all(c.isalnum() or c in '-_' for c in contributor):
        raise HTTPException(422, 'Invalid contributor ID')
    if not isinstance(reviewer_id, str) or not reviewer_id.isascii() or not (1 <= len(reviewer_id) <= 80) or not all(c.isalnum() or c in '-_' for c in reviewer_id):
        raise HTTPException(422, 'Invalid reviewer ID')
    if not isinstance(reviewer_name, str) or not (2 <= len(reviewer_name.strip()) <= 120):
        raise HTTPException(422, 'Invalid reviewer display name')
    if note is not None and (not isinstance(note, str) or len(note.strip()) > 500):
        raise HTTPException(422, 'Invalid reviewer note')
    note = note.strip() if isinstance(note, str) and note.strip() else None
    if decision == 'reject':
        if reason not in REJECTION_REASONS:
            raise HTTPException(422, 'Select an approved rejection reason')
        if reason == 'other' and not note:
            raise HTTPException(422, 'Explain the other rejection reason')
    elif reason is not None:
        raise HTTPException(422, 'Acceptance cannot have a rejection reason')
    if expected not in (None, 'accept', 'reject'):
        raise HTTPException(422, 'Invalid expected Tier 3 state')
    return dict(operation_key=key, recording_id=rid, contributor_id=contributor,
                reviewer_id=reviewer_id, reviewer_name=reviewer_name.strip(),
                decision=decision, reason_code=reason, note=note,
                expected_qc_tier3=expected)


def _writer_permission_check(cur) -> None:
    # Writer has INSERT/SELECT on qc_human_reviews, and NO recording mutations.
    # Intentionally no UPDATE/DELETE on the audit table.
    cur.execute('''SELECT
       COALESCE(has_table_privilege(current_user,to_regclass('public.qc_human_reviews'),'SELECT'),false),
       COALESCE(has_table_privilege(current_user,to_regclass('public.qc_human_reviews'),'INSERT'),false),
       COALESCE(has_table_privilege(current_user,to_regclass('public.qc_human_reviews'),'UPDATE'),false),
       COALESCE(has_table_privilege(current_user,to_regclass('public.qc_human_reviews'),'DELETE'),false),
       COALESCE(has_any_column_privilege(current_user,to_regclass('public.recordings'),'SELECT'),false),
       COALESCE(has_any_column_privilege(current_user,to_regclass('public.recordings'),'UPDATE'),false),
       COALESCE(has_table_privilege(current_user,to_regclass('public.recordings'),'INSERT'),false),
       COALESCE(has_table_privilege(current_user,to_regclass('public.recordings'),'DELETE'),false)
    ''')
    reads, inserts, audit_update, audit_delete, rec_select, rec_update, rec_insert, rec_delete = cur.fetchone()
    if not reads or not inserts or any((audit_update, audit_delete, rec_select, rec_update, rec_insert, rec_delete)):
        raise HTTPException(503, 'Supervisor writer privileges do not match approved contract')


def _read_contract_check(cur) -> None:
    # Fails closed until the owner has installed/granted new columns and triggers.
    cur.execute('''SELECT r.qc_tier3,r.qc_tier3_reason,r.qc_tier2,r.qc_final
                   FROM public.recordings r LIMIT 0''')
    cur.execute('''SELECT recording_id,decision,reason_code,note,reviewer_id,
                          reviewer_name,operation_key,created_at,notified_at
                   FROM public.qc_human_reviews LIMIT 0''')
    cur.execute('''SELECT EXISTS (
      SELECT 1 FROM pg_trigger
       WHERE tgrelid=to_regclass('public.qc_human_reviews')
         AND NOT tgisinternal AND tgenabled IN ('O','A')
         AND (tgtype & 4) <> 0
    )''')
    if not cur.fetchone()[0]:
        raise HTTPException(503, 'Owner QC trigger not installed')


def _read_record(cur, recording_id: int):
    cur.execute('''SELECT r.id,r.participant_id,r.status,r.qc_tier3,
                          r.duplicate_of_recording_id,p.is_test
                   FROM public.recordings r
                   JOIN public.participants_public p ON p.participant_id=r.participant_id
                   WHERE r.id=%s''', (recording_id,))
    return cur.fetchone()


def register(app) -> None:
    @app.get('/v1/supervision/write-status')
    def write_status(request: Request):
        write_dsn, read_dsn = _write_access(request)
        from psycopg import connect
        try:
            with connect(read_dsn, connect_timeout=5, options='-c default_transaction_read_only=on') as read_conn:
                with read_conn.cursor() as cur:
                    _read_contract_check(cur)
            with connect(write_dsn, connect_timeout=5) as writer:
                with writer.cursor() as cur:
                    _writer_permission_check(cur)
                    cur.execute('''SELECT operation_key,recording_id,decision,reason_code,note,
                                          reviewer_id,reviewer_name FROM public.qc_human_reviews LIMIT 0''')
            return JSONResponse(dict(enabled=True, mode='human-review',
                                     decisions=list(DECISIONS), audit='append-only',
                                     collector_qc_mutation=False), headers={'Cache-Control':'no-store'})
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, 'Owner-controlled QC schema or permissions unavailable')

    @app.post('/v1/supervision/reviews')
    async def submit_review(request: Request):
        write_dsn, read_dsn = _write_access(request)
        raw = await request.body()
        if len(raw) > 4096:
            raise HTTPException(413, 'Review payload too large')
        try:
            data = _validate(json.loads(raw))
        except (ValueError, TypeError):
            raise HTTPException(422, 'Invalid review JSON')
        from psycopg import connect
        from psycopg.rows import dict_row
        try:
            with connect(write_dsn, connect_timeout=8, options='-c statement_timeout=15000',row_factory=dict_row) as writer:
                with writer.cursor() as cur:
                    _writer_permission_check(cur)
                    # Serialize our gateway submissions for this recording, and
                    # make replays of the same UUID safe. Owner's DB trigger
                    # must also atomically enforce the expected Tier 3 state.
                    cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (str(data['recording_id']),))
                    cur.execute('''SELECT operation_key::text,recording_id,decision,reason_code,
                                          note,reviewer_id,reviewer_name
                                   FROM public.qc_human_reviews WHERE operation_key=%s::uuid''',
                                (data['operation_key'],))
                    existing = cur.fetchone()
                    fields = ('recording_id','decision','reason_code','note','reviewer_id','reviewer_name')
                    if existing:
                        if not all(existing[k] == data[k] for k in fields):
                            raise HTTPException(409, 'Operation key used for a different decision')
                        return JSONResponse(dict(confirmed=True, mode='human-review', replayed=True,
                                                 operation_key=data['operation_key'],
                                                 human_decision=existing['decision']),
                                            headers={'Cache-Control':'no-store'})
                    with connect(read_dsn, connect_timeout=8,
                                 options='-c default_transaction_read_only=on -c statement_timeout=10000',
                                 row_factory=dict_row) as reader:
                        with reader.cursor() as rc:
                            _read_contract_check(rc)
                            record = _read_record(rc, data['recording_id'])
                            if not record or record['participant_id'] != data['contributor_id']:
                                raise HTTPException(404, 'Recording not in authorized catalog')
                            if record['status'] != 'stored':
                                raise HTTPException(409, 'Recording not stored')
                            if record['qc_tier3'] != data['expected_qc_tier3']:
                                raise HTTPException(409, 'Tier 3 state changed; refresh before submitting')
                            if data['decision'] == 'accept' and (record['is_test'] or record['duplicate_of_recording_id'] is not None):
                                raise HTTPException(409, 'Test or duplicate recording cannot be accepted')
                    # Owner trigger must compare this session value under a DB
                    # row lock, then set qc_tier3 / qc_final atomically.
                    cur.execute("SELECT set_config('masakhane.expected_qc_tier3',%s,true)",
                                (data['expected_qc_tier3'] if data['expected_qc_tier3'] is not None else '__NULL__',))
                    cur.execute('''INSERT INTO public.qc_human_reviews
                        (recording_id,decision,reason_code,note,reviewer_id,reviewer_name,operation_key)
                        VALUES (%s,%s,%s,%s,%s,%s,%s::uuid)
                        ON CONFLICT (operation_key) DO NOTHING RETURNING id''',
                        (data['recording_id'],data['decision'],data['reason_code'],data['note'],
                         data['reviewer_id'],data['reviewer_name'],data['operation_key']))
                    if cur.fetchone() is None:
                        raise HTTPException(409, 'Concurrent review request; retry with same operation key')
            return JSONResponse(dict(confirmed=True, mode='human-review', replayed=False,
                                     operation_key=data['operation_key'],
                                     human_decision=data['decision']), headers={'Cache-Control':'no-store'})
        except HTTPException:
            raise
        except Exception:
            # Never report a final decision after an ambiguous network failure.
            raise HTTPException(503, 'Review confirmation unavailable; retry with same operation key')

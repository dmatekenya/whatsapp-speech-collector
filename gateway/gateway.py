"""Isolated production gateway: restricted read-only snapshot and audio, plus
optional owner-authorized, audited QC write-back using separate credentials.
No WhatsApp sending or contributor names/telephone numbers are exposed.
Run only on the private Docker network behind existing HTTPS Caddy.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

VERSION = "0.0.0.3-owner-review"
MAX_AUDIO_BYTES = 25_000_000
AUDIO_MIME = {'.amr': 'audio/amr', '.bin': 'application/octet-stream', '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.opus': 'audio/ogg', '.webm': 'audio/webm', '.m4a': 'audio/mp4', '.aac': 'audio/aac'}

app = FastAPI(title="Masakhane restricted production gateway", version=VERSION,
              docs_url=None, redoc_url=None, openapi_url=None)


def _configuration() -> tuple[str, str, Path]:
    token = os.environ.get("SUPERVISION_GATEWAY_TOKEN", "")
    db_url = os.environ.get("SUPERVISION_READONLY_DSN", "")
    root = Path(os.environ.get("SUPERVISION_AUDIO_ROOT", "/audio")).resolve()
    if len(token) < 48 or not db_url or not root.is_dir():
        raise RuntimeError("Gateway configuration incomplete")
    return token, db_url, root


def _verified(request: Request) -> tuple[str, Path]:
    try:
        token, dsn, root = _configuration()
    except RuntimeError:
        raise HTTPException(503, detail="Gateway not configured")
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not hmac.compare_digest(
        hashlib.sha256(header[7:].encode("utf-8")).digest(),
        hashlib.sha256(token.encode("utf-8")).digest(),
    ):
        raise HTTPException(401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"})
    return dsn, root


def _conn(dsn: str):
    # Separate least-privilege PostgreSQL login; never use the collector superuser.
    from psycopg import connect
    return connect(dsn, connect_timeout=8, options='-c default_transaction_read_only=on -c statement_timeout=12000')


def _duration_ms(value: Any) -> int | None:
    if value is None:
        return None
    ms = int((Decimal(str(value)) * Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if ms < 0 or ms > 36_000_000:
        return None
    return ms


def _snapshot(rows: list[tuple], prompt_metadata_available: bool = False, human_review_available: bool = False) -> dict[str, Any]:
    contributors: dict[str, dict] = {}
    recordings: list[dict] = []
    qc = {"accepted": 0, "pending": 0, "needs_review": 0, "rejected": 0}
    accepted_ms = 0
    unknown_accepted = False
    stored = 0
    seen_ids: set[int] = set()
    classified_count = 0
    unclassified_count = 0
    human_accepted_ms = 0
    human_unknown_accepted = False
    human_accepted_count = 0
    human_rejected_count = 0
    for row in rows:
        if len(row) == 11:
            (rid, pid, received, length, file_status, qcs, reason, duplicate_of,
             participant_status, is_test, batch) = row
            prompt_type = None
            human_decision = human_reviewed_at = human_operation_key = None
        elif len(row) == 12:
            (rid, pid, received, length, file_status, qcs, reason, duplicate_of,
             participant_status, is_test, batch, prompt_type) = row
            human_decision = human_reviewed_at = human_operation_key = None
        elif len(row) == 15:
            (rid, pid, received, length, file_status, qcs, reason, duplicate_of,
             participant_status, is_test, batch, prompt_type,
             human_decision, human_reviewed_at, human_operation_key) = row
        else:
            raise ValueError("Unexpected production record structure")
        if not pid or not isinstance(rid, int) or rid in seen_ids:
            raise ValueError("Missing contributor or duplicate/invalid recording ID")
        seen_ids.add(rid)
        if qcs not in qc:
            raise ValueError("Unexpected QC status from production")
        qc[qcs] += 1
        if file_status == "stored":
            stored += 1
        ms = _duration_ms(length)
        if file_status == "stored" and qcs == "accepted" and is_test is False and duplicate_of is None:
            if ms is None:
                unknown_accepted = True
            else:
                accepted_ms += ms
        if human_decision not in (None, 'accept', 'reject'):
            raise ValueError('Invalid human decision')
        if human_decision == 'accept':
            human_accepted_count += 1
            if file_status == 'stored' and is_test is False and duplicate_of is None:
                if ms is None:
                    human_unknown_accepted = True
                else:
                    human_accepted_ms += ms
        elif human_decision == 'reject':
            human_rejected_count += 1
        if isinstance(prompt_type, str) and prompt_type.strip():
            classified_count += 1
        else:
            unclassified_count += 1
        if isinstance(received, datetime):
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            received_utc = received.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            raise ValueError("Invalid production UTC timestamp")
        contributors[pid] = {
            "participant_id": pid,
            "status": str(participant_status or "unknown"),
            "is_test": bool(is_test),
            "batch": str(batch or ""),
        }
        recordings.append({
            "id": rid, "participant_id": pid, "received_at": received_utc,
            "duration_ms": ms, "file_status": file_status if file_status in
               ("pending", "stored", "failed", "deleted", "missing", "unknown") else "unknown",
            "qc_status": qcs, "qc_reason": str(reason or "")[:150],
            "is_test": bool(is_test), "duplicate_of": duplicate_of,
            "human_decision": human_decision,
            "human_reviewed_at": (human_reviewed_at.astimezone(timezone.utc).isoformat().replace("+00:00","Z")
                                  if isinstance(human_reviewed_at, datetime) else None),
            "human_operation_key": str(human_operation_key) if human_operation_key is not None else None,
            # Only classification, not actual prompt text or participant data.
            "prompt_type": (str(prompt_type).strip().lower()[:48]
                            if isinstance(prompt_type, str) and prompt_type.strip()
                            else None),
        })
    return {"snapshot": {
        "stored": stored, "total_rows": len(rows),
        "accepted_ms": None if unknown_accepted else accepted_ms, "qc": qc,
        "human_review_available": bool(human_review_available),
        "human_accepted_ms": (None if not human_review_available or human_unknown_accepted else human_accepted_ms),
        "human_accepted_count": human_accepted_count,
        "human_rejected_count": human_rejected_count,
        "prompt_metadata_available": bool(prompt_metadata_available),
        "classified_recordings": classified_count,
        "unclassified_recordings": unclassified_count,
    }, "contributors": list(contributors.values()), "recordings": recordings}


@app.get("/v1/supervision/snapshot")
def snapshot(request: Request):
    dsn, _ = _verified(request)
    try:
        with _conn(dsn) as conn:
            with conn.cursor() as cur:
                # Strictly classification metadata: never SELECT prompt text or private contacts.
                # Both relationships are used by the collector. A recording may have a
                # direct assignment_id and/or be the currently answered recording on
                # assignments.recording_id. Unassigned clips stay unclassified.
                cur.execute("""
                    SELECT
                      COALESCE(has_column_privilege(current_user, to_regclass('public.recordings'),
                                                   'assignment_id','SELECT'),false),
                      COALESCE(has_column_privilege(current_user, to_regclass('public.assignments'),
                                                   'id','SELECT'),false),
                      COALESCE(has_column_privilege(current_user, to_regclass('public.assignments'),
                                                   'recording_id','SELECT'),false),
                      COALESCE(has_column_privilege(current_user, to_regclass('public.assignments'),
                                                   'prompt_type','SELECT'),false)
                """)
                recording_link, assignment_id, reverse_link, prompt_col = (bool(x) for x in cur.fetchone())
                direct_ok = recording_link and assignment_id and prompt_col
                reverse_ok = reverse_link and prompt_col
                has_assignments = direct_ok or reverse_ok
                human_ok = False
                cur.execute("""SELECT COALESCE(has_column_privilege(current_user,
                     to_regclass('public.qc_human_reviews'),'recording_id','SELECT'),false)
                     AND COALESCE(has_column_privilege(current_user,
                     to_regclass('public.qc_human_reviews'),'decision','SELECT'),false)
                     AND COALESCE(has_column_privilege(current_user,
                     to_regclass('public.qc_human_reviews'),'created_at','SELECT'),false)
                     AND COALESCE(has_column_privilege(current_user,
                     to_regclass('public.qc_human_reviews'),'operation_key','SELECT'),false)""")
                human_ok = bool(cur.fetchone()[0])
                human_expr = 'NULL::text, NULL::timestamptz, NULL::uuid'
                human_join = ''
                if human_ok:
                    human_expr = 'h.decision, h.created_at, h.operation_key'
                    human_join = '''LEFT JOIN LATERAL (SELECT decision,created_at,operation_key FROM public.qc_human_reviews
                        WHERE recording_id=r.id ORDER BY created_at DESC,id DESC LIMIT 1) h ON TRUE'''
                prompt_expr = 'NULL::text'
                if direct_ok and reverse_ok:
                    prompt_expr = """COALESCE(
                        (SELECT a.prompt_type FROM public.assignments a
                         WHERE a.id=r.assignment_id LIMIT 1),
                        (SELECT a.prompt_type FROM public.assignments a
                         WHERE a.recording_id=r.id ORDER BY a.id DESC LIMIT 1))"""
                elif direct_ok:
                    prompt_expr = """(SELECT a.prompt_type FROM public.assignments a
                                       WHERE a.id=r.assignment_id LIMIT 1)"""
                elif reverse_ok:
                    prompt_expr = """(SELECT a.prompt_type FROM public.assignments a
                                       WHERE a.recording_id=r.id ORDER BY a.id DESC LIMIT 1)"""
                # All selected fields come from the already-authorized restricted
                # recording view plus optional assignment classification columns.
                cur.execute(f"""
                    SELECT r.id, r.participant_id, r.created_at, r.duration_s,
                           r.status, r.qc_status, r.qc_reason,
                           r.duplicate_of_recording_id,
                           p.status, p.is_test, p.batch,
                           {prompt_expr} AS prompt_type,
                           {human_expr}
                    FROM public.recordings AS r
                    INNER JOIN public.participants_public AS p
                        ON p.participant_id = r.participant_id
                    {human_join}
                    ORDER BY r.id ASC
                """)
                records = cur.fetchall()
        return JSONResponse(content=_snapshot(records, has_assignments, human_ok), headers={"Cache-Control": "no-store"})
    except ValueError:
        raise HTTPException(503, "Catalog requires operator review")
    except Exception:
        # Do not reveal DB URLs, role, SQL or filesystem details to the caller.
        raise HTTPException(503, "Restricted production catalog unavailable")




def _column_available(cur, table: str, column: str) -> bool:
    """Check real grants, not only schema existence (fail closed)."""
    cur.execute("""SELECT COALESCE(has_column_privilege(current_user,
        to_regclass(%s),%s,'SELECT'),false)""", (table, column))
    return bool(cur.fetchone()[0])


def _feature_exprs(cur) -> tuple[list[str], str, str]:
    names = ('qc_tier2','qc_tier3','qc_tier3_reason','qc_final')
    expressions = [f'r.{n}' if _column_available(cur,'public.recordings',n) else 'NULL' for n in names]
    # Tier 2 band_reason lives in qc_results history. Do not infer a tier 2 reason
    # until the owner's schema and SELECT grants are visible.
    cols = ('recording_id','band_reason','created_at','id')
    tier2_reason = ('(SELECT q.band_reason FROM public.qc_results q WHERE q.recording_id=r.id '
                    'ORDER BY q.created_at DESC,q.id DESC LIMIT 1)') if all(
        _column_available(cur, 'public.qc_results', n) for n in cols) else 'NULL'
    expressions.append(tier2_reason)
    assignment_grants = (
        _column_available(cur,'public.recordings','assignment_id'),
        _column_available(cur,'public.assignments','id'),
        _column_available(cur,'public.assignments','recording_id'),
        _column_available(cur,'public.assignments','prompt_type'),
    )
    record_link, assignment_id, reverse_link, prompt_type = assignment_grants
    if record_link and assignment_id and reverse_link and prompt_type:
        prompt = '''COALESCE((SELECT a.prompt_type FROM public.assignments a WHERE a.id=r.assignment_id LIMIT 1),
              (SELECT a.prompt_type FROM public.assignments a WHERE a.recording_id=r.id ORDER BY a.id DESC LIMIT 1))'''
    elif record_link and assignment_id and prompt_type:
        prompt = '(SELECT a.prompt_type FROM public.assignments a WHERE a.id=r.assignment_id LIMIT 1)'
    elif reverse_link and prompt_type:
        prompt = '(SELECT a.prompt_type FROM public.assignments a WHERE a.recording_id=r.id ORDER BY a.id DESC LIMIT 1)'
    else:
        prompt = 'NULL'
    human_cols = ('recording_id','decision','created_at','operation_key','id')
    human = all(_column_available(cur,'public.qc_human_reviews',n) for n in human_cols)
    human_join = ('''LEFT JOIN LATERAL (
        SELECT h.decision,h.operation_key,h.created_at
        FROM public.qc_human_reviews h WHERE h.recording_id=r.id
        ORDER BY h.created_at DESC,h.id DESC LIMIT 1) h ON TRUE''' if human else '')
    expressions.extend(('h.decision','h.operation_key::text','h.created_at') if human else ('NULL','NULL','NULL'))
    return expressions, prompt, human_join


def _aggregate_ms(value):
    if value is None:
        return None
    return max(0, int((Decimal(str(value))*Decimal(1000)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _changed_record(row):
    (rid,pid,created,updated,duration_s,file_status,qc_status,qc_reason,duplicate,
     part_status,is_test,batch,tier2,tier3,tier3reason,final,band_reason,
     prompt_type,human_decision,human_operation_key,human_reviewed_at) = row
    return dict(id=rid,participant_id=pid,received_at=_as_utc(created),
       updated_at=_as_utc(updated),duration_ms=_duration_ms(duration_s),
       file_status=file_status,qc_status=qc_status,qc_reason=(qc_reason or '')[:150],
       duplicate_of=duplicate,is_test=bool(is_test),prompt_type=prompt_type,
       participant_status=part_status,participant_batch=batch,
       qc_tier2=tier2,qc_tier3=tier3,qc_tier3_reason=tier3reason,
       qc_final=final,band_reason=band_reason,
       human_decision=human_decision,human_operation_key=human_operation_key,
       human_reviewed_at=_as_utc(human_reviewed_at))


def _iso_param(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    if len(value) > 40:
        raise HTTPException(422,'Invalid synchronization timestamp')
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('No timezone')
        return parsed.astimezone(timezone.utc)
    except (TypeError,ValueError):
        raise HTTPException(422,'Invalid synchronization timestamp')


@app.get('/v1/supervision/changes')
def changes(request: Request, after: str = '1970-01-01T00:00:00Z', after_id: int = 0,
            until: str | None = None, limit: int = 500):
    """Bounded keyset paging by (updated_at,id). No 10,000-row catalogue limit.

    A fixed upper watermark prevents rows created mid-page from being skipped;
    they are collected on the next sync. Cache cursor only AFTER committing a
    Hostinger page. Owner's QC triggers must advance recordings.updated_at.
    """
    dsn, _ = _verified(request)
    if limit < 1 or limit > 500 or after_id < 0 or after_id >= 2**63:
        raise HTTPException(422,'Invalid synchronization cursor')
    lower=_iso_param(after,datetime(1970,1,1,tzinfo=timezone.utc))
    upper=_iso_param(until,datetime.now(timezone.utc))
    if upper < lower or upper > datetime.now(timezone.utc).replace(microsecond=999999):
        raise HTTPException(422,'Invalid synchronization watermark')
    try:
        with _conn(dsn) as conn:
            with conn.cursor() as cur:
                extras,prompt,join = _feature_exprs(cur)
                cur.execute(f'''SELECT r.id,r.participant_id,r.created_at,r.updated_at,
                    r.duration_s,r.status,r.qc_status,r.qc_reason,r.duplicate_of_recording_id,
                    p.status,p.is_test,p.batch,{','.join(extras[:5])}, {prompt}, {','.join(extras[5:])}
                    FROM public.recordings r
                    JOIN public.participants_public p ON p.participant_id=r.participant_id
                    {join}
                    WHERE (r.updated_at,r.id)>(%s::timestamptz,%s)
                      AND r.updated_at<=%s::timestamptz
                    ORDER BY r.updated_at ASC,r.id ASC LIMIT %s''',
                    (lower,after_id,upper,limit+1))
                rows=cur.fetchall()
        more=len(rows)>limit
        page=rows[:limit]
        items=[_changed_record(r) for r in page]
        cursor_ts=_as_utc(page[-1][3]) if page else _as_utc(lower)
        cursor_id=page[-1][0] if page else after_id
        people={r[1]:dict(participant_id=r[1],status=r[9],is_test=bool(r[10]),batch=r[11]) for r in page}
        return JSONResponse(dict(recordings=items,contributors=list(people.values()),
            cursor=dict(after=cursor_ts,after_id=cursor_id),until=_as_utc(upper),
            has_more=more,limit=limit),headers={'Cache-Control':'no-store'})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503,'Restricted incremental catalog unavailable')


@app.get('/v1/supervision/summary')
def summary(request: Request):
    """Owner-defined collected versus usable speech: never use Tier 1 acceptance as final QC."""
    dsn,_=_verified(request)
    try:
        with _conn(dsn) as conn:
            with conn.cursor() as cur:
                has_final=_column_available(cur,'public.recordings','qc_final')
                final_expr='r.qc_final' if has_final else 'NULL::text'
                cur.execute(f'''SELECT COUNT(*),COUNT(*) FILTER (WHERE r.status='stored'),
                   COALESCE(SUM(r.duration_s) FILTER (WHERE r.status='stored' AND r.qc_status <> 'rejected'),0),
                   COALESCE(SUM(r.duration_s) FILTER (WHERE r.status='stored' AND {final_expr} IN ('gold','silver')),0),
                   COALESCE(SUM(r.duration_s) FILTER (WHERE r.status='stored' AND {final_expr}='bronze'),0),
                   COALESCE(SUM(r.duration_s) FILTER (WHERE r.status='stored' AND {final_expr}='rejected'),0),
                   COUNT(*) FILTER (WHERE {final_expr}='gold'),
                   COUNT(*) FILTER (WHERE {final_expr}='silver'),
                   COUNT(*) FILTER (WHERE {final_expr}='bronze'),
                   COUNT(*) FILTER (WHERE {final_expr}='rejected')
                   FROM public.recordings r''')
                total,stored,collected,usable,bronze,rejected,gold_n,silver_n,bronze_n,rejected_n=cur.fetchone()
                cur.execute('SELECT COUNT(*) FROM public.participants_public')
                contributors=cur.fetchone()[0]
        return JSONResponse(dict(total_rows=int(total),stored=int(stored),contributors=int(contributors),
            collected_ms=_aggregate_ms(collected),usable_ms=_aggregate_ms(usable) if has_final else None,
            bronze_ms=_aggregate_ms(bronze) if has_final else None,
            rejected_ms=_aggregate_ms(rejected) if has_final else None,
            qc_final_available=has_final,
            final_counts=dict(gold=int(gold_n),silver=int(silver_n),bronze=int(bronze_n),rejected=int(rejected_n))
            ),headers={'Cache-Control':'no-store'})
    except Exception:
        raise HTTPException(503,'Restricted production quality summary unavailable')

def _safe_audio_path(path: str | None, root: Path) -> Path | None:
    if not path or len(path) > 500 or Path(path).is_absolute() or "\\" in path:
        return None
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    if candidate.suffix.lower() not in AUDIO_MIME or candidate.suffix.lower() == '.bin':
        return None
    if not os.access(candidate, os.R_OK):
        return None
    if candidate.stat().st_size < 1 or candidate.stat().st_size > MAX_AUDIO_BYTES:
        return None
    return candidate




@app.get('/v1/supervision/revision')
def revision(request: Request):
    dsn,_=_verified(request)
    try:
        with _conn(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute('''SELECT COUNT(*),COALESCE(MAX(id),0),MAX(updated_at)
                               FROM public.recordings''')
                n,latest,updated=cur.fetchone()
                cur.execute('''SELECT COUNT(*),MD5(COALESCE(STRING_AGG(
                       participant_id || ':' || COALESCE(status,'') || ':' ||
                       COALESCE(is_test::text,'') || ':' || COALESCE(batch,''),
                       '|' ORDER BY participant_id),'')) FROM public.participants_public''')
                pc,pd=cur.fetchone()
        return JSONResponse(dict(recordings=int(n),newest_id=int(latest),
            latest_update=_as_utc(updated) or '',participants=int(pc),
            participant_digest=pd),headers={'Cache-Control':'no-store'})
    except Exception:
        raise HTTPException(503,'Restricted production revision unavailable')


def _audio_lookup(recording_id: int, dsn: str, root: Path):
    try:
        with _conn(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.status, r.original_path, r.wav_path
                    FROM public.recordings r
                    INNER JOIN public.participants_public p
                      ON p.participant_id=r.participant_id
                    WHERE r.id=%s LIMIT 1
                """, (recording_id,))
                item = cur.fetchone()
    except Exception:
        raise HTTPException(503, "Restricted audio lookup unavailable")
    if not item or item[0] != 'stored':
        raise HTTPException(404, "Recording is not stored")
    # Prefer standard WAV for browser decoding; compact original is a fallback.
    paths = [item[2], item[1]]
    candidate = next((p for p in (_safe_audio_path(path, root) for path in paths) if p is not None), None)
    if candidate is None:
        # Distinguish unreadable existing media from missing media without
        # disclosing a file path, participant identity, or server directory.
        for path in paths:
            if not path or Path(path).is_absolute() or '\\' in path:
                continue
            test_path = (root / path).resolve()
            if test_path.is_relative_to(root) and test_path.is_file() and not os.access(test_path, os.R_OK):
                raise HTTPException(503, "Stored audio is not readable by gateway process")
        raise HTTPException(404, "Stored audio file unavailable")
    return candidate


@app.get("/v1/supervision/audio/{recording_id}/status")
def audio_status(recording_id: int, request: Request):
    dsn, root = _verified(request)
    if recording_id < 1:
        raise HTTPException(404, "Recording unavailable")
    file = _audio_lookup(recording_id, dsn, root)
    return JSONResponse({"available": True, "bytes": file.stat().st_size,
                         "mime": AUDIO_MIME[file.suffix.lower()]},
                        headers={"Cache-Control": "no-store"})

@app.get("/v1/supervision/audio/{recording_id}")
def audio(recording_id: int, request: Request):
    dsn, root = _verified(request)
    if recording_id < 1:
        raise HTTPException(404, "Recording unavailable")
    file = _audio_lookup(recording_id, dsn, root)
    return FileResponse(file, media_type=AUDIO_MIME[file.suffix.lower()],
                        headers={"Cache-Control": "private, no-store",
                                 "X-Content-Type-Options": "nosniff"})


@app.get("/v1/supervision/health")
def health(request: Request):
    # Authenticated health only: no anonymous production metadata.
    _verified(request)
    return {"status": "ok", "version": VERSION}

# Production writes are isolated in a disabled-by-default, owner-approved module.
from writeback import register as _register_qc_writeback
_register_qc_writeback(app)

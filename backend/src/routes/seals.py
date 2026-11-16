from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.config.database import get_db
from src.middlewares.auth import can_write, current_user
from src.services import seals
from src.services.audit import record_seal

router = APIRouter(prefix='/audit-seals')


class PackDocument(BaseModel):
    document: dict[str, Any] = Field(description='待验真的封存包 JSON 对象')


class SealRequest(BaseModel):
    note: str = Field(default='', max_length=200)


@router.get('')
def listing(db=Depends(get_db), user=Depends(current_user)):
    return seals.listing(db)


@router.post('', status_code=201)
def create(data: SealRequest, db=Depends(get_db), user=Depends(can_write)):
    pack = seals.create_seal(db, user.email)
    record_seal(db, user.email, 'audit.seal.created',
                {'pack_no': pack.pack_no, 'items': pack.item_count,
                 'from': pack.first_audit_id, 'to': pack.last_audit_id, 'note': data.note})
    db.commit()
    return seals.listing(db)


@router.post('/{id}/retry')
def retry(id: int, db=Depends(get_db), user=Depends(can_write)):
    pack = seals.retry_seal(db, id, user.email)
    record_seal(db, user.email, 'audit.seal.retried', {'pack_no': pack.pack_no, 'items': pack.item_count})
    db.commit()
    return seals.listing(db)


@router.get('/{id}/download')
def download(id: int, db=Depends(get_db), user=Depends(current_user)):
    pack, path = seals.sealed_file(db, id)
    record_seal(db, user.email, 'audit.seal.downloaded', {'pack_no': pack.pack_no})
    db.commit()
    return FileResponse(path, filename=pack.file_name, media_type='application/json')


@router.get('/verify')
def verify_chain(db=Depends(get_db), user=Depends(current_user)):
    return seals.verify_chain()


@router.post('/verify')
def verify_document(data: PackDocument, db=Depends(get_db), user=Depends(current_user)):
    result = seals.verify_document(data.document)
    record_seal(db, user.email, 'audit.seal.verified',
                {'pack_no': result.get('pack_no'), 'ok': bool(result.get('ok'))})
    db.commit()
    return result


@router.post('/index/rebuild')
def rebuild_index(db=Depends(get_db), user=Depends(can_write)):
    document = seals.rebuild_index()
    record_seal(db, user.email, 'audit.seal.index', {'packs': len(document['packs'])})
    db.commit()
    return {'packs': len(document['packs']), 'index_hash': document['index_hash']}

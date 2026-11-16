import time
from src.models import Audit, AuditSealEvent


def record(db, actor, action, detail=None):
    """写入业务审计（进入封存包范围）。"""
    db.add(Audit(actor=actor, action=action, detail=detail or {}, at=time.time()))


def record_seal(db, actor, action, detail=None):
    """写入封存管理流水；不属于业务动作，不进入封存包，避免自指追尾。"""
    db.add(AuditSealEvent(actor=actor, action=action, detail=detail or {}, at=time.time()))

from sqlalchemy import String, Integer, Double, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class AuditSealEvent(Base):
    """封存管理动作（创建、重试、下载、重建索引）的独立流水。

    封存包只覆盖业务审计（audits）；若把封存动作也写进 audits，就会产生永远追不上的
    “待封存动作”和只含封存动作的空包。因此封存动作记录在本表中，不进入封存范围。
    """
    __tablename__ = 'audit_seal_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(150))
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[float] = mapped_column(Double)


class AuditSealState(Base):
    """单独登记的封存头，用于核对封存包链，不依赖包内自述。"""
    __tablename__ = 'audit_seal_state'
    id: Mapped[int] = mapped_column(primary_key=True)
    last_pack_no: Mapped[int] = mapped_column(Integer, default=0)
    last_header_hash: Mapped[str] = mapped_column(String(64), default='0' * 64)
    last_audit_id: Mapped[int] = mapped_column(Integer, default=0)
    pending_pack_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[float] = mapped_column(Double, default=0.0)


class AuditSeal(Base):
    __tablename__ = 'audit_seals'
    id: Mapped[int] = mapped_column(primary_key=True)
    pack_no: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    first_audit_id: Mapped[int] = mapped_column(Integer)
    last_audit_id: Mapped[int] = mapped_column(Integer)
    item_count: Mapped[int] = mapped_column(Integer)
    prev_pack_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prev_header_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    header_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    boundary_audit_id: Mapped[int] = mapped_column(Integer, default=0)
    file_name: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[float] = mapped_column(Double)
    sealed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(1000), default='')


class AuditSealItem(Base):
    """封存时的动作快照；audit_id 全局唯一，保证两次封存不交叠。"""
    __tablename__ = 'audit_seal_items'
    __table_args__ = (UniqueConstraint('audit_id', name='uq_audit_seal_item_audit'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    seal_id: Mapped[int] = mapped_column(ForeignKey('audit_seals.id'), index=True)
    audit_id: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(150))
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[float] = mapped_column(Double)
    item_hash: Mapped[str] = mapped_column(String(64))

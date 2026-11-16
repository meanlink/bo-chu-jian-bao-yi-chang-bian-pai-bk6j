"""连续审计封存包：按已提交顺序快照审计动作，形成可下载、可验真的证据链。

设计要点：
- 审计记录（audits）按自增 id 表示提交顺序；封存包只覆盖开启封存时已提交的范围，
  封存期间新产生的动作留给下一包。
- 全局状态行 audit_seal_state 是“已登记封存头”：记录最后包号、最后包头指纹与最后审计 id；
  磁盘索引 audit-seals/index.json 是同一封存头的镜像。验真同时核对两者与封存快照，
  不依赖包内自述指纹。
- 状态行的 pending_pack_no 是并发与重试的闸门：同一时间只允许一包在途；重试复用同一
  包号与快照，写入中断不会重复出包。
- 包文件先写临时文件再原子替换；未 sealed 且未登记到封存头的包不可下载。
"""
import hashlib
import json
import os
import tempfile
import time

from fastapi import HTTPException
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError

from src.config.database import Session
from src.config.settings import ARTIFACT_DIR
from src.models import Audit, AuditSeal, AuditSealItem, AuditSealState

FORMAT = 'audit-seal/v1'
INDEX_FORMAT = 'audit-seal-index/v1'
GENESIS = '0' * 64
SEAL_DIR = 'audit-seals'
INDEX_FILE = 'index.json'
RECORD_KEYS = ('audit_id', 'actor', 'action', 'detail', 'at')


def canonical(value):
    """稳定 JSON 序列化，确保跨进程、跨重试得到同一指纹。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _record(audit_id, actor, action, detail, at):
    return {'audit_id': int(audit_id), 'actor': actor, 'action': action,
            'detail': detail or {}, 'at': float(at)}


def _item_payload(item):
    return {'audit_id': item.audit_id, 'actor': item.actor, 'action': item.action,
            'detail': item.detail or {}, 'at': item.at, 'position': item.position,
            'item_hash': item.item_hash}


def _snapshot(rows, seed):
    """逐条哈希链：第 n 条绑定第 n-1 条，首条绑定前包包头指纹。"""
    previous, output = seed, []
    for position, row in enumerate(rows, 1):
        record = _record(row.id, row.actor, row.action, row.detail, row.at)
        item_hash = digest({'previous': previous, 'record': record})
        output.append({**record, 'position': position, 'item_hash': item_hash})
        previous = item_hash
    return output


def _file_name(pack_no):
    return f'audit-seal-{pack_no:06d}.json'


def seal_dir():
    path = os.path.join(ARTIFACT_DIR, SEAL_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def pack_path(seal):
    return os.path.join(seal_dir(), seal.file_name)


def _atomic_write(path, text):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=directory, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _load(path):
    with open(path, 'r', encoding='utf-8') as stream:
        return json.load(stream)


# ---------- 登记封存头与索引 ----------

def _state(db):
    state = db.scalar(select(AuditSealState).order_by(AuditSealState.id))
    if state is not None:
        return state
    state = AuditSealState(id=1, last_pack_no=0, last_header_hash=GENESIS, last_audit_id=0,
                           pending_pack_no=None, updated_at=time.time())
    db.add(state)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        state = db.scalar(select(AuditSealState).order_by(AuditSealState.id))
    return state


def _last_committed_audit(db):
    return db.scalar(select(func.max(Audit.id))) or 0


def _db_items(db, seal):
    return list(db.scalars(select(AuditSealItem).where(AuditSealItem.seal_id == seal.id)
                           .order_by(AuditSealItem.position)))


def _sealed_chain(db, state):
    return list(db.scalars(select(AuditSeal)
                           .where(AuditSeal.status == 'sealed', AuditSeal.pack_no <= state.last_pack_no)
                           .order_by(AuditSeal.pack_no)))


def _sealed_head(seals):
    if not seals:
        return {'last_pack_no': 0, 'last_header_hash': GENESIS, 'last_audit_id': 0}
    last = seals[-1]
    return {'last_pack_no': last.pack_no, 'last_header_hash': last.header_hash,
            'last_audit_id': last.last_audit_id}


def _index_document(seals):
    body = {'format': INDEX_FORMAT, 'registered_head': _sealed_head(seals),
            'packs': [{'pack_no': seal.pack_no, 'file': seal.file_name, 'status': seal.status,
                       'item_count': seal.item_count, 'first_audit_id': seal.first_audit_id,
                       'last_audit_id': seal.last_audit_id, 'prev_pack_no': seal.prev_pack_no,
                       'prev_header_hash': seal.prev_header_hash, 'header_hash': seal.header_hash,
                       'content_hash': seal.content_hash, 'sealed_at': seal.sealed_at} for seal in seals]}
    return {**body, 'index_hash': digest(body)}


def _write_index(db, state):
    document = _index_document(_sealed_chain(db, state))
    _atomic_write(os.path.join(seal_dir(), INDEX_FILE), json.dumps(document, ensure_ascii=False, indent=1))
    return document


# ---------- 封存包文档 ----------

def _chain_records(records, seed):
    previous, output = seed, []
    for position, record in enumerate(records, 1):
        item_hash = digest({'previous': previous, 'record': record})
        output.append({**record, 'position': position, 'item_hash': item_hash})
        previous = item_hash
    return output


def build_seal(seal, items):
    """组装封存包文档；纯函数，写入失败后可用同一快照幂等重试。"""
    records = [{key: getattr(item, key) for key in RECORD_KEYS} for item in items]
    body = {
        'format': FORMAT,
        'pack_no': seal.pack_no,
        'status': 'sealed',
        'scope': {
            'audit_id_from': seal.first_audit_id,
            'audit_id_to': seal.last_audit_id,
            'item_count': len(items),
            'boundary_audit_id': seal.boundary_audit_id,
        },
        'prev': {'pack_no': seal.prev_pack_no, 'header_hash': seal.prev_header_hash},
        'chain_seed': seal.prev_header_hash,
        'items': _chain_records(records, seal.prev_header_hash),
        'sealed_at': seal.sealed_at,
        'created_by': seal.created_by,
    }
    body['content_hash'] = digest({key: body[key] for key in ('scope', 'prev', 'chain_seed', 'items')})
    body['header'] = {'pack_no': seal.pack_no, 'prev_header_hash': seal.prev_header_hash,
                      'content_hash': body['content_hash'], 'sealed_at': seal.sealed_at}
    body['header_hash'] = digest(body['header'])
    return body


def _item_row(item):
    return {'audit_id': item['audit_id'], 'position': item['position'], 'actor': item['actor'],
            'action': item['action'], 'detail': item['detail'], 'at': item['at'],
            'item_hash': item['item_hash']}


def _finalize(db, state, seal):
    """把在途包写成可下载的封存包，并推进登记封存头。"""
    seal.sealed_at = time.time()
    document = build_seal(seal, _db_items(db, seal))
    _atomic_write(pack_path(seal), json.dumps(document, ensure_ascii=False, indent=1))
    seal.status = 'sealed'
    seal.item_count = document['scope']['item_count']
    seal.content_hash = document['content_hash']
    seal.header_hash = document['header_hash']
    seal.error = ''
    registered = db.execute(update(AuditSealState)
                            .where(AuditSealState.id == state.id,
                                   AuditSealState.pending_pack_no == seal.pack_no,
                                   AuditSealState.last_pack_no == seal.pack_no - 1)
                            .values(last_pack_no=seal.pack_no, last_header_hash=seal.header_hash,
                                    last_audit_id=seal.last_audit_id, pending_pack_no=None,
                                    updated_at=time.time()))
    if registered.rowcount != 1:
        raise HTTPException(409, '封存头已被其他操作更新，请刷新后重试')
    state.last_pack_no = seal.pack_no
    state.last_header_hash = seal.header_hash
    state.last_audit_id = seal.last_audit_id
    state.pending_pack_no = None
    return document


def _store_index(db, state):
    """已封存后刷新索引；索引可从登记头随时重建，失败不回滚封存。"""
    try:
        _write_index(db, state)
    except Exception:
        pass


# ---------- 验真 ----------

def _verify_pack(seal, document):
    """用登记字段核对单个包文件；不信任包内自带的指纹。"""
    if not isinstance(document, dict):
        return ['包文件不是有效的封存包对象']
    problems = []
    if document.get('format') != FORMAT:
        problems.append('包格式标识不正确')
    if document.get('pack_no') != seal.pack_no:
        problems.append(f"包号不符：文件为 {document.get('pack_no')}，登记为 {seal.pack_no}")
    if document.get('status') != 'sealed':
        problems.append('包状态不是已封存')
    scope = document.get('scope') or {}
    if scope.get('audit_id_from') != seal.first_audit_id or scope.get('audit_id_to') != seal.last_audit_id:
        problems.append('包内范围与登记范围不一致')
    if scope.get('item_count') != seal.item_count:
        problems.append(f"包内声明条数 {scope.get('item_count')} 与登记条数 {seal.item_count} 不一致")
    if document.get('prev') != {'pack_no': seal.prev_pack_no, 'header_hash': seal.prev_header_hash}:
        problems.append('包内前包指纹与登记不一致')
    expected_content = digest({key: document.get(key) for key in ('scope', 'prev', 'chain_seed', 'items')})
    if document.get('content_hash') != expected_content:
        problems.append('包内内容指纹与包内容不一致，记录可能被改写')
    if document.get('content_hash') != seal.content_hash:
        problems.append('包内内容指纹与已登记内容指纹不一致')
    expected_header = digest({'pack_no': document.get('pack_no'),
                              'prev_header_hash': (document.get('prev') or {}).get('header_hash'),
                              'content_hash': document.get('content_hash'),
                              'sealed_at': document.get('sealed_at')})
    if document.get('header_hash') != expected_header:
        problems.append('包内登记头指纹与包内容不一致')
    if document.get('header_hash') != seal.header_hash:
        problems.append('包内登记头指纹与已登记封存头不一致')
    items = document.get('items') or []
    if len(items) != seal.item_count:
        problems.append(f"包内实际条数 {len(items)} 与登记条数 {seal.item_count} 不一致")
    previous = seal.prev_header_hash
    previous_id = seal.first_audit_id - 1
    for position, item in enumerate(items, 1):
        if not isinstance(item, dict):
            problems.append(f'第 {position} 条不是有效记录')
            continue
        record = {key: item.get(key) for key in RECORD_KEYS}
        expected = digest({'previous': previous, 'record': record})
        if item.get('position') != position:
            problems.append(f'第 {position} 条位置标识不连续')
        audit_id = item.get('audit_id')
        if not isinstance(audit_id, int) or not seal.first_audit_id <= audit_id <= seal.last_audit_id:
            problems.append(f'第 {position} 条审计编号不在登记范围 #{seal.first_audit_id}–#{seal.last_audit_id} 内')
        elif audit_id <= previous_id:
            problems.append(f'第 {position} 条审计编号未按提交顺序排列')
        if item.get('item_hash') != expected:
            problems.append(f'第 {position} 条（审计 #{audit_id}）动作指纹不匹配，记录可能被改写')
        previous = expected
        if isinstance(audit_id, int):
            previous_id = audit_id
    return problems


def _compare_snapshot(db, seal, document):
    """包文件与封存时落库的快照逐条比对。"""
    problems = []
    items, records = _db_items(db, seal), document.get('items') or []
    if [item.audit_id for item in items] != [item.get('audit_id') if isinstance(item, dict) else None for item in records]:
        problems.append('包文件记录的审计编号与封存快照不一致')
        return problems
    for stored, snapshot in zip(items, records):
        if any(snapshot.get(key) != value for key, value in _item_payload(stored).items()):
            problems.append(f'封存快照第 {stored.position} 条与包文件不一致')
    return problems


def _compare_audits(db, seal):
    """核对已封存范围内的原审计记录是否仍在、是否被改写。"""
    items = _db_items(db, seal)
    if not items:
        return []
    ids = [item.audit_id for item in items]
    rows = {row.id: row for row in db.scalars(select(Audit).where(Audit.id >= ids[0], Audit.id <= ids[-1]))}
    missing = [audit_id for audit_id in ids if audit_id not in rows]
    problems = []
    if missing:
        problems.append(f'已封存范围内的审计记录缺失：#{missing[0]}' + (f' 等 {len(missing)} 条' if len(missing) > 1 else ''))
    for item in items:
        row = rows.get(item.audit_id)
        if row and _record(row.id, row.actor, row.action, row.detail, row.at) != \
                _record(item.audit_id, item.actor, item.action, item.detail, item.at):
            problems.append(f'审计 #{item.audit_id} 的原始记录已被改写')
            break
    return problems


def _coverage_problems(db, state):
    """已登记范围内：原始审计不得漏出快照，快照条目不得指向已删除的审计。"""
    problems = []
    if not state.last_audit_id:
        return problems
    uncovered = db.scalar(select(func.count()).select_from(Audit)
                          .where(Audit.id <= state.last_audit_id,
                                 ~exists().where(AuditSealItem.audit_id == Audit.id))) or 0
    if uncovered:
        first = db.scalar(select(func.min(Audit.id)).select_from(Audit)
                          .where(Audit.id <= state.last_audit_id,
                                 ~exists().where(AuditSealItem.audit_id == Audit.id)))
        problems.append(f'审计 #{first} 等 {uncovered} 条已提交动作未进入任何封存包：范围存在断点')
    orphan = db.scalar(select(func.count()).select_from(AuditSealItem)
                       .join(AuditSeal, AuditSeal.id == AuditSealItem.seal_id)
                       .where(AuditSeal.status == 'sealed',
                              ~exists().where(Audit.id == AuditSealItem.audit_id))) or 0
    if orphan:
        first = db.scalar(select(func.min(AuditSealItem.audit_id)).select_from(AuditSealItem)
                          .join(AuditSeal, AuditSeal.id == AuditSealItem.seal_id)
                          .where(AuditSeal.status == 'sealed',
                                 ~exists().where(Audit.id == AuditSealItem.audit_id)))
        problems.append(f'已封存审计 #{first} 等 {orphan} 条原始记录已被删除')
    return problems


def _index_problems(state, sealed):
    """磁盘索引是登记封存头的独立镜像；滞后的索引只是提示，超前的索引说明封存头被回退。"""
    path = os.path.join(seal_dir(), INDEX_FILE)
    if not os.path.exists(path):
        return [], ['磁盘封存索引缺失，可重建（不影响封存包正文）']
    try:
        document = _load(path)
    except Exception:
        return [], ['磁盘封存索引无法解析，可重建']
    body = {key: document.get(key) for key in ('format', 'registered_head', 'packs')}
    if document.get('format') != INDEX_FORMAT or document.get('index_hash') != digest(body):
        return [], ['磁盘封存索引自身校验失败，可能被改写']
    index_head = document.get('registered_head') or {}
    if (index_head.get('last_pack_no') or 0) > state.last_pack_no:
        return [f"磁盘封存索引登记的最后包为第 {index_head.get('last_pack_no')} 包，"
                f"但数据库封存头只到第 {state.last_pack_no} 包：封存头被移除或回退"], []
    if (index_head.get('last_pack_no') or 0) == state.last_pack_no and \
            index_head.get('last_header_hash') != state.last_header_hash and state.last_pack_no:
        return ['磁盘封存索引的封存头指纹与数据库登记不一致：封存头可能被替换'], []
    expected = _index_document(_sealed_chain_rows(sealed))
    if index_head != expected['registered_head']:
        return [], ['磁盘封存索引登记的封存头与数据库登记不一致']
    if [pack.get('pack_no') for pack in document.get('packs') or []] != \
            [pack['pack_no'] for pack in expected['packs']]:
        return [], ['磁盘封存索引的包列表与数据库登记不一致']
    return [], []


def _sealed_chain_rows(seals):
    """索引镜像只需要已封存且已登记进头部的包。"""
    head = _sealed_head(seals)
    return [seal for seal in seals
            if seal.pack_no <= head['last_pack_no'] and seal.header_hash]


def verify_document(document):
    """验真一个包文件：与已登记封存头逐项核对，并定位前后断点。"""
    pack_no = document.get('pack_no') if isinstance(document, dict) else None
    if not isinstance(pack_no, int) or isinstance(pack_no, bool):
        return {'ok': False, 'pack_no': pack_no, 'problems': ['包文件缺少有效的包号，无法与登记封存头核对']}
    with Session() as db:
        state = _state(db)
        seal = db.scalar(select(AuditSeal).where(AuditSeal.pack_no == pack_no))
        if seal is None or seal.status != 'sealed' or not seal.header_hash or pack_no > state.last_pack_no:
            return {'ok': False, 'pack_no': pack_no,
                    'problems': [f'已登记封存头中没有第 {pack_no} 包的登记信息，无法确认来源']}
        problems = _verify_pack(seal, document)
        problems += _compare_snapshot(db, seal, document)
        broken = _broken_packs(db, state)
        before = [pack_no_ for pack_no_ in broken if pack_no_ < pack_no]
        after = [pack_no_ for pack_no_ in broken if pack_no_ > pack_no]
        if before:
            problems.append(f'该包之前的第 {before[0]} 包已存在断点')
        if after:
            problems.append(f'该包之后的第 {after[0]} 包已存在断点')
        return {'ok': not problems, 'pack_no': pack_no, 'item_count': seal.item_count,
                'header_hash': seal.header_hash, 'prev_header_hash': seal.prev_header_hash,
                'sealed_at': seal.sealed_at, 'problems': problems}


def _broken_packs(db, state):
    sealed = _sealed_chain(db, state)
    broken, expected, previous_hash, last_range = [], 1, GENESIS, 0
    for seal in sealed:
        if (seal.pack_no != expected or seal.prev_header_hash != previous_hash
                or (seal.pack_no == 1 and seal.first_audit_id != 1)
                or seal.first_audit_id <= last_range
                or not os.path.exists(pack_path(seal))):
            broken.append(seal.pack_no)
        expected, previous_hash, last_range = seal.pack_no + 1, seal.header_hash, seal.last_audit_id
    return broken


def verify_chain():
    """从已登记封存头回推整条链，发现记录改写、误删、尾部整包移除和包间断点。"""
    with Session() as db:
        state = _state(db)
        last_committed = _last_committed_audit(db)
        seals = list(db.scalars(select(AuditSeal).order_by(AuditSeal.pack_no)))
        sealed = [seal for seal in seals if seal.status == 'sealed' and seal.pack_no <= state.last_pack_no]
        problems, packs = [], []
        expected, previous_hash, last_range = 1, GENESIS, 0
        for seal in sealed:
            entry = {'pack_no': seal.pack_no, 'file': seal.file_name, 'ok': False,
                     'item_count': seal.item_count, 'first_audit_id': seal.first_audit_id,
                     'last_audit_id': seal.last_audit_id, 'header_hash': seal.header_hash,
                     'sealed_at': seal.sealed_at, 'problems': []}
            if seal.pack_no != expected:
                entry['problems'].append(f'包号不连续：应为第 {expected} 包')
                problems.append(f'第 {seal.pack_no} 包：封存包序号不连续，缺少第 {expected} 包')
            if seal.prev_header_hash != previous_hash:
                entry['problems'].append('前包指纹与上一已封存包不一致')
                problems.append(f'第 {seal.pack_no} 包：前包指纹与上一包不一致，链在此断开')
            if seal.first_audit_id <= last_range:
                entry['problems'].append(f'审计范围与上一包重叠或倒置（上一包止于 #{last_range}）')
                problems.append(f'第 {seal.pack_no} 包：审计范围从 #{seal.first_audit_id} 开始，'
                                f'与上一包止点 #{last_range} 重叠')
            path = pack_path(seal)
            if not os.path.exists(path):
                entry['problems'].append('包文件缺失或已被移除')
                problems.append(f'第 {seal.pack_no} 包：包文件缺失或已被移除')
            else:
                try:
                    document = _load(path)
                except Exception:
                    entry['problems'].append('包文件无法解析')
                    problems.append(f'第 {seal.pack_no} 包：包文件无法解析')
                else:
                    detail = _verify_pack(seal, document) + _compare_snapshot(db, seal, document)
                    entry['problems'].extend(detail)
                    if detail:
                        problems.append(f'第 {seal.pack_no} 包：内容验真失败，' + '；'.join(detail[:3]))
            detail = _compare_audits(db, seal)
            entry['problems'].extend(detail)
            if detail:
                problems.append(f'第 {seal.pack_no} 包：' + '；'.join(detail[:2]))
            entry['ok'] = not entry['problems']
            packs.append(entry)
            expected, previous_hash, last_range = seal.pack_no + 1, seal.header_hash, seal.last_audit_id

        beyond_head = [seal.pack_no for seal in seals
                       if seal.status == 'sealed' and seal.pack_no > state.last_pack_no]
        if beyond_head:
            problems.append(f'封存登记显示第 {beyond_head[0]} 包已封存，但数据库封存头回退到第 '
                            f'{state.last_pack_no} 包：第 {beyond_head[0]} 包起的登记被移除')
        registered_last = sealed[-1].pack_no if sealed else 0
        if state.last_pack_no != registered_last:
            problems.append(f'已登记封存头记录最后为第 {state.last_pack_no} 包，但索引中最后只到第 {registered_last} 包：'
                            f'尾部第 {registered_last + 1} 至 {state.last_pack_no} 包被移除')
        if sealed and state.last_header_hash != sealed[-1].header_hash:
            problems.append(f'已登记封存头指纹与最后一包不一致：尾部第 {state.last_pack_no} 包可能被替换')
        if sealed and state.last_audit_id != sealed[-1].last_audit_id:
            problems.append(f'已登记封存头记录最后审计为 #{state.last_audit_id}，但最后一包只到 '
                            f'#{sealed[-1].last_audit_id}：覆盖范围断点')
        problems.extend(_coverage_problems(db, state))
        index_problems, warnings = _index_problems(state, sealed)
        problems.extend(index_problems)
        pending = [seal for seal in seals if seal.status != 'sealed']
        if pending:
            warnings.append(f'第 {pending[0].pack_no} 包仍在封存中，尚未登记；可重试完成封存')
        if state.last_audit_id > last_committed:
            problems.append(f'已登记封存头记录审计 #{state.last_audit_id}，审计表最大仅 #{last_committed}：记录被误删')
        return {
            'ok': not problems,
            'checked_packs': len(packs),
            'checked_records': sum(pack['item_count'] for pack in packs),
            'registered_head': {'last_pack_no': state.last_pack_no,
                                'last_header_hash': state.last_header_hash,
                                'last_audit_id': state.last_audit_id},
            'last_committed_audit_id': last_committed,
            'unsealed_audit_count': db.scalar(select(func.count()).select_from(Audit)
                                               .where(Audit.id > state.last_audit_id)) or 0,
            'pending_pack_no': state.pending_pack_no,
            'break': problems[0] if problems else None,
            'problems': problems,
            'warnings': warnings,
            'packs': packs,
        }


# ---------- 对外操作 ----------

def _public(seal, state):
    return {
        'id': seal.id, 'pack_no': seal.pack_no, 'status': seal.status, 'file_name': seal.file_name,
        'first_audit_id': seal.first_audit_id, 'last_audit_id': seal.last_audit_id,
        'item_count': seal.item_count, 'prev_pack_no': seal.prev_pack_no,
        'prev_header_hash': seal.prev_header_hash, 'content_hash': seal.content_hash,
        'header_hash': seal.header_hash, 'created_by': seal.created_by,
        'created_at': seal.created_at, 'sealed_at': seal.sealed_at, 'attempts': seal.attempts,
        'error': seal.error, 'is_registered_head': state.last_pack_no == seal.pack_no,
        'downloadable': seal.status == 'sealed' and bool(seal.header_hash) and state.last_pack_no >= seal.pack_no,
    }


def _finish(db, state, seal, actor):
    """幂等完成在途包：复用包号与快照，重写文件后推进封存头。"""
    try:
        items = _db_items(db, seal)
        if not items:
            rows = list(db.scalars(select(Audit).where(Audit.id >= seal.first_audit_id,
                                                        Audit.id <= seal.last_audit_id)
                                    .order_by(Audit.id)))
            if not rows:
                seal.status = 'failed'
                seal.error = '原封存范围内的审计记录已不存在，无法完成封存'
                db.execute(update(AuditSealState)
                           .where(AuditSealState.id == state.id,
                                  AuditSealState.pending_pack_no == seal.pack_no)
                           .values(pending_pack_no=None, updated_at=time.time()))
                db.commit()
                raise HTTPException(409, seal.error)
            for item in _snapshot(rows, seal.prev_header_hash):
                db.add(AuditSealItem(seal_id=seal.id, **_item_row(item)))
            db.flush()
        _finalize(db, state, seal)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as error:
        db.rollback()
        current = db.get(AuditSeal, seal.id)
        current.attempts = (current.attempts or 0) + 1
        current.error = f'写入封存包失败，可重试：{error}'[:990]
        db.commit()
        raise HTTPException(503, current.error)
    _store_index(db, state)
    return db.get(AuditSeal, seal.id)


def create_seal(db, actor):
    """开启一包新封存：先以状态行抢占在途包号，再锁定范围、落库快照并写出包文件。"""
    state = _state(db)
    last_committed = _last_committed_audit(db)
    if state.pending_pack_no:
        raise HTTPException(409, f'第 {state.pending_pack_no} 包仍在封存中，尚未登记；请重试完成该包后再封存新动作')
    if last_committed <= state.last_audit_id:
        raise HTTPException(409, '没有新的已提交业务动作需要封存')
    pack_no = state.last_pack_no + 1
    existing = db.scalar(select(AuditSeal).where(AuditSeal.pack_no == pack_no))
    if existing is not None:
        if existing.status == 'pending':
            raise HTTPException(409, f'第 {pack_no} 包仍在封存中，尚未登记；请重试完成该包')
        if existing.status == 'sealed':
            raise HTTPException(409, '封存索引与已登记封存头不一致，请先运行整链验真')
        # 失败包从未登记、也无快照，可原地清理后复用该包号。
        for item in _db_items(db, existing):
            db.delete(item)
        db.delete(existing)
        db.flush()
    claimed = db.execute(update(AuditSealState)
                         .where(AuditSealState.id == state.id,
                                AuditSealState.last_pack_no == state.last_pack_no,
                                AuditSealState.pending_pack_no.is_(None))
                         .values(pending_pack_no=pack_no, updated_at=time.time()))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, '另一名同事正在登记新的封存包，请刷新后重试')
    state.pending_pack_no = pack_no
    rows = list(db.scalars(select(Audit).where(Audit.id > state.last_audit_id,
                                                Audit.id <= last_committed).order_by(Audit.id)))
    if not rows:
        # 抢占已提交，必须显式释放闸门，避免状态行卡在 pending。
        db.rollback()
        with Session() as release:
            release.execute(update(AuditSealState)
                            .where(AuditSealState.id == state.id,
                                   AuditSealState.pending_pack_no == pack_no)
                            .values(pending_pack_no=None, updated_at=time.time()))
            release.commit()
        raise HTTPException(409, '已提交动作在登记期间被移除，请重新打开封存页面')
    seal = AuditSeal(pack_no=pack_no, status='pending',
                     first_audit_id=rows[0].id, last_audit_id=rows[-1].id, item_count=len(rows),
                     prev_pack_no=state.last_pack_no or None, prev_header_hash=state.last_header_hash,
                     boundary_audit_id=last_committed, file_name=_file_name(pack_no),
                     created_by=actor, created_at=time.time(), attempts=0, error='')
    db.add(seal)
    try:
        db.flush()
        for item in _snapshot(rows, seal.prev_header_hash):
            db.add(AuditSealItem(seal_id=seal.id, **_item_row(item)))
        db.commit()
    except Exception:
        db.rollback()
        # 抢占失败时释放闸门，避免把状态行永久卡在 pending。
        db.execute(update(AuditSealState)
                   .where(AuditSealState.id == state.id, AuditSealState.last_pack_no == state.last_pack_no,
                          AuditSealState.pending_pack_no == pack_no)
                   .values(pending_pack_no=None))
        db.commit()
        raise
    return _finish(db, _state(db), db.get(AuditSeal, seal.id), actor)


def retry_seal(db, seal_id, actor):
    seal = db.get(AuditSeal, seal_id)
    if seal is None:
        raise HTTPException(404, '封存包不存在')
    if seal.status == 'sealed':
        return seal
    state = _state(db)
    if state.pending_pack_no != seal.pack_no:
        raise HTTPException(409, '该包不是当前在途的封存包，请刷新后重试')
    return _finish(db, state, seal, actor)


def listing(db):
    state = _state(db)
    seals = list(db.scalars(select(AuditSeal).order_by(AuditSeal.pack_no.desc())))
    last_committed = _last_committed_audit(db)
    return {
        'registered_head': {'last_pack_no': state.last_pack_no,
                            'last_header_hash': state.last_header_hash,
                            'last_audit_id': state.last_audit_id},
        'pending_pack_no': state.pending_pack_no,
        'last_committed_audit_id': last_committed,
        'unsealed_audit_count': db.scalar(select(func.count()).select_from(Audit)
                                           .where(Audit.id > state.last_audit_id)) or 0,
        'packs': [_public(seal, state) for seal in seals],
    }


def sealed_file(db, seal_id):
    seal = db.get(AuditSeal, seal_id)
    if seal is None:
        raise HTTPException(404, '封存包不存在')
    state = _state(db)
    if seal.status != 'sealed' or not seal.header_hash:
        raise HTTPException(409, '封存包尚未完成，暂不可下载；请先重试封存')
    if state.last_pack_no < seal.pack_no:
        raise HTTPException(409, '封存包尚未登记到封存头，暂不可下载')
    path = pack_path(seal)
    if not os.path.exists(path):
        raise HTTPException(404, '封存包文件缺失，请运行整链验真定位断点')
    return seal, path


def rebuild_index():
    """从登记封存头重建磁盘索引；不改变任何封存包内容。

    若封存索引中已有超出登记封存头的已封存包，说明封存头被回退，此时拒绝重建，
    以免用空索引覆盖掉“尾部被移除”的现场证据。
    """
    with Session() as db:
        state = _state(db)
        beyond = db.scalar(select(func.min(AuditSeal.pack_no))
                           .where(AuditSeal.status == 'sealed',
                                  AuditSeal.pack_no > state.last_pack_no))
        if beyond is not None:
            raise HTTPException(409, f'封存索引显示第 {beyond} 包已封存，但登记封存头只到第 '
                                     f'{state.last_pack_no} 包；请先运行整链验真定位断点，暂不能重建索引')
        return _write_index(db, state)

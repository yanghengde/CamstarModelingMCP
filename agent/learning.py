"""Evidence-backed knowledge lifecycle. No model calls or business writes.

Corrections are untrusted review candidates, never automatic instructions.
Retirement is enforced at retrieval, with immutable transition snapshots.
"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import argparse
import hashlib
import json
from pathlib import Path
import re

from config import CAMSTAR_BASE_URL, CAMSTAR_QUERY_BASE_URL, MEMORY_CONTRACT_VERSION
from agent.experience import _connect, _now, _sanitize

TOPICS = {
    'order_dates': r'开工日期|开工时间|首箱|完工日期|完工时间|工单.{0,15}(?:日期|是否开工|有没有开工)',
    'wip': r'在制|\bwip\b',
    'container_start': r'序列号|自动编号|容器启动|container.?start',
    'order_completion': r'是否完工|是否完成|完工量|工单完工',
}
STATES = {'candidate', 'active', 'stale', 'retracted', 'superseded', 'rejected'}
CORRECTION = re.compile(r'不对|不正确|不准确|回答错|答错|查错|漏查|漏掉|漏了一|有误|纠正|更正|wrong answer|incorrect', re.I)


def environment():
    return hashlib.sha256(json.dumps([CAMSTAR_BASE_URL.rstrip('/'),
        CAMSTAR_QUERY_BASE_URL.rstrip('/')]).encode()).hexdigest()


def topics_for(text):
    return [name for name, pattern in TOPICS.items() if re.search(pattern, text, re.I)]


def _clean(value):
    """Sanitize before serialization, including JSON embedded in tool messages."""
    if isinstance(value, dict):
        return {key: ('<redacted>' if re.search(r'password|token|authorization|api.?key|otp|cookie|headers', str(key), re.I)
                      else _clean(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return _sanitize(value, 1200)
        if isinstance(parsed, (dict, list)):
            return json.dumps(_clean(parsed), ensure_ascii=False)
        return _sanitize(value, 1200)
    return value


@contextmanager
def db():
    with _connect() as connection:
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS learned_knowledge (
            id INTEGER PRIMARY KEY, candidate_id INTEGER UNIQUE,
            kind TEXT NOT NULL, topics TEXT NOT NULL, tool_name TEXT NOT NULL,
            content TEXT NOT NULL, evidence TEXT NOT NULL, source TEXT NOT NULL,
            state TEXT NOT NULL, environment TEXT NOT NULL, contract_version TEXT NOT NULL,
            owner TEXT NOT NULL, valid_from TEXT NOT NULL, expires_at TEXT,
            verified_at TEXT, replaced_by INTEGER, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, dependencies TEXT NOT NULL DEFAULT '[]',
            knowledge_key TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS learned_scope ON learned_knowledge(state, environment, owner);
        CREATE TABLE IF NOT EXISTS knowledge_audit (
            id INTEGER PRIMARY KEY, knowledge_id INTEGER NOT NULL, at TEXT NOT NULL,
            action TEXT NOT NULL, reason TEXT NOT NULL, snapshot TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS learning_feedback (
            id INTEGER PRIMARY KEY, environment TEXT NOT NULL, owner TEXT NOT NULL,
            session_id TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
            question TEXT NOT NULL, answer TEXT NOT NULL, correction TEXT NOT NULL,
            tool_evidence TEXT NOT NULL, knowledge_ids TEXT NOT NULL,
            created_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
        ''')
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(learned_knowledge)')}
        if 'dependencies' not in columns:
            connection.execute("ALTER TABLE learned_knowledge ADD COLUMN dependencies TEXT NOT NULL DEFAULT '[]'")
        if 'knowledge_key' not in columns:
            connection.execute("ALTER TABLE learned_knowledge ADD COLUMN knowledge_key TEXT NOT NULL DEFAULT ''")
        # Serialize read/modify/write decisions as well as audit snapshots.
        # Without this, two concurrent chats can both pass a deduplication read.
        connection.execute('BEGIN IMMEDIATE')
        # Legacy approvals have no known environment/version. Preserve them for
        # explicit revalidation rather than silently granting current authority.
        for row in connection.execute("SELECT * FROM experience_candidates WHERE status='approved' AND id NOT IN "
                "(SELECT candidate_id FROM learned_knowledge WHERE candidate_id IS NOT NULL)").fetchall():
            cursor = connection.execute('''INSERT INTO learned_knowledge
                (candidate_id,kind,topics,tool_name,content,evidence,source,state,environment,
                 contract_version,owner,valid_from,created_at,updated_at)
                VALUES (?,'procedure','[]',?,?,?,'legacy_approval','stale','legacy','legacy','',?,?,?)''',
                (row['id'], row['tool_name'], _sanitize(row['resolution'] or '', 3000), _sanitize(row['evidence'] or '', 3000),
                 row['first_seen'], _now(), _now()))
            _audit(connection, cursor.lastrowid, 'migrated', 'Legacy scope must be revalidated')
            connection.execute("UPDATE experience_candidates SET status='pending' WHERE id=?", (row['id'],))
        yield connection


def _audit(connection, key, action, reason):
    row = connection.execute('SELECT * FROM learned_knowledge WHERE id=?', (key,)).fetchone()
    connection.execute('INSERT INTO knowledge_audit(knowledge_id,at,action,reason,snapshot) VALUES(?,?,?,?,?)',
        (key, _now(), action, _sanitize(reason), json.dumps(dict(row), ensure_ascii=False)))


def _change(connection, key, state, reason, **changes):
    if state not in STATES:
        raise ValueError('invalid knowledge state')
    # Keys are internal literals only; never caller-provided SQL identifiers.
    allowed = {'content', 'evidence', 'topics', 'environment', 'contract_version', 'verified_at',
               'valid_from', 'expires_at', 'replaced_by'}
    if set(changes) - allowed:
        raise ValueError('unsupported memory update')
    values = {'state': state, 'updated_at': _now(), **changes}
    cursor = connection.execute('UPDATE learned_knowledge SET ' + ','.join(k+'=?' for k in values)
        + ' WHERE id=?', [*values.values(), key])
    if cursor.rowcount != 1:
        raise ValueError('knowledge not found')
    legacy_status = 'approved' if state == 'active' else 'pending' if state in {'candidate', 'stale'} else 'rejected'
    connection.execute('UPDATE experience_candidates SET status=? WHERE id=(SELECT candidate_id FROM learned_knowledge WHERE id=?)',
        (legacy_status, key))
    _audit(connection, key, state, reason)
    if state != 'active':
        # Dependent procedures lose their evidence when a premise is retired.
        # They need review even if a replacement premise has been published.
        for dependent in connection.execute("SELECT id,dependencies FROM learned_knowledge WHERE state='active'").fetchall():
            if key in json.loads(dependent['dependencies']):
                _change(connection, dependent['id'], 'stale', f'Premise knowledge #{key} is {state}')


def _maintenance(connection):
    now = _now()
    for row in connection.execute("SELECT * FROM learned_knowledge WHERE state='active'").fetchall():
        if row['environment'] != environment() or row['contract_version'] != MEMORY_CONTRACT_VERSION:
            # Different environments remain valid in their own deployment.
            if row['environment'] == environment():
                _change(connection, row['id'], 'stale', 'Contract version changed')
        elif row['expires_at'] and row['expires_at'] <= now:
            _change(connection, row['id'], 'stale', 'Verification interval expired')


def propose_knowledge(*, content, evidence, topics, tool_name='', kind='procedure',
                      owner='', source='manual', ttl_days=90, dependencies=None, knowledge_key=''):
    """Create a candidate. Approval is a separate administrative operation."""
    if kind not in {'procedure', 'contract', 'fact'}:
        raise ValueError('kind must be procedure, contract or fact')
    if (not isinstance(topics, list) or not topics or any(t not in TOPICS for t in topics)
            or not content.strip() or not evidence.strip()):
        raise ValueError('content, evidence and known topics are required')
    if type(ttl_days) is not int or not 1 <= ttl_days <= 3650:
        raise ValueError('ttl_days must be 1..3650')
    if knowledge_key and not re.fullmatch(r'[a-zA-Z0-9_.-]{1,120}', knowledge_key):
        raise ValueError('knowledge_key must be a stable ASCII business concept key')
    dependencies = dependencies or []
    if not isinstance(dependencies, list) or any(type(key) is not int for key in dependencies):
        raise ValueError('dependencies must be knowledge IDs')
    now = _now()
    expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat(timespec='seconds')
    with db() as connection:
        _maintenance(connection)
        _validate_dependencies(connection, dependencies, owner)
        # Consolidate identical candidates without refreshing their validity or
        # treating repeated statements as additional independent verification.
        existing = connection.execute('''SELECT id FROM learned_knowledge WHERE content=? AND topics=?
            AND owner=? AND environment=? AND contract_version=? AND kind=? AND tool_name=?
            AND dependencies=? AND knowledge_key=? AND state IN ('candidate','active')''',
            (_sanitize(content, 3000), json.dumps(sorted(set(topics))), owner, environment(),
             MEMORY_CONTRACT_VERSION, kind, tool_name, json.dumps(sorted(set(dependencies))), knowledge_key)).fetchone()
        if existing:
            return existing['id']
        cursor = connection.execute('''INSERT INTO learned_knowledge
            (kind,topics,tool_name,content,evidence,source,state,environment,contract_version,
             owner,valid_from,expires_at,created_at,updated_at,dependencies,knowledge_key)
            VALUES (?,?,?,?,?,?,'candidate',?,?,?,?,?,?,?,?,?)''',
            (kind, json.dumps(sorted(set(topics))), tool_name, _sanitize(content, 3000),
             _sanitize(evidence, 3000), _sanitize(source, 200), environment(), MEMORY_CONTRACT_VERSION,
             owner, now, expires, now, now, json.dumps(sorted(set(dependencies))), knowledge_key))
        key = cursor.lastrowid
        _audit(connection, key, 'proposed', 'Evidence submitted for review')
        return key


def _validate_dependencies(connection, keys, owner):
    for key in keys:
        premise = connection.execute('SELECT * FROM learned_knowledge WHERE id=?', (key,)).fetchone()
        if (not premise or premise['state'] != 'active' or premise['environment'] != environment()
                or premise['contract_version'] != MEMORY_CONTRACT_VERSION
                or premise['owner'] not in ('', owner)):
            raise ValueError('dependencies must be active knowledge with compatible scope')


def _check_conflict(connection, row, excluding=None):
    if row['knowledge_key']:
        conflicts = connection.execute("SELECT id FROM learned_knowledge WHERE knowledge_key=? AND owner=? "
            "AND environment=? AND contract_version=? AND state='active' AND id!=?",
            (row['knowledge_key'], row['owner'], environment(), MEMORY_CONTRACT_VERSION, row['id'])).fetchall()
        if any(r['id'] != excluding for r in conflicts):
            raise ValueError('active knowledge for this concept exists; explicitly supersede it')


def review_knowledge(key, *, action, evidence, replacement_id=None, ttl_days=90):
    if not evidence.strip():
        raise ValueError('review evidence/reason required')
    if action not in {'activate', 'revalidate', 'retract', 'supersede', 'reject'}:
        raise ValueError('invalid review action')
    with db() as connection:
        _maintenance(connection)
        row = connection.execute('SELECT * FROM learned_knowledge WHERE id=?', (key,)).fetchone()
        if not row:
            raise ValueError('knowledge not found')
        if action in {'activate', 'revalidate'}:
            permitted = {'candidate', 'stale'} if action == 'activate' else {'active', 'stale'}
            if row['state'] not in permitted:
                raise ValueError('only candidate/stale knowledge can be activated')
            if not row['content'].strip() or type(ttl_days) is not int or not 1 <= ttl_days <= 3650:
                raise ValueError('content and ttl_days=1..3650 required')
            _validate_dependencies(connection, json.loads(row['dependencies']), row['owner'])
            _check_conflict(connection, row)
            expires = (datetime.now(timezone.utc)+timedelta(days=ttl_days)).isoformat(timespec='seconds')
            _change(connection, key, 'active', evidence, evidence=_sanitize(evidence, 3000),
                environment=environment(), contract_version=MEMORY_CONTRACT_VERSION,
                verified_at=_now(), expires_at=expires)
        elif action == 'supersede':
            replacement = connection.execute('SELECT * FROM learned_knowledge WHERE id=?', (replacement_id,)).fetchone()
            if (not replacement or replacement_id == key or replacement['state'] not in {'candidate', 'active'}
                    or replacement['environment'] != row['environment'] or replacement['owner'] != row['owner']
                    or replacement['contract_version'] != row['contract_version']
                    or (row['knowledge_key'] and replacement['knowledge_key'] != row['knowledge_key'])
                    or not set(json.loads(replacement['topics'])).intersection(json.loads(row['topics']))):
                raise ValueError('replacement must be candidate/active, related and have identical scope')
            if row['state'] in {'retracted', 'superseded', 'rejected'}:
                raise ValueError('retired knowledge cannot be superseded again')
            pending = list(json.loads(replacement['dependencies']))
            seen = set()
            while pending:
                parent = pending.pop()
                if parent == key:
                    raise ValueError('replacement cannot depend on the knowledge it replaces')
                if parent not in seen:
                    seen.add(parent)
                    premise = connection.execute('SELECT dependencies FROM learned_knowledge WHERE id=?', (parent,)).fetchone()
                    if premise:
                        pending.extend(json.loads(premise['dependencies']))
            _validate_dependencies(connection, json.loads(replacement['dependencies']), replacement['owner'])
            _check_conflict(connection, replacement, excluding=key)
            if replacement['state'] == 'candidate':
                if type(ttl_days) is not int or not 1 <= ttl_days <= 3650:
                    raise ValueError('ttl_days must be 1..3650')
                expires = (datetime.now(timezone.utc)+timedelta(days=ttl_days)).isoformat(timespec='seconds')
                _change(connection, replacement_id, 'active', evidence, evidence=_sanitize(evidence, 3000),
                    verified_at=_now(), expires_at=expires)
            _change(connection, key, 'superseded', evidence, replaced_by=replacement_id)
        else:
            if row['state'] in {'retracted', 'superseded', 'rejected'}:
                raise ValueError('knowledge already retired')
            _change(connection, key, 'retracted' if action == 'retract' else 'rejected', evidence)


def review_legacy_candidate(candidate_id, *, status, resolution, evidence, revalidate, topics):
    """Keep existing administration usable; commit legacy and lifecycle together."""
    with db() as connection:
        old = connection.execute('SELECT * FROM experience_candidates WHERE id=?', (candidate_id,)).fetchone()
        if not old:
            raise ValueError(f'candidate {candidate_id} was not found')
        related = topics or topics_for((resolution or '') + ' ' + old['symptom'])
        if any(t not in TOPICS for t in related):
            raise ValueError('unknown topic')
        existing = connection.execute('SELECT * FROM learned_knowledge WHERE candidate_id=?', (candidate_id,)).fetchone()
        if existing and existing['state'] in {'superseded', 'retracted', 'rejected'}:
            raise ValueError('retired knowledge requires a new candidate')
        if (existing and existing['source'] != 'legacy_approval' and status == 'approved'
                and existing['content'] != _sanitize(resolution or '', 3000)):
            raise ValueError('changed knowledge requires a new candidate and explicit supersession')
        now = _now()
        expires = (datetime.now(timezone.utc)+timedelta(days=90)).isoformat(timespec='seconds')
        values = (json.dumps(related), old['tool_name'], _sanitize(resolution or '', 3000),
            _sanitize(evidence or '', 3000), 'active' if status == 'approved' else 'rejected',
            environment(), MEMORY_CONTRACT_VERSION, now, expires, now, now)
        if existing:
            if status == 'rejected':
                _change(connection, existing['id'], 'rejected', evidence or 'Rejected by reviewer')
            else:
                connection.execute('''UPDATE learned_knowledge SET topics=?,tool_name=?,content=?,evidence=?,state=?,
                    environment=?,contract_version=?,valid_from=?,expires_at=?,verified_at=?,updated_at=?,
                    source='candidate_review' WHERE id=?''', (*values, existing['id']))
            key = existing['id']
        else:
            cursor = connection.execute('''INSERT INTO learned_knowledge(topics,tool_name,content,evidence,state,
                environment,contract_version,valid_from,expires_at,verified_at,updated_at,candidate_id,
                kind,source,owner,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'procedure','candidate_review','',?)''',
                (*values, candidate_id, now))
            key = cursor.lastrowid
        connection.execute('UPDATE experience_candidates SET status=?,resolution=?,evidence=?,revalidate=?,reviewed_at=? WHERE id=?',
            (status, _sanitize(resolution or '', 3000), _sanitize(evidence or '', 3000), int(revalidate), now, candidate_id))
        _audit(connection, key, status, evidence or 'Rejected by reviewer')


def list_knowledge(state='all'):
    if state != 'all' and state not in STATES:
        raise ValueError('invalid state')
    with db() as connection:
        _maintenance(connection)
        sql = 'SELECT * FROM learned_knowledge' + (' WHERE state=?' if state != 'all' else '')
        return [dict(row) for row in connection.execute(sql + ' ORDER BY id DESC', () if state == 'all' else (state,))]


def retrieve(message, tool_names=(), owner='', limit=5):
    requested = set(topics_for(message))
    with db() as connection:
        _maintenance(connection)
        rows = connection.execute("SELECT * FROM learned_knowledge WHERE state='active' AND environment=? "
            "AND contract_version=? AND owner IN ('',?) AND valid_from<=? ORDER BY verified_at DESC,id DESC",
            (environment(), MEMORY_CONTRACT_VERSION, owner, _now())).fetchall()
        ranked = []
        private_keys = {r['knowledge_key'] for r in rows if r['owner'] and r['knowledge_key']}
        for row in rows:
            if not row['owner'] and row['knowledge_key'] in private_keys:
                continue
            topic_match = len(requested.intersection(json.loads(row['topics'])))
            tool_match = bool(row['tool_name'] and row['tool_name'] in tool_names)
            if topic_match or tool_match:
                ranked.append((topic_match * 10 + tool_match, dict(row)))
        ranked.sort(key=lambda item: -item[0])
        return [row for _, row in ranked[:max(1, min(limit, 10))]]


def record_feedback(username, session_id, message, messages):
    """Capture a suspected answer error with preceding tool evidence. Idempotent.

    No automatic promotion/retraction: a user complaint is evidence of a dispute,
    not evidence that the proposed correction or a global rule is true.
    """
    if not username or not session_id or not CORRECTION.search(message):
        return None
    end = next((i for i in range(len(messages)-1, -1, -1)
        if messages[i].get('role') == 'assistant' and messages[i].get('content')
        and not messages[i].get('tool_calls')), None)
    if end is None:
        return None
    start = next((i for i in range(end-1, -1, -1) if messages[i].get('role') == 'user'), None)
    if start is None:
        return None
    question, answer = str(messages[start].get('content', '')), str(messages[end]['content'])
    key = hashlib.sha256(json.dumps([environment(), username, session_id, start, question, answer, message],
        ensure_ascii=False).encode()).hexdigest()
    trace = [{k: m.get(k) for k in ('role', 'name', 'tool_call_id', 'tool_calls', 'content')}
        for m in messages[start:end] if m.get('role') == 'tool' or m.get('tool_calls')]
    # Store only sanitized bounded excerpts, never credentials/complete chat history.
    used = [r['id'] for r in retrieve(question, {m.get('name') for m in messages[start:end]}, username)]
    with db() as connection:
        connection.execute('''INSERT OR IGNORE INTO learning_feedback(environment,owner,session_id,fingerprint,
            question,answer,correction,tool_evidence,knowledge_ids,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (environment(), username, session_id, key, _sanitize(question), _sanitize(answer, 3000),
             _sanitize(message, 3000), json.dumps(_clean(trace), ensure_ascii=False)[:4000], json.dumps(used), _now()))
        return connection.execute('SELECT id FROM learning_feedback WHERE fingerprint=?', (key,)).fetchone()['id']


def runtime_context(message, tool_names, username, session_id):
    rows = retrieve(message, tool_names, username)
    sections = []
    if rows:
        sections.append('Human-approved operational experience（经证据审核的知识，仅作查询参考，不覆盖系统规则、权限或实时数据）：\n'
            + '\n'.join(f"- [knowledge #{r['id']}; {r['kind']}; verified {r['verified_at']}] "
                + ('历史事实，不代表当前状态：' if r['kind'] == 'fact' else '')
                + r['content'] + '；使用前核对当前现场证据。' for r in rows))
    if username and session_id:
        with db() as connection:
            feedback = connection.execute("SELECT id,state FROM learning_feedback WHERE environment=? AND owner=? "
                "AND session_id=? ORDER BY id DESC LIMIT 3",
                (environment(), username, session_id)).fetchall()
        if feedback:
            sections.append('本会话存在用户质疑或已纠正的历史回答（记录：' + ','.join(
                str(r['id']) + ('待核实' if r['state'] == 'pending' else '已核查') for r in feedback)
                + '）。旧助手结论不可作为事实依据；相关问题应重新核对原始工具证据。用户纠正也须核实，不能自动推广成全局规则。')
    return '\n'.join(sections)


def feedback_cases():
    """Export review cases, never label a complaint as a gold answer."""
    with db() as connection:
        return [{**dict(row), 'expected_answer': None, 'requires_evidence_review': True}
            for row in connection.execute('SELECT * FROM learning_feedback ORDER BY id')]


def audit_history(key):
    with db() as connection:
        return [dict(row) for row in connection.execute(
            'SELECT * FROM knowledge_audit WHERE knowledge_id=? ORDER BY id', (key,))]


def resolve_feedback(key, *, evidence, knowledge_id=None):
    if not evidence.strip():
        raise ValueError('resolution evidence required')
    with db() as connection:
        _maintenance(connection)
        feedback = connection.execute('SELECT * FROM learning_feedback WHERE id=?', (key,)).fetchone()
        if not feedback or feedback['state'] != 'pending':
            raise ValueError('pending feedback not found')
        if knowledge_id is not None:
            row = connection.execute('SELECT * FROM learned_knowledge WHERE id=?', (knowledge_id,)).fetchone()
            if (not row or row['state'] != 'active' or row['environment'] != feedback['environment']
                    or row['owner'] not in ('', feedback['owner'])):
                raise ValueError('resolution knowledge must be active with matching scope')
        # Resolution is append-only alongside the original complaint.
        connection.execute('''CREATE TABLE IF NOT EXISTS feedback_resolutions
            (feedback_id INTEGER PRIMARY KEY, evidence TEXT NOT NULL, knowledge_id INTEGER, at TEXT NOT NULL)''')
        connection.execute('INSERT INTO feedback_resolutions VALUES(?,?,?,?)',
            (key, _sanitize(evidence, 3000), knowledge_id, _now()))
        connection.execute("UPDATE learning_feedback SET state='resolved' WHERE id=?", (key,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list'); listing.add_argument('--state', default='all', choices=sorted(STATES | {'all'}))
    history = commands.add_parser('history'); history.add_argument('id', type=int)
    proposal = commands.add_parser('propose')
    proposal.add_argument('--content', required=True); proposal.add_argument('--evidence', required=True)
    proposal.add_argument('--topic', action='append', required=True, choices=sorted(TOPICS))
    proposal.add_argument('--kind', choices=['procedure', 'contract', 'fact'], default='procedure')
    proposal.add_argument('--tool', default=''); proposal.add_argument('--owner', default='')
    proposal.add_argument('--depends-on', action='append', type=int, default=[])
    proposal.add_argument('--key', default='')
    review = commands.add_parser('review'); review.add_argument('id', type=int)
    review.add_argument('--action', required=True, choices=['activate', 'revalidate', 'retract', 'supersede', 'reject'])
    review.add_argument('--evidence', required=True); review.add_argument('--replacement', type=int)
    review.add_argument('--ttl-days', type=int, default=90)
    export = commands.add_parser('export-feedback'); export.add_argument('--output', type=Path, required=True)
    resolve = commands.add_parser('resolve-feedback'); resolve.add_argument('id', type=int)
    resolve.add_argument('--evidence', required=True); resolve.add_argument('--knowledge-id', type=int)
    args = parser.parse_args()
    if args.command == 'list':
        print(json.dumps(list_knowledge(args.state), ensure_ascii=False, indent=2))
    elif args.command == 'history':
        print(json.dumps(audit_history(args.id), ensure_ascii=False, indent=2))
    elif args.command == 'propose':
        print(propose_knowledge(content=args.content, evidence=args.evidence, topics=args.topic,
            tool_name=args.tool, kind=args.kind, owner=args.owner, dependencies=args.depends_on,
            knowledge_key=args.key))
    elif args.command == 'review':
        review_knowledge(args.id, action=args.action, evidence=args.evidence,
            replacement_id=args.replacement, ttl_days=args.ttl_days)
    elif args.command == 'resolve-feedback':
        resolve_feedback(args.id, evidence=args.evidence, knowledge_id=args.knowledge_id)
    else:
        args.output.write_text(json.dumps(feedback_cases(), ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()

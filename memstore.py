"""记忆：会话上下文持久化 + 长期记忆（模型自己整理）。

存在 ``cache/memory.db``（SQLite），三张表：

* ``msg``   —— 每个会话最近的对话，重启后接着聊，不再一重启就「失忆」
* ``fact``  —— 长期记忆：谁、什么事，一句话；``kind`` 区分事实卡片与日记片段
* ``meta``  —— 计数、日记游标（整理到哪条消息了）、上次整理日期

设计约束，按重要性排：

1. 记忆出任何问题都不能影响回消息 —— 所有对外函数自己吞异常，只记日志；
2. 不额外依赖 —— 只用 sqlite3 标准库；
3. 省 token —— 聊天机器人是「写一次、读几百次」，所以**压缩率比生成频率重要**。

两种整理方式，由 ``memory.mode`` 决定：

* ``fact``（旧）：每攒 N 条消息就调一次模型，抽成「谁 + 什么事」的静态事实。
* ``diary``（默认）：**每个会话每天最多调一次模型**，把上次游标之后的对话写成
  日记，再压成不超过 ``diary_budget`` 字、最多 ``diary_max_items`` 条的短片段。
  片段仍走相关度召回，不整篇塞进 prompt —— 否则每轮多带几百字，反而更贵。

日记模式能记住事情的过程和情绪（事实卡片只能记静态属性），代价是当天聊的事
要等写日记之后才进入长期记忆；最近 12 条历史仍然即时可见，不影响当下对话。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

_DB = {'path': None}
_DEFAULTS = {
    'enabled': True,
    'mode': 'diary',          # 'diary' 按天写日记 | 'fact' 按条抽事实（旧）
    'keep_messages': 60,      # 每个会话保留多少条历史
    'max_facts': 120,         # 每个会话最多多少条长期记忆
    'recall_limit': 6,        # 每次回答最多回忆几条（日记模式自动降到 4）
    'extract_every': 16,      # fact 模式：每攒多少条消息总结一次
    'extract_min_gap': 180,   # fact 模式：两次总结至少间隔多少秒
    'diary_min_msgs': 12,     # diary 模式：当天至少攒够多少条才写
    'diary_max_items': 6,     # 一次日记最多几条片段
    'diary_item_chars': 36,   # 每条片段最多多少字（含日期前缀）
    'diary_budget': 150,      # 一次日记所有片段合计不超过多少字
    'diary_input_budget': 6000,  # 喂给模型的对话最多多少字（防刷屏日爆量）
}
_CFG = dict(_DEFAULTS)
_LOCK = threading.Lock()
_STATE = {'extracting': False}
_HIST_CACHE = {}              # chat -> 已建过的 deque 标记（仅用于避免重复读取）


# ---------------------------------------------------------------- 基础
def configure(root: Path | str, cfg: dict | None = None) -> None:
    """设置数据库位置与参数。bot 启动时调一次，控制台也会调。"""
    cfg = cfg or {}
    _CFG.clear()
    _CFG.update(_DEFAULTS)                     # 多次 configure 不互相残留
    _CFG.update({k: v for k, v in cfg.items() if v is not None})
    if str(_CFG.get('mode', 'diary')) not in ('diary', 'fact'):
        _CFG['mode'] = 'diary'
    if _CFG['mode'] == 'diary':
        # 日记片段比事实卡片长，注入条数少一点才能让「读侧」开销持平
        if cfg.get('recall_limit') is None:
            _CFG['recall_limit'] = 4
        # 按天归档要能回看一整天的对话，60 条会被后面的消息冲掉
        # （history() 仍只取 history_messages 条，多存不影响每轮 prompt）
        if cfg.get('keep_messages') is None:
            _CFG['keep_messages'] = 400
    db = cfg.get('db') or 'cache/memory.db'
    p = Path(db)
    if not p.is_absolute():
        p = Path(root) / p
    _DB['path'] = p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _conn() -> sqlite3.Connection | None:
    path = _DB['path']
    if not path:
        return None
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=3000')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS msg ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
        ' chat TEXT NOT NULL, role TEXT NOT NULL, speaker TEXT DEFAULT "",'
        ' content TEXT NOT NULL, ts REAL NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_msg_chat ON msg(chat, id)')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS fact ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
        ' chat TEXT NOT NULL, subject TEXT DEFAULT "", text TEXT NOT NULL,'
        ' norm TEXT NOT NULL, ts REAL NOT NULL, last_used REAL DEFAULT 0,'
        ' uses INTEGER DEFAULT 0, weight REAL DEFAULT 1.0,'
        " kind TEXT DEFAULT 'fact')")
    # 老库没有 kind 列：补上，已有的事实卡片保持 kind='fact'，不会丢
    cols = {r[1] for r in conn.execute('PRAGMA table_info(fact)')}
    if 'kind' not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN kind TEXT DEFAULT 'fact'")
        conn.commit()
    conn.execute('CREATE INDEX IF NOT EXISTS idx_fact_chat ON fact(chat, id)')
    conn.execute('CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)')
    return conn


def enabled() -> bool:
    return bool(_CFG.get('enabled', True)) and _DB['path'] is not None


def diary_mode() -> bool:
    return str(_CFG.get('mode', 'diary')) == 'diary'


def diary_done_today(chat: str) -> bool:
    """今天这个会话是否已经写过日记（供调用方避免反复起空线程）。"""
    try:
        return _meta_get('diary_day:' + chat) == time.strftime('%Y-%m-%d')
    except Exception:
        return False


# ---------------------------------------------------------------- 对话历史
def history(chat: str, limit: int) -> list[dict]:
    """取最近 limit 条消息，按时间正序，直接可以拼进 messages。"""
    if limit <= 0:
        return []
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return []
        rows = conn.execute(
            'SELECT role, content FROM msg WHERE chat=? ORDER BY id DESC LIMIT ?',
            (chat, limit)).fetchall()
        return [{'role': r[0], 'content': r[1]} for r in reversed(rows)]
    except Exception:
        return []
    finally:
        _close(conn)


def log(chat: str, role: str, content: str, speaker: str = '') -> None:
    """存一条对话，并顺手裁掉超出保留量的旧记录。"""
    if not enabled() or not content:
        return
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return
        conn.execute('INSERT INTO msg(chat, role, speaker, content, ts) VALUES(?,?,?,?,?)',
                     (chat, role, speaker or '', content, time.time()))
        keep = int(_CFG.get('keep_messages', 60) or 60)
        conn.execute(
            'DELETE FROM msg WHERE chat=? AND id <= ('
            ' SELECT id FROM msg WHERE chat=? ORDER BY id DESC LIMIT 1 OFFSET ?)',
            (chat, chat, keep))
        conn.commit()
    except Exception:
        pass
    finally:
        _close(conn)


def recent_lines(chat: str, limit: int) -> list[str]:
    """给总结用的可读对话行：``发言人：内容``。"""
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return []
        rows = conn.execute(
            'SELECT role, speaker, content FROM msg WHERE chat=? ORDER BY id DESC LIMIT ?',
            (chat, limit)).fetchall()
    except Exception:
        return []
    finally:
        _close(conn)
    out = []
    for role, speaker, content in reversed(rows):
        who = '我' if role == 'assistant' else (speaker or '对方')
        out.append('%s：%s' % (who, _clip(content, 120)))
    return out


# ---------------------------------------------------------------- 长期记忆
def _grams(text: str) -> set:
    s = re.sub(r'\s+', '', (text or '').lower())
    grams = {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}
    grams |= set(re.findall(r'[a-z]{3,}', (text or '').lower()))
    return {g for g in grams if g}


_STOP_CHARS = set('的了在是我你他她它吗呢吧啊么什怎这那个有和就都也还很太不要会能对把'
                  '被让给说做去来到上下中一个们您')


def _chars(text: str) -> set:
    """去掉虚词后的单字集合。中文问句常把词拆开（「想喝点什么」vs「喜欢喝咖啡」），
    两字窗口碰不上，靠单字兜一层。"""
    return {c for c in (text or '') if '\u4e00' <= c <= '\u9fff' and c not in _STOP_CHARS}


def _norm(text: str) -> str:
    return re.sub(r'[\s，。！？、：；,.!?:;"\'（）()【】\[\]]+', '', (text or '').lower())


def _clip(text: str, n: int) -> str:
    text = ' '.join((text or '').split())
    return text if len(text) <= n else text[:n] + '…'


def recall(chat: str, query: str, limit: int | None = None) -> list[str]:
    """按相关度取回长期记忆，返回可直接进 prompt 的短句列表。"""
    if not enabled():
        return []
    limit = int(limit or _CFG.get('recall_limit', 6))
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return []
        rows = conn.execute(
            'SELECT id, subject, text, ts, weight, uses FROM fact WHERE chat=? '
            'ORDER BY id DESC LIMIT 400', (chat,)).fetchall()
    except Exception:
        return []
    finally:
        _close(conn)
    if not rows:
        return []
    q = _grams(query)
    qc = _chars(query)
    now = time.time()
    scored = []
    for fid, subject, text, ts, weight, uses in rows:
        f = _grams(text) | _grams(subject)
        overlap = len(q & f)
        if overlap:
            rel = overlap / (len(q) ** 0.5)
        else:
            # 两字窗口没撞上，再看单字（「想喝点什么」里的「喝」）
            shared = qc & _chars(text + subject)
            if not shared:
                # 一点不沾边就不往 prompt 里塞：宁可少一条，也别让模型顺着无关记忆跑偏
                continue
            rel = 0.45 * min(len(shared), 3)
        age_days = max(0.0, (now - (ts or now)) / 86400.0)
        fresh = 1.0 / (1.0 + age_days / 30.0)          # 一个月内权重不明显衰减
        score = rel + fresh * 0.35 + float(weight or 1.0) * 0.1 + min(int(uses or 0), 5) * 0.02
        scored.append((score, fid, subject, text))
    scored.sort(reverse=True)
    picked = scored[:limit]
    ids = [p[1] for p in picked]
    _touch(ids)
    out = []
    for _s, _fid, subject, text in picked:
        out.append(('%s：%s' % (subject, text)) if subject else text)
    return out


def _touch(ids: list[int]) -> None:
    if not ids:
        return
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return
        conn.executemany('UPDATE fact SET uses=uses+1, last_used=? WHERE id=?',
                         [(time.time(), i) for i in ids])
        conn.commit()
    except Exception:
        pass
    finally:
        _close(conn)


def prompt_block(chat: str, query: str) -> dict:
    """组装要拼进 system 的记忆段，返回 {'text': str, 'count': int}。

    返回 dict 而不是字符串，是为了让调用方能顺手在日志里报「回忆了几条」。
    """
    if not enabled():
        return {'text': '', 'count': 0}
    try:
        items = recall(chat, query)
    except Exception:
        return {'text': '', 'count': 0}
    if not items:
        return {'text': '', 'count': 0}
    body = '\n'.join('· ' + it for it in items)
    if diary_mode():
        text = ('\n\n【日记片段（自然带出来，别报菜名、别说"根据我的记忆"）】\n'
                + body + '\n【与当前话题无关就不提，别编细节】')
    else:
        text = ('\n\n【印象里记得的事（自然带出来，别硬报菜名、别说"根据我的记忆"）】\n'
                + body + '\n【以上只是印象，不要编造细节；与当前对话无关就不提】')
    return {'text': text, 'count': len(items)}


def add_fact(chat: str, subject: str, text: str, kind: str | None = None) -> str:
    """写入一条记忆。返回 'new' / 'update' / 'skip'。

    kind='diary' 的片段有更短的长度上限 —— 注入时要按字付费，不能让它膨胀。
    """
    kind = kind or ('diary' if diary_mode() else 'fact')
    cap_chars = (int(_CFG.get('diary_item_chars', 36) or 36)
                 if kind == 'diary' else 60)
    text = _clip(text, cap_chars)
    if not enabled() or len(text) < 4:
        return 'skip'
    subject = _clip(subject or '', 20)
    norm = _norm(text)
    if not norm:
        return 'skip'
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return 'skip'
        rows = conn.execute('SELECT id, text, norm FROM fact WHERE chat=? '
                            'ORDER BY id DESC LIMIT 300', (chat,)).fetchall()
        for fid, old_text, old_norm in rows:
            if old_norm == norm:
                return 'skip'
            if difflib.SequenceMatcher(None, norm, old_norm).ratio() > 0.82:
                # 同一件事换了说法：以新的为准，权重略加权
                conn.execute('UPDATE fact SET text=?, norm=?, subject=?, ts=?,'
                             ' weight=weight+0.2, kind=? WHERE id=?',
                             (text, norm, subject, time.time(), kind, fid))
                conn.commit()
                return 'update'
        conn.execute('INSERT INTO fact(chat, subject, text, norm, ts, kind) '
                     'VALUES(?,?,?,?,?,?)',
                     (chat, subject, text, norm, time.time(), kind))
        cap = int(_CFG.get('max_facts', 120) or 120)
        conn.execute(
            'DELETE FROM fact WHERE chat=? AND id <= ('
            ' SELECT id FROM fact WHERE chat=? ORDER BY id DESC LIMIT 1 OFFSET ?)',
            (chat, chat, cap))
        conn.commit()
        return 'new'
    except Exception:
        return 'skip'
    finally:
        _close(conn)


def facts(chat: str = '', search: str = '', limit: int = 500) -> list[dict]:
    """列表用（控制台记忆页）。"""
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return []
        sql = 'SELECT id, chat, subject, text, ts, uses, kind FROM fact'
        args = []
        where = []
        if chat:
            where.append('chat=?')
            args.append(chat)
        if search:
            where.append('text LIKE ?')
            args.append('%' + search + '%')
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
    except Exception:
        return []
    finally:
        _close(conn)
    return [{'id': r[0], 'chat': r[1], 'subject': r[2], 'text': r[3],
             'ts': r[4], 'uses': r[5], 'kind': r[6] or 'fact'} for r in rows]


def chats() -> list[str]:
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return []
        rows = conn.execute('SELECT chat, COUNT(*) c FROM fact GROUP BY chat '
                            'ORDER BY c DESC').fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
    finally:
        _close(conn)


def delete_fact(fid: int) -> bool:
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return False
        conn.execute('DELETE FROM fact WHERE id=?', (int(fid),))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        _close(conn)


def clear(chat: str = '') -> int:
    """清空某个会话的记忆；chat 为空则清空全部。返回删除条数。"""
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return 0
        if chat:
            cur = conn.execute('DELETE FROM fact WHERE chat=?', (chat,))
            conn.execute('DELETE FROM msg WHERE chat=?', (chat,))
        else:
            cur = conn.execute('DELETE FROM fact')
            conn.execute('DELETE FROM msg')
        conn.execute("DELETE FROM meta WHERE k LIKE 'pending:%' "
                     "OR k LIKE 'diary_day:%' OR k LIKE 'diary_cursor:%'")
        conn.commit()
        return cur.rowcount or 0
    except Exception:
        return 0
    finally:
        _close(conn)


def stats() -> dict:
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return {'facts': 0, 'messages': 0, 'chats': 0, 'path': ''}
        f = conn.execute('SELECT COUNT(*) FROM fact').fetchone()[0]
        d = conn.execute("SELECT COUNT(*) FROM fact WHERE kind='diary'").fetchone()[0]
        m = conn.execute('SELECT COUNT(*) FROM msg').fetchone()[0]
        c = conn.execute('SELECT COUNT(DISTINCT chat) FROM fact').fetchone()[0]
        return {'facts': f, 'diary': d, 'messages': m, 'chats': c,
                'path': str(_DB['path'] or '')}
    except Exception:
        return {'facts': 0, 'diary': 0, 'messages': 0, 'chats': 0, 'path': ''}
    finally:
        _close(conn)


# ---------------------------------------------------------------- 总结
_SYSTEM = """你在帮一个微信群聊天机器人整理「长期记忆」。

从下面的对话里挑出**以后还会用到**的事实，每条写成一句短话（不超过 30 字）。

该记的：身份与称呼、稳定偏好（喜欢/讨厌什么）、习惯、约定与计划、重要经历、近况变化。
不该记的：寒暄、一次性的情绪发泄、随时会变的琐事、账号密码住址手机号等敏感信息、
以及「正在问什么」这类马上过期的信息。

只输出 JSON 数组，不要解释。元素格式：
{"who": "谁（人名，或「群」表示整个群）", "what": "一句话事实"}
没有值得记的就输出 []。"""


def maybe_extract(chat: str, llm, log_fn=None, force: bool = False) -> int:
    """攒够消息就让模型总结一次；返回新增/更新的条数。

    llm: 形如 ``llm(messages) -> str`` 的调用（由调用方注入模型配置）。
    节流：每 ``extract_every`` 条消息一次，且两次之间至少 ``extract_min_gap`` 秒。
    """
    say = log_fn or (lambda *a, **k: None)
    if not enabled():
        return 0
    with _LOCK:
        if _STATE['extracting'] and not force:
            return 0
        pending = _pending(chat)
        need = int(_CFG.get('extract_every', 16) or 16)
        if not force and pending < need:
            return 0
        if not force:
            last = float(_meta_get('last:' + chat) or 0)
            gap = _CFG.get('extract_min_gap')
            gap = 180.0 if gap is None else float(gap)     # 0 = 不节流，允许连跑
            if gap > 0 and time.time() - last < gap:
                return 0
        _STATE['extracting'] = True
        _meta_set('pending:' + chat, '0')
        _meta_set('last:' + chat, str(time.time()))
    try:
        lines = recent_lines(chat, 40)
        if len(lines) < 4:
            return 0
        known = [f['text'] for f in facts(chat, limit=40)]
        user = []
        if known:
            user.append('已有记忆（不要重复，也不用再说一遍）：')
            user += ['- ' + k for k in known]
        user.append('对话：')
        user += lines
        raw = llm([{'role': 'system', 'content': _SYSTEM},
                   {'role': 'user', 'content': '\n'.join(user)}])
        items = _parse(raw)
        added = 0
        for who, what in items:
            r = add_fact(chat, who, what)
            if r in ('new', 'update'):
                added += 1
                say('[记忆] %s %s：%s' % ('新增' if r == 'new' else '更新',
                                          who or chat, what), flush=True)
        if added:
            say('[记忆] 本次从「%s」里整理出 %d 条' % (chat, added), flush=True)
        return added
    except Exception as exc:
        say('[记忆] 总结失败（不影响聊天）：%s: %s' % (type(exc).__name__, exc),
            flush=True)
        return 0
    finally:
        with _LOCK:
            _STATE['extracting'] = False


def _parse(raw: str) -> list[tuple[str, str]]:
    """从模型输出里抠出 JSON 数组，容忍 ```json 包裹和前后废话。"""
    if not raw:
        return []
    s = raw.strip()
    s = re.sub(r'^```(?:json)?|```$', '', s, flags=re.M).strip()
    i, j = s.find('['), s.rfind(']')
    if i < 0 or j <= i:
        return []
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return []
    out = []
    for it in data if isinstance(data, list) else []:
        if isinstance(it, dict):
            who = str(it.get('who') or it.get('subject') or '').strip()
            what = str(it.get('what') or it.get('text') or '').strip()
        elif isinstance(it, str):
            who, what = '', it.strip()
        else:
            continue
        what = re.sub(r'^[-·•\s]+', '', what)
        if 4 <= len(what) <= 120:
            out.append((who, what))
    return out


# ---------------------------------------------------------------- 日记模式
_DIARY_SYSTEM = """你在替一个微信群聊天机器人写「日记」。

把下面这段对话写成几条以后还用得上的日记片段。要求：
- 每条一句话，不超过 30 字；最多 6 条，全部加起来不超过 150 字；
- 记事情的过程和来龙去脉：谁提了什么、大家怎么回应、约好做什么、后来有没有变、
  当时的气氛。稳定偏好和身份也记，但别只写成「谁喜欢什么」的清单；
- 不记：纯寒暄、刷屏、一次性的情绪发泄、账号密码住址手机号等敏感信息；
- who 填最相关的人名（或「群」表示整个群），没有明确主角就留空。

只输出 JSON 数组，不要解释：
[{"who": "小王", "what": "说要减肥，被大家调侃坚持不过三天"}]
没什么值得记的就输出 []。"""


def diary_lines(chat: str, since_id: int = 0, limit: int = 800):
    """取游标之后的新对话，返回 (可读行, 新游标, 日期标签)。"""
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return [], since_id, ''
        rows = conn.execute(
            'SELECT id, role, speaker, content, ts FROM msg '
            'WHERE chat=? AND id>? ORDER BY id ASC LIMIT ?',
            (chat, since_id, limit)).fetchall()
    except Exception:
        return [], since_id, ''
    finally:
        _close(conn)
    if not rows:
        return [], since_id, ''
    pairs = []
    for _id, role, speaker, content, ts in rows:
        who = '我' if role == 'assistant' else (speaker or '对方')
        # 机器人自己的回复对「今天群里发生了什么」信息量低，截短省输入
        cap = 24 if role == 'assistant' else 120
        pairs.append(('%s：%s' % (who, _clip(content, cap)), ts))
    budget = int(_CFG.get('diary_input_budget', 6000) or 6000)
    if budget > 0 and sum(len(x) for x, _ in pairs) > budget:
        # 刷屏日保护：只留最近的一段。日记本来就是摘要，宁可略掉上午的闲聊，
        # 也不让单次调用的输入无限增长；游标仍推到最后一条，避免下次重复喂。
        kept, used = [], 0
        for pair in reversed(pairs):
            if used + len(pair[0]) > budget:
                break
            kept.append(pair)
            used += len(pair[0])
        pairs = list(reversed(kept))
    if not pairs:
        return [], since_id, ''
    lines = [p[0] for p in pairs]
    first = time.strftime('%m/%d', time.localtime(pairs[0][1] or time.time()))
    last = time.strftime('%m/%d', time.localtime(pairs[-1][1] or time.time()))
    label = first if first == last else first + '-' + last
    return lines, rows[-1][0], label


def _compress_diary(items: list[tuple[str, str]], day_label: str):
    """硬性预算：条数、单条长度、总字数三重限制，模型写长了也会被截掉。"""
    max_items = int(_CFG.get('diary_max_items', 6) or 6)
    item_chars = int(_CFG.get('diary_item_chars', 36) or 36)
    budget = int(_CFG.get('diary_budget', 150) or 150)
    label = day_label.split('-')[0]
    out, used = [], 0
    for who, what in items:
        # 日期由代码统一加，不信模型自己写的
        what = re.sub(r'^\d{1,2}/\d{1,2}\s*', '', (what or '').strip())
        if len(what) < 6:
            continue
        what = label + ' ' + what
        if len(what) > item_chars:
            what = what[:item_chars - 1] + '…'      # 不用 _clip：它会多带一个字
        if used + len(what) > budget:
            break
        out.append((who, what))
        used += len(what)
        if len(out) >= max_items:
            break
    return out


def maybe_diary(chat: str, llm, log_fn=None, force: bool = False) -> int:
    """每个会话每天最多写一次日记；失败会回滚，下次补写，不丢对话。

    游标（diary_cursor）保证同一段对话不会被整理两次，也不会因 keep_messages
    裁剪而漏掉。返回新增/更新的片段数。
    """
    say = log_fn or (lambda *a, **k: None)
    if not enabled() or not diary_mode():
        return 0
    today = time.strftime('%Y-%m-%d')
    prev_pending = _pending(chat)
    with _LOCK:
        if _STATE['extracting'] and not force:
            return 0
        if not force:
            if _meta_get('diary_day:' + chat) == today:
                return 0
            if prev_pending < int(_CFG.get('diary_min_msgs', 12) or 12):
                return 0
        _STATE['extracting'] = True
        _meta_set('diary_day:' + chat, today)
        _meta_set('pending:' + chat, '0')
    ok = False
    try:
        since = int(_meta_get('diary_cursor:' + chat) or 0)
        lines, cursor, day_label = diary_lines(chat, since)
        if len(lines) < 4:
            say('[日记] 「%s」新对话不足，暂不写' % chat, flush=True)
            return 0
        # 已记下的只带一小把去重，输入不会随记忆库一起膨胀
        known = [f['text'] for f in facts(chat, limit=6)]
        user = []
        if known:
            user.append('近期已记下（别重复）：')
            user += ['- ' + k for k in known]
        user.append('日期：' + day_label)
        user.append('对话：')
        user += lines
        raw = llm([{'role': 'system', 'content': _DIARY_SYSTEM},
                   {'role': 'user', 'content': '\n'.join(user)}])
        items = _compress_diary(_parse(raw), day_label)
        added = 0
        for who, what in items:
            r = add_fact(chat, who, what, kind='diary')
            if r in ('new', 'update'):
                added += 1
                say('[日记] %s %s：%s'
                    % ('新增' if r == 'new' else '更新', who or chat, what), flush=True)
        if added:
            say('[日记] 「%s」整理出 %d 条 / %d 字，入库 %d 条'
                % (chat, len(items), sum(len(w) for _, w in items), added),
                flush=True)
        _meta_set('diary_cursor:' + chat, str(cursor))
        _meta_set('last:' + chat, str(time.time()))
        ok = True
        return added
    except Exception as exc:
        say('[日记] 整理失败（不影响聊天）：%s: %s'
            % (type(exc).__name__, exc), flush=True)
        return 0
    finally:
        if not ok:
            # 回滚：别把游标推过去，否则这段对话永远丢了；明天继续补写
            _meta_set('diary_day:' + chat, '')
            _meta_set('pending:' + chat, str(prev_pending))
        with _LOCK:
            _STATE['extracting'] = False


# ---------------------------------------------------------------- 计数
def bump(chat: str) -> int:
    """记一条「有新对话」，返回待总结条数。"""
    if not enabled():
        return 0
    try:
        n = int(_meta_get('pending:' + chat) or 0) + 1
        _meta_set('pending:' + chat, str(n))
        return n
    except Exception:
        return 0


def _meta_get(k: str) -> str:
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return ''
        row = conn.execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
        return row[0] if row else ''
    except Exception:
        return ''
    finally:
        _close(conn)


def _meta_set(k: str, v: str) -> None:
    conn = None
    try:
        conn = _conn()
        if conn is None:
            return
        conn.execute('INSERT INTO meta(k, v) VALUES(?, ?) '
                     'ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, v))
        conn.commit()
    except Exception:
        pass
    finally:
        _close(conn)


def _pending(chat: str) -> int:
    try:
        return int(_meta_get('pending:' + chat) or 0)
    except Exception:
        return 0


def _close(conn) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 命令行
def _main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent
    configure(root, {})
    cmd = argv[1] if len(argv) > 1 else 'stats'
    if cmd == 'stats':
        st = stats()
        print('数据库：%s' % st['path'])
        print('模式：%s' % ('日记' if diary_mode() else '事实卡片'))
        print('记忆 %d 条（其中日记 %d 条）/ 历史 %d 条 / 涉及 %d 个会话'
              % (st['facts'], st['diary'], st['messages'], st['chats']))
        for c in chats():
            print('  · %s：%d 条' % (c, len(facts(c))))
    elif cmd == 'list':
        chat = argv[2] if len(argv) > 2 else ''
        for f in facts(chat):
            tag = '[日记] ' if f['kind'] == 'diary' else ''
            print('[%d] %s%s  %s' % (f['id'], tag, f['chat'], f['subject']))
            print('     ' + f['text'])
    elif cmd == 'clear':
        chat = argv[2] if len(argv) > 2 else ''
        print('已清空 %d 条（%s）' % (clear(chat), chat or '全部'))
    else:
        print('用法：python memstore.py [stats|list [会话]|clear [会话]]')
        return 2
    return 0


if __name__ == '__main__':
    import sys
    raise SystemExit(_main(sys.argv))

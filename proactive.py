# -*- coding: utf-8 -*-
"""主动发消息：没人说话的时候，由机器人自己挑一个会话开口。

职责边界
    * 本模块只负责**调度**：什么时候能发、发给谁、内容怎么让模型生成、
      发完怎么记账。模型调用与真正发送由 bot.py 注入，便于离线单测
      （给定时间和状态，断言它选不选、选谁），不需要微信窗口在场。

为什么要有这些闸门
    主动发消息最大的风险是「扰民」和「抢焦点」。所以默认叠了五层限制：
      1. 活跃时段之外不发（默认 08:00~23:00）
      2. 同一会话两次之间有最小间隔（min_gap_minutes）
      3. 单会话每日上限 + 全局每日上限
      4. 目标会话必须已经安静 idle_minutes 分钟（别人正在聊就不插嘴）
      5. 用户键鼠必须空闲 require_user_idle_seconds 秒（避免抢窗口）
    任何一层不满足都只是「这一轮跳过」，不会让线程退出。

状态持久化在 cache/proactive-state.json，重启后不会立刻重发一轮，
也不会把当天的额度清零。
"""
import json
import random
import re
import time
from datetime import date, datetime
from pathlib import Path

DEFAULT_TOPICS = [
    '今天吃了什么，随口问问',
    '最近有没有什么想去的地方',
    '吐槽一下天气',
    '分享一件今天遇到的小事',
    '问问对方最近在忙什么',
    '说一个刚想到的冷知识',
    '推荐一首最近在听的歌',
    '聊聊这周末打算怎么过',
]

# 模型在这轮要扮演「主动开口」而不是「回消息」，这些约束直接写进 system，
# 比事后清洗更有效；clean() 只是兜底。
RULES = """
【这一轮是主动发言】
不是你回别人的消息，而是你自己先开口。要求：
1. 像平时聊天一样，只发一条，控制在 {lo}~{hi} 字，不要分段。
2. 不要解释「我在主动找你」「好久没聊了」这类话，也不要加括号旁白。
3. 不要问「在吗」「有人吗」这种空话，直接用内容开场。
4. 不要用列表、标题、Markdown、表情符号堆砌。
5. 不要 @ 任何人，不要提「群」「机器人」「AI」这些词。
6. 语气要和你平时回消息完全一致，别突然变得热情。
"""


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def read_config(cfg_path) -> dict:
    """每次调用都重读，改 config.json 不用重启。"""
    try:
        d = json.loads(Path(cfg_path).read_text(encoding='utf-8-sig'))
    except Exception:
        return {}
    section = d.get('proactive')
    return section if isinstance(section, dict) else {}


def _hhmm(text) -> int | None:
    """'08:30' / '8:30' / '23' -> 分钟数；格式不对返回 None。"""
    s = str(text or '').strip()
    if not s:
        return None
    parts = s.split(':')
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if not (0 <= h <= 24 and 0 <= m <= 59):
        return None
    return h * 60 + m


def active_window(cfg: dict):
    """返回 (起, 止) 分钟；None 表示全天不限。支持跨午夜，如 18:00-02:00。"""
    spec = str(cfg.get('active_hours') or '').strip()
    if spec.lower() in ('', 'off', 'none', 'always', '24h', '0-24'):
        return None
    start_s, _, end_s = spec.replace('~', '-').replace('—', '-').partition('-')
    start, end = _hhmm(start_s), _hhmm(end_s)
    if start is None or end is None:
        return None
    return start, end


def in_active_window(cfg: dict, now: datetime | None = None) -> bool:
    win = active_window(cfg)
    if win is None:
        return True
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    start, end = win
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end          # 跨午夜


def window_text(cfg: dict) -> str:
    win = active_window(cfg)
    if win is None:
        return '全天'
    return '%02d:%02d~%02d:%02d' % (win[0] // 60, win[0] % 60, win[1] // 60, win[1] % 60)


def num(cfg: dict, key: str, default: float) -> float:
    """读数值配置。注意不能用 `value or default`：那样 0 会被当成没填，
    而 0 在 daily_limit / min_gap_minutes 里是有意义的（= 禁用 / 不限）。"""
    value = cfg.get(key, None)
    if value is None or value == '':
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def next_delay(cfg: dict) -> float:
    """下一轮检查的间隔（秒）。起点就随机，避免每次重启都是同一个节奏。"""
    lo = max(1.0, num(cfg, 'interval_min', 30))
    hi = max(lo, num(cfg, 'interval_max', 90))
    return random.uniform(lo, hi) * 60.0


# --------------------------------------------------------------------------
# 记账
# --------------------------------------------------------------------------

class State:
    """已发数量与时间。按天自动清零，写盘用临时文件替换，断电不会写坏。"""

    def __init__(self, path):
        self.path = Path(path)
        self.day = ''
        self.sent = {}
        self.last = {}
        self.texts = []
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.day = str(raw.get('day') or '')
        self.sent = {str(k): int(v) for k, v in (raw.get('sent') or {}).items()}
        self.last = {str(k): float(v) for k, v in (raw.get('last') or {}).items()}
        self.texts = [str(x) for x in (raw.get('texts') or [])][-20:]
        self._roll()

    def _roll(self):
        today = date.today().isoformat()
        if self.day != today:
            self.day = today
            self.sent = {}

    def save(self):
        self._roll()
        payload = {'day': self.day, 'sent': self.sent,
                   'last': self.last, 'texts': self.texts[-20:]}
        tmp = self.path.with_name(self.path.name + '.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding='utf-8')
            tmp.replace(self.path)
        except Exception:
            pass          # 记账失败不该影响发送本身

    def total_today(self) -> int:
        self._roll()
        return sum(self.sent.values())

    def mark(self, key: str, text: str, now: datetime | None = None):
        now = now or datetime.now()
        self._roll()
        self.sent[key] = self.sent.get(key, 0) + 1
        self.last[key] = now.timestamp()
        self.texts.append(text)
        self.save()


def check_target(state: State, key: str, cfg: dict,
                 now: datetime | None = None) -> tuple[bool, str]:
    """单目标是否还能发。返回 (能否, 不能的原因)。"""
    now = now or datetime.now()
    gap_min = max(1.0, num(cfg, 'min_gap_minutes', 45))
    since = now.timestamp() - float(state.last.get(key, 0) or 0)
    if since < gap_min * 60:
        return False, '距上次仅 %.0f 分钟（需 %g）' % (since / 60, gap_min)
    per_cap = int(num(cfg, 'per_target_daily', 6))
    if state.sent.get(key, 0) >= per_cap:
        return False, '该会话今日已发 %d 条（上限 %d）' % (state.sent.get(key, 0), per_cap)
    total_cap = int(num(cfg, 'daily_limit', 14))
    if state.total_today() >= total_cap:
        return False, '今日总量已达上限 %d' % total_cap
    return True, ''


def pick_target(cfg: dict, state: State, now: datetime | None = None,
                allow=None, targets=None):
    """按权重随机挑一个还能发的目标。allow(target) 为额外的可行性过滤。

    targets 必须是**展开后的**具体会话列表（每项至少含 key / label / kind / name），
    因为配置里写的 {"type":"group"} 之类还没有具体会话名。不传时才退回读 cfg，
    那种情况只有调用方自己把 name 填好了才有效。
    """
    now = now or datetime.now()
    items = targets if targets is not None else (cfg.get('targets') or [])
    cands, why = [], []
    for t in items:
        if not isinstance(t, dict) or t.get('enabled') is False:
            continue
        key = str(t.get('key') or '')
        if not key:
            continue
        ok, reason = check_target(state, key, cfg, now)
        if not ok:
            why.append('%s：%s' % (t.get('label') or key, reason))
            continue
        if allow is not None and not allow(t):
            why.append('%s：会话不满足附加条件' % (t.get('label') or key))
            continue
        cands.append(t)
    if not cands:
        return None, why
    weights = [max(0.01, float(t.get('weight', 1) or 1)) for t in cands]
    return random.choices(cands, weights=weights, k=1)[0], why


# --------------------------------------------------------------------------
# 内容
# --------------------------------------------------------------------------

def pick_topic(cfg: dict) -> str:
    pool = cfg.get('topics')
    if not isinstance(pool, list) or not pool:
        pool = DEFAULT_TOPICS
    return str(random.choice(pool))


def build_messages(persona: str, target: dict, recent: str, topic: str,
                   cfg: dict) -> list:
    maxc = max(20, int(cfg.get('max_chars', 60) or 60))
    lo = max(8, maxc // 3)
    rules = RULES.format(lo=lo, hi=maxc)
    system = (persona or '') + rules
    who = '群里的朋友' if target.get('kind') == 'group' else '对方'
    if recent.strip():
        lead = ('（你和%s最近的聊天记录：\n%s\n\n'
                '现在安静了一会儿，你主动说一句话。'
                '可以顺着上面的话头接着聊，也可以换个新话题，别复述他们说过的话。）'
                % (who, recent.strip()))
    else:
        lead = ('（你和%s最近没怎么说话。现在你主动开口，'
                '参考方向：%s。说得随意一点，像随口一提。）' % (who, topic))
    return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': lead}]


_LEAD_NOISE = re.compile(r'^\s*(?:[-*•]\s*|(?:小鲸|机器人|助手|AI|assistant|bot)\s*[:：]\s*)',
                         re.IGNORECASE)


def clean(text: str, cfg: dict) -> str:
    """去掉模型常见的多余包装：列表符号、引号、自称前缀、Markdown 强调。"""
    t = str(text or '').strip()
    t = re.sub(r'^\s*[（(][^）)]{0,20}[）)]\s*', '', t)     # 开头旁白
    t = _LEAD_NOISE.sub('', t)
    t = re.sub(r'[*_`]{1,3}', '', t)
    t = t.strip().strip('“”"\'`「」『』')
    t = re.sub(r'\s*\n+\s*', ' ', t)
    t = re.sub(r'\s{2,}', ' ', t).strip()
    maxc = max(20, int(cfg.get('max_chars', 60) or 60))
    if len(t) > maxc:
        cut = t[:maxc]
        for sep in ('。', '！', '？', '，', '…', ' '):
            pos = cut.rfind(sep)
            if pos >= maxc // 2:
                return cut[:pos + 1] if sep not in '，… ' else cut[:pos] + '…'
        return cut.rstrip('，,、') + '…'
    return t


def too_similar(text: str, recent_texts, threshold: float = 0.62) -> bool:
    """字符二元组 Jaccard，粗筛「又是这一句」。"""
    def grams(s):
        s = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', str(s or ''))
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}
    a = grams(text)
    if not a:
        return False
    for old in (recent_texts or [])[-12:]:
        b = grams(old)
        if not b:
            continue
        inter = len(a & b)
        union = len(a | b)
        if union and inter / union >= threshold:
            return True
    return False


# --------------------------------------------------------------------------
# 周边
# --------------------------------------------------------------------------

def user_idle_seconds() -> float:
    """距上一次键鼠操作的秒数。拿不到时返回一个很大的值（视为空闲）。"""
    try:
        import ctypes

        class _LII(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]

        info = _LII()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 1e9
        now = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, (now - info.dwTime) / 1000.0)
    except Exception:
        return 1e9


_RICH_KINDS = (
    ('<emoji', '[表情]'),
    ('<img', '[图片]'),
    ('<videomsg', '[视频]'),
    ('<voicemsg', '[语音]'),
    ('<appmsg', '[链接或卡片]'),
    ('<voip', '[语音通话]'),
    ('<sysmsg', '[系统消息]'),
)


def plain_content(content) -> str:
    """把消息正文压成一句人话。

    微信的图片 / 表情 / 链接在库里是整段 XML，直接把原文喂给模型既浪费
    token，又会把 wxid、aeskey 这类东西漏进 prompt。这里统一换成占位符。
    """
    c = str(content or '').strip()
    if not c:
        return ''
    # 群聊里「@某人」的正文前面会挂发送者 wxid，如 "wxid_abc123: @张三 看这个"
    c = re.sub(r'^wxid_[0-9A-Za-z_\-]{4,}\s*:\s*', '', c).strip()
    if not c:
        return ''
    low = c.lower()
    for tag, label in _RICH_KINDS:
        if tag in low:
            if tag == '<appmsg':
                m = re.search(r'<title>(.{1,40}?)</title>', c, re.S)
                return label + (m.group(1).strip() if m else '')
            return label
    if low.startswith(('<?xml', '<msg')):
        return '[系统消息]'
    if c.startswith('<') and '</' in c:
        return '[图片或表情]'
    c = re.sub(r'<[^>]{0,120}>', '', c)          # 残留标签
    return re.sub(r'\s+', ' ', c).strip()


def recent_lines(rows, self_id: str, is_group: bool, limit: int = 8,
                 nick_of=None) -> str:
    """把 DB 里的最近消息压成几行，喂给模型当上下文。

    nick_of(wxid) -> 昵称，由调用方注入（需要 DB）。解析不到就退回「群友」，
    绝不把 wxid 原样交给模型。
    """
    out = []
    for row in list(rows or [])[-limit:]:
        content = plain_content(row.get('content'))
        if not content:
            continue
        sender = str(row.get('sender_username') or '')
        if sender and sender == self_id:
            who = '我'
        elif is_group:
            who = ''
            if nick_of is not None:
                try:
                    who = str(nick_of(sender) or '')
                except Exception:
                    who = ''
            if not who or who == sender or who.startswith('wxid_'):
                who = str(row.get('sender_nickname') or row.get('sender_nick') or '')
            who = (who or '群友')[:10]
        else:
            who = '对方'
        out.append('%s：%s' % (who, content[:80]))
    return '\n'.join(out)


def send_text(gui, name: str, text: str):
    """走 GUI 发送，和主线程共用同一把 UI 锁，避免点两下。

    verify=True 会回读数据库确认落地；失败时抛异常由调用方记录。
    """
    from wechatauto.utils import uilock

    @uilock
    def _do():
        return gui.send_msg(text, who=name, verify=True)

    resp = _do()
    ok = bool(resp)
    msg = ''
    try:
        msg = str(resp.get('message') or '')
    except Exception:
        msg = str(resp)
    if not ok:
        raise RuntimeError(msg or '发送失败')
    return msg

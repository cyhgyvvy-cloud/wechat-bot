# -*- coding: utf-8 -*-
"""主动发消息的离线自检：只验调度、闸门、内容清洗，不发任何消息。

用法：
  python diag_proactive.py            # 跑全部用例
  python diag_proactive.py --plan     # 只看当前配置会怎么排
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import proactive  # noqa: E402

OK, FAIL = [], []


def check(name, cond, detail=''):
    (OK if cond else FAIL).append(name)
    print(('  [OK]   ' if cond else '  [FAIL] ') + name + (('  ' + detail) if detail else ''))


def t_window():
    print('\n[1] 活跃时段')
    cfg = {'active_hours': '08:30-23:00'}
    check('09:00 在窗口内', proactive.in_active_window(cfg, datetime(2026, 1, 1, 9, 0)))
    check('07:00 在窗口外', not proactive.in_active_window(cfg, datetime(2026, 1, 1, 7, 0)))
    check('23:30 在窗口外', not proactive.in_active_window(cfg, datetime(2026, 1, 1, 23, 30)))
    check('08:30 准点算在内', proactive.in_active_window(cfg, datetime(2026, 1, 1, 8, 30)))
    night = {'active_hours': '18:00-02:00'}
    check('跨午夜 23:00 在内', proactive.in_active_window(night, datetime(2026, 1, 1, 23, 0)))
    check('跨午夜 01:00 在内', proactive.in_active_window(night, datetime(2026, 1, 1, 1, 0)))
    check('跨午夜 03:00 在外', not proactive.in_active_window(night, datetime(2026, 1, 1, 3, 0)))
    check('空配置视为全天', proactive.in_active_window({}, datetime(2026, 1, 1, 4, 0)))
    check('乱写格式视为全天', proactive.in_active_window({'active_hours': '瞎写'},
                                                         datetime(2026, 1, 1, 4, 0)))
    check('窗口文本', proactive.window_text(cfg) == '08:30~23:00', proactive.window_text(cfg))


def t_delay():
    print('\n[2] 间隔随机')
    cfg = {'interval_min': 30, 'interval_max': 90}
    vals = [proactive.next_delay(cfg) / 60 for _ in range(400)]
    check('全部落在 30~90 分钟', all(30 <= v <= 90 for v in vals),
          '最小 %.1f 最大 %.1f' % (min(vals), max(vals)))
    check('确实随机（不是定值）', max(vals) - min(vals) > 30)
    same = [proactive.next_delay({'interval_min': 12, 'interval_max': 12}) / 60
            for _ in range(5)]
    check('min=max 时固定', all(abs(v - 12) < 1e-6 for v in same))


def t_state(tmp):
    print('\n[3] 记账与跨天')
    path = Path(tmp) / 'state.json'
    cfg = {'min_gap_minutes': 45, 'per_target_daily': 2, 'daily_limit': 3}
    st = proactive.State(path)
    now = datetime.now()

    ok, why = proactive.check_target(st, 'group:A', cfg, now)
    check('全新状态可发', ok, why)

    st.mark('group:A', '第一句', now)
    ok, why = proactive.check_target(st, 'group:A', cfg, now)
    check('刚发完被最小间隔挡住', not ok, why)
    ok, _ = proactive.check_target(st, 'group:A', cfg, now + timedelta(minutes=50))
    check('过了 45 分钟可再发', ok)

    st.mark('group:A', '第二句', now)
    ok, why = proactive.check_target(st, 'group:A', cfg, now + timedelta(minutes=50))
    check('单会话日上限生效', not ok, why)

    st.mark('private:B', '第三句', now)
    ok, why = proactive.check_target(st, 'private:C', cfg, now + timedelta(minutes=50))
    check('全局日上限生效', not ok, why)

    st2 = proactive.State(path)
    check('重启后计数仍在', st2.sent.get('group:A') == 2, str(st2.sent))
    check('重启后最近文本仍在', '第三句' in st2.texts)

    st2.day = '2000-01-01'
    st2.save()
    st3 = proactive.State(path)
    check('跨天后计数清零', st3.sent == {}, str(st3.sent))
    check('跨天后保留最后一次时间戳', bool(st3.last))


def t_pick():
    print('\n[4] 选目标')
    tmp = Path(tempfile.mkdtemp())
    st = proactive.State(tmp / 's.json')
    cfg = {
        'min_gap_minutes': 1, 'per_target_daily': 10, 'daily_limit': 100,
        'targets': [
            {'key': 'group:测试群', 'label': '群聊 测试群', 'kind': 'group', 'name': '测试群', 'weight': 1},
            {'key': 'private:甲', 'label': '私聊 甲', 'kind': 'private', 'name': '甲', 'weight': 1},
        ],
    }
    seen = set()
    for _ in range(200):
        t, _why = proactive.pick_target(cfg, st)
        seen.add(t['key'])
    check('两个目标都会被选中', len(seen) == 2, str(seen))

    t, why = proactive.pick_target(cfg, st, allow=lambda x: x['kind'] == 'group')
    check('allow 过滤生效', t is not None and t['kind'] == 'group')

    t, why = proactive.pick_target(cfg, st, allow=lambda x: False)
    check('全部被过滤时返回 None', t is None)
    check('给出跳过原因', len(why) == 2, str(why))

    capped = dict(cfg, daily_limit=0)
    t, _ = proactive.pick_target(capped, st)
    check('日上限 0 时不出手', t is None)

    # 回归：配置里写的是 {"type":"group"} 这种「规格」，没有具体会话名，
    # 必须由调用方展开后传入，否则会被当成无效目标全部跳过（曾因此不发消息）。
    raw_cfg = {'min_gap_minutes': 1, 'per_target_daily': 10, 'daily_limit': 100,
               'targets': [{'type': 'group', 'weight': 1}]}
    expanded = [{'key': 'group:测试群', 'label': '群聊 测试群', 'kind': 'group',
                 'name': '测试群', 'weight': 1}]
    st2 = proactive.State(Path(tempfile.mkdtemp()) / 's2.json')
    t_raw, why_raw = proactive.pick_target(raw_cfg, st2)
    check('原始 spec 没有 key，不会被误选', t_raw is None, str(why_raw))
    t_exp, _ = proactive.pick_target(raw_cfg, st2, targets=expanded)
    check('传入展开列表后能选中', t_exp is not None and t_exp['key'] == 'group:测试群',
          str(t_exp))


def t_content():
    print('\n[5] 内容构造与清洗')
    persona = '你是一个随和的女生，说话简短。'
    target = {'kind': 'group', 'name': '测试群', 'label': '群聊 测试群'}
    cfg = {'max_chars': 40}

    m1 = proactive.build_messages(persona, target, '我：在干嘛\n对方：加班', '', cfg)
    check('有历史时 system 含人设', persona in m1[0]['content'])
    check('有历史时要求衔接', '最近的聊天记录' in m1[1]['content'])
    m2 = proactive.build_messages(persona, target, '', '聊聊天气', cfg)
    check('无历史时用话题', '聊聊天气' in m2[1]['content'])
    check('约束里带字数上限', '40' in m1[0]['content'])

    cases = [
        ('- 今天好热啊', '今天好热啊'),
        ('小鲸：今天好热啊', '今天好热啊'),
        ('**今天好热啊**', '今天好热啊'),
        ('「今天好热啊」', '今天好热啊'),
        ('（小声）今天好热啊', '今天好热啊'),
        ('第一行\n第二行', '第一行 第二行'),
        ('', ''),
    ]
    for src, want in cases:
        got = proactive.clean(src, cfg)
        check('清洗 %r -> %r' % (src[:12], want[:12]), got == want, '实际 %r' % got)

    long_text = '今天真的好热啊，空调开了一整天，电费都要交不起了，你说这天气还有没有天理了'
    cut = proactive.clean(long_text, {'max_chars': 20})
    check('超长会截断且不超限', len(cut) <= 21, '%d 字：%s' % (len(cut), cut))

    a = '今天好热啊，空调开了一整天'
    b = '今天好热啊，空调开了一整天呢'
    c = '刚发现楼下新开了家面馆'
    check('近似句判重', proactive.too_similar(b, [a]))
    check('不同句不判重', not proactive.too_similar(c, [a]))


def t_context():
    print('\n[6] 上下文压缩')
    rows = [
        {'sender_username': 'me', 'content': '我先说一句'},
        {'sender_username': 'u2', 'content': '<msg><img aeskey="3b345b" src="x"/></msg>'},
        {'sender_username': 'u3', 'content': '  今天   好热  '},
        {'sender_username': 'u4', 'content':
            '<?xml version="1.0"?><msg><appmsg><title>有那么好笑吗</title></appmsg></msg>'},
        {'sender_username': 'u5', 'content': '<msg><emoji md5="abc" /></msg>'},
        {'sender_username': 'u3', 'content': 'wxid_abc123def456: @小王 本来想给你看的'},
    ]
    nicks = {'u2': '小王', 'u3': '小李', 'u4': '小张', 'u5': '小赵'}
    out = proactive.recent_lines(rows, 'me', True, limit=8,
                                 nick_of=lambda w: nicks.get(w, ''))
    flat = out.replace('\n', ' | ')
    check('自己显示为「我」', '我：我先说一句' in out, flat)
    check('图片压成占位符', '[图片]' in out)
    check('表情压成占位符', '[表情]' in out)
    check('链接保留标题', '[链接或卡片]有那么好笑吗' in out)
    check('不泄漏 aeskey', 'aeskey' not in flat)
    check('不泄漏 wxid', 'wxid_' not in flat and 'u2' not in flat.replace('u2：', ''))
    check('剥掉 @ 消息的 wxid 前缀', '小李：@小王 本来想给你看的' in out, flat)
    check('XML 原文不出现', '<?xml' not in flat and '<msg>' not in flat)
    check('昵称已解析', all(n in out for n in ('小王', '小李', '小张', '小赵')))
    check('空白被压平', '今天 好热' in out)

    out_nn = proactive.recent_lines(rows[:3], 'me', True, limit=5)
    check('没有 nick_of 时不崩且不出现 wxid', '群友' in out_nn and 'wxid_' not in out_nn,
          out_nn.replace('\n', ' | '))

    out_p = proactive.recent_lines([{'sender_username': 'u1', 'content': '在吗'}],
                                   'me', False, limit=3)
    check('私聊显示「对方」', out_p == '对方：在吗', out_p)


def t_plan():
    print('\n[7] 当前配置的排期')
    cfg = proactive.read_config(ROOT / 'config.json')
    if not cfg:
        print('  config.json 里没有 proactive 段')
        return
    print('  启用:', cfg.get('enabled'))
    print('  活跃时段:', proactive.window_text(cfg))
    print('  间隔: %s~%s 分钟' % (cfg.get('interval_min'), cfg.get('interval_max')))
    print('  日上限: 总 %s 条 / 单会话 %s 条' % (cfg.get('daily_limit'),
                                            cfg.get('per_target_daily')))
    print('  同会话最小间隔: %s 分钟' % cfg.get('min_gap_minutes'))
    print('  要求会话安静: %s 分钟' % cfg.get('idle_minutes'))
    print('  要求键鼠空闲: %s 秒' % cfg.get('require_user_idle_seconds'))
    print('  目标:', json.dumps(cfg.get('targets'), ensure_ascii=False))
    lo = float(cfg.get('interval_min') or 30)
    hi = float(cfg.get('interval_max') or 90)
    win = proactive.active_window(cfg)
    span = (win[1] - win[0]) % 1440 if win else 1440
    est = span / ((lo + hi) / 2)
    print('  粗估: 活跃 %d 分钟内约 %d 轮检查，受日上限 %s 约束'
          % (span, round(est), cfg.get('daily_limit')))


def main():
    print('=' * 62)
    print('主动发消息 · 离线自检')
    print('=' * 62)
    tmp = tempfile.mkdtemp()
    t_window()
    t_delay()
    t_state(tmp)
    t_pick()
    t_content()
    t_context()
    t_plan()
    print('\n' + '=' * 62)
    print('通过 %d 项，失败 %d 项' % (len(OK), len(FAIL)))
    if FAIL:
        print('失败用例：')
        for n in FAIL:
            print('  -', n)
    print('=' * 62)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    sys.exit(main())

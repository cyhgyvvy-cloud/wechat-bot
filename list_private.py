# -*- coding: utf-8 -*-
"""列出微信里的私聊对象，用于填写 config.json 的 private_chats。

微信的 list_message_chats 给出「备注优先、无备注取昵称」的显示名，
与 username_by_nickname 的匹配规则一致，因此这里列出的名字可以直接填进配置。

用法：
    python list_private.py [输出文件]
默认写到脚本同目录的 list-private.txt（UTF-8 with BOM，记事本可读）。
"""
import paths
import json
import sys
from pathlib import Path

ROOT = paths.ROOT
sys.path.insert(0, str(ROOT))


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'list-private.txt'
    cfg_path = ROOT / 'config.json'
    if not cfg_path.exists():
        print('找不到 config.json：%s' % cfg_path, file=sys.stderr)
        return 2
    cfg = json.loads(cfg_path.read_text(encoding='utf-8-sig'))
    group = str(cfg.get('group_name') or '').strip()
    current = [str(x).strip() for x in cfg.get('private_chats', []) if str(x).strip()]

    import bot  # 顶层有 __main__ 保护，导入安全

    try:
        db, _, _ = bot.locate_account(group)
    except Exception as exc:
        print('定位微信账号失败：%s' % exc, file=sys.stderr)
        print('请确认微信已登录并保持窗口可见。', file=sys.stderr)
        return 3

    me = str((db.get_self_info() or {}).get('username') or '')
    rows = []
    for it in db.list_message_chats():
        user = str(it.get('username') or '')
        if not user or user == me or user.endswith('@chatroom'):
            continue
        name = str(it.get('name') or user).strip() or user
        cnt = int(it.get('message_count') or 0)
        rows.append((cnt, name, user))
    rows.sort(key=lambda x: (-x[0], x[1]))

    resolved = {n: db.username_by_nickname(n) for n in current}

    lines = []
    add = lines.append
    add('=== 私聊对象清单（可直接填进 config.json 的 private_chats）===')
    add('共 %d 个私聊会话，按消息条数排序。名字 = 备注优先，无备注取昵称。' % len(rows))
    add('')
    for cnt, name, user in rows:
        flag = '已加入' if name in current else '可加入'
        add('[%s] %-24s %-28s 消息 %d' % (flag, name, user, cnt))
    add('')
    add('=== 当前配置 private_chats ===')
    if not current:
        add('(空) —— 空列表表示完全不回复任何私聊')
    for n in current:
        wxid = resolved.get(n)
        if wxid:
            add('  OK      %s -> %s' % (n, wxid))
        else:
            add('  未匹配  %s  ← 这个名字在通讯录里找不到，该条目不会生效' % n)
    add('')
    add('提示：')
    add('1) 有备注的联系人必须填「备注」，填他的原始昵称匹配不上。')
    add('2) 改完 private_chats 必须重启机器人（停止后台.vbs -> 启动后台.vbs），')
    add('   监听是在启动时注册的，改文件不会热生效。')
    add('3) 想把某人从名单删掉，直接删那一项；整行留 [] 就是全部私聊都不回。')

    out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    print('已写入：%s' % out_path)
    print('私聊会话 %d 个，当前名单 %d 项（其中 %d 项未匹配）'
          % (len(rows), len(current), sum(1 for v in resolved.values() if not v)))
    return 0


if __name__ == '__main__':
    sys.exit(main())

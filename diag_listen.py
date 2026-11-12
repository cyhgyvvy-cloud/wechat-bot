# -*- coding: utf-8 -*-
"""监听探针：验证 AddListenChat 是否能收到新消息（发给自己，不打扰别人）。"""
import paths
import json
import sys
import time
from pathlib import Path

ROOT = paths.ROOT
sys.path.insert(0, str(ROOT))

import bot            # noqa: E402
from wechatauto import WeChat   # noqa: E402

TARGET = '文件传输助手'
got = []


def main():
    group = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))['group_name']
    db, _, _ = bot.locate_account(group)
    wc = WeChat(db=db)

    def cb(msg, chat):
        got.append((time.time(), msg))
        print(f'  回调 {time.strftime("%H:%M:%S")}  who={getattr(chat, "who", "")!r} '
              f'type={getattr(msg, "type", "")} attr={getattr(msg, "attr", "")} '
              f'sender={getattr(msg, "sender_wxid", "")} content={str(getattr(msg, "content", ""))[:40]!r}',
              flush=True)

    r = wc.AddListenChat(nickname=TARGET, callback=cb)
    print('注册结果:', r, flush=True)

    from wechatauto.guia import WeChatGUI
    gui = WeChatGUI()
    uia = gui._get_uia()
    if uia is None or not uia.open_chat(TARGET):
        print('[x] 打不开会话')
        return 1
    print('已切到', uia.current_chat(), '，3 秒后自发一条消息…', flush=True)
    time.sleep(3)
    from wechatauto import Chat
    chat = Chat(who=TARGET, db=db)
    sent_at = time.time()
    ok = chat.SendMsg('监听探针 ' + str(int(sent_at) % 10000))
    print(f'发送结果 {ok}，等待回调 8 秒…', flush=True)
    for _ in range(80):
        if got:
            break
        time.sleep(0.1)
    if got:
        delay = got[0][0] - sent_at
        print(f'\n✅ 监听正常：自发消息在 {delay:.2f}s 后触发回调')
        return 0
    print('\n❌ 8 秒内没有回调：监听/DB 轮询这条路有问题')
    return 2


if __name__ == '__main__':
    sys.exit(main())

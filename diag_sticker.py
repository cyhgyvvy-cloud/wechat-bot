# -*- coding: utf-8 -*-
"""表情包快路径实测：发到「文件传输助手」，测量从调用到落库的真实耗时。"""
import paths
import json
import re
import sys
import time
from pathlib import Path

ROOT = paths.ROOT
sys.path.insert(0, str(ROOT))

import bot        # noqa: E402
import sticker    # noqa: E402

TARGET = '文件传输助手'


def main():
    cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
    sdir = (cfg.get('sticker') or {}).get('dir', '')
    lib = sticker.load_library(sdir)
    hit = sticker.pick(lib, '傲娇', [], animated=False)
    if not hit:
        print('[x] 图库为空')
        return 1
    path, cat = hit
    print(f'选图：{cat}/{path.name}（{path.stat().st_size} B）')

    from wechatauto.guia import WeChatGUI
    gui = WeChatGUI()
    uia = gui._get_uia()
    if uia is None or not uia.open_chat(TARGET):
        print('[x] 打不开会话')
        return 1
    print(f'已切到会话：{uia.current_chat()!r}')

    db, _, _ = bot.locate_account(cfg.get('group_name') or '')
    before = db.get_messages('filehelper', limit=1)
    base_seq = int(before[0].get('sort_seq') or 0) if before else 0
    print(f'发送前最高 sort_seq={base_seq}')

    t0 = time.perf_counter()
    sticker.fast_send(path, wait=0.8)
    t_click = time.perf_counter() - t0
    print(f'fast_send 返回（已回车）：{t_click:.2f}s')

    found_at = None
    for _ in range(24):
        rows = db.get_messages('filehelper', limit=2)
        for r in rows:
            if int(r.get('sort_seq') or 0) > base_seq:
                found_at = time.perf_counter() - t0
                content = str(r.get('content') or '')[:80].replace('\n', ' ')
                print(f'已落库：{found_at:.2f}s  type={r.get("type")}  {content}')
                break
        if found_at:
            break
        time.sleep(0.1)
    if found_at is None:
        print('[!] 12 秒内没在 DB 里看到新消息')
        return 2
    print(f'\n结论：端到端 {found_at:.2f}s（其中动作 {t_click:.2f}s，落库确认 +{found_at - t_click:.2f}s）')
    return 0


if __name__ == '__main__':
    sys.exit(main())

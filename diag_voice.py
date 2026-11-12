# -*- coding: utf-8 -*-
"""语音链路诊断：把语音发到「文件传输助手」（发给自己），打印完整时间线与 .result。

用法：
  python diag_voice.py             # 发一条自检语音，打印完整时间线
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import voice         # noqa: E402

TARGET = '文件传输助手'
JOBS = Path(r'D:\Bots\LegacyVoice\data\voice-send-jobs')


class Tail(threading.Thread):
    """后台盯 job 目录，记录每个文件的出现时间。"""

    def __init__(self, d: Path):
        super().__init__(daemon=True)
        self.d = d
        self.t0 = time.monotonic()
        self.seen = {}
        self.lines = []

    def run(self):
        while not self.stop_flag:
            try:
                for p in self.d.iterdir():
                    k = p.name
                    if k not in self.seen:
                        self.seen[k] = time.monotonic() - self.t0
                        self.lines.append(f'  {self.seen[k]:6.2f}s  出现 {k}  ({p.stat().st_size} B)')
            except OSError:
                pass
            time.sleep(0.05)

    stop_flag = False


def main():
    cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
    vcfg = cfg.get('voice') or {}
    root = vcfg.get('legacy_dir') or voice.DEFAULT_ROOT

    from wechatauto.guia import WeChatGUI
    gui = WeChatGUI()
    hwnd = gui.main_hwnd
    print(f'微信窗口 hwnd={hwnd}', flush=True)
    uia = gui._get_uia()
    if uia is None:
        print('[x] UIA 不可用，无法确认会话', flush=True)
        return 1
    cur = uia.current_chat()
    print(f'当前会话：{cur!r}', flush=True)
    if not uia.open_chat(TARGET):
        print(f'[x] 打不开会话 {TARGET}', flush=True)
        return 1
    print(f'已切到会话：{uia.current_chat()!r}', flush=True)

    JOBS.mkdir(parents=True, exist_ok=True)
    for p in list(JOBS.iterdir()):
        try:
            p.unlink()
        except OSError:
            pass

    tail = Tail(JOBS)
    tail.start()

    start = time.monotonic()
    text = '这是一条语音链路自检，如果你听到这段字，说明录音和发送都正常。'
    ok, outcome = voice.send(text, TARGET, gui,
                             ROOT / 'config.json',
                             log=lambda *a: print('  [log]', *a, flush=True))
    tail.stop_flag = True
    time.sleep(0.15)
    total = time.monotonic() - start

    print(f'\n结果：ok={ok}  耗时={total:.1f}s')
    print('outcome 全文：' + json.dumps(outcome, ensure_ascii=False))
    print('\njob 目录时间线：')
    for line in tail.lines:
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())

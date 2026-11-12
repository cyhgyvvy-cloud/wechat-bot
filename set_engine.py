# -*- coding: utf-8 -*-
"""把 config.json 的 voice 段切到独立引擎，并补齐 TTS 相关字段（幂等）。"""
import json
import pathlib
import shutil
import sys

BOT = pathlib.Path(__file__).resolve().parent
CFG = BOT / 'config.json'

WANT = {
    'engine': 'native',
    'tts_base': 'https://api.breeze.blue/v1',
    'tts_model': 'breeze-tts-2-multilingual',
    'tts_voice': 'voc_u3nxkd5578wc',
    'out_device': 'CABLE Input',
    'hotkey': 'Ctrl+Tab',
    'lead_in_ms': 400,
    'tail_ms': 600,
}


def main():
    if not CFG.is_file():
        print('FAIL config.json 不存在')
        return 2
    data = json.loads(CFG.read_text(encoding='utf-8-sig'))
    voice = data.setdefault('voice', {})
    added = {}
    for k, v in WANT.items():
        if k not in voice or voice[k] in (None, ''):
            voice[k] = v
            added[k] = v
    if added:
        shutil.copy2(str(CFG), str(CFG) + '.engine.bak')
        CFG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        print('RESULT=ADDED', json.dumps(added, ensure_ascii=False))
    else:
        print('RESULT=ALREADY_SET')
    print('ENGINE=%s  OUT_DEVICE=%s  HOTKEY=%s' % (
        voice.get('engine'), voice.get('out_device'), voice.get('hotkey')))
    return 0


if __name__ == '__main__':
    sys.exit(main())

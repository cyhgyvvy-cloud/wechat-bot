# -*- coding: utf-8 -*-
"""生成机器人自己的 TTS 密钥文件 breeze.key（DPAPI 按机器+用户加密）。

用法：
  python gen_voice_key.py                # 从旧版程序的密钥迁移（需旧版程序仍在）
  python gen_voice_key.py brz_xxxxxxxx   # 直接写入明文密钥（会自动加密）
"""
import paths
import json
import pathlib
import sys

import win32crypt

BOT = paths.ROOT
DST = BOT / 'breeze.key'
DEF_ROOT = pathlib.Path(r'D:\Bots\LegacyVoice')


def wg_root() -> pathlib.Path:
    try:
        cfg = json.loads((BOT / 'config.json').read_text(encoding='utf-8-sig'))
        p = (cfg.get('voice') or {}).get('legacy_dir')
        if p:
            return pathlib.Path(str(p))
    except Exception:
        pass
    return DEF_ROOT


def encrypt(text: str) -> str:
    blob = win32crypt.CryptProtectData(text.encode('utf-8'), None, None, None, None, 0)
    return blob.hex()


def verify() -> str:
    blob = bytes.fromhex(DST.read_text(encoding='ascii').strip())
    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode('utf-8')


def main():
    if len(sys.argv) > 1 and sys.argv[1].strip().startswith('brz_'):
        key = sys.argv[1].strip()
        DST.write_text(encrypt(key), encoding='ascii')
        print('RESULT=WROTE_FROM_ARG')
    else:
        src = wg_root() / 'data' / 'voice-send-breeze.key'
        if src.is_file():
            try:
                plain = win32crypt.CryptUnprotectData(
                    src.read_bytes(), None, None, None, 0)[1].decode('utf-8')
            except Exception as exc:
                print('FAIL 旧密钥无法解密：%r' % (exc,))
                return 2
            DST.write_text(encrypt(plain), encoding='ascii')
            print('RESULT=MIGRATED_FROM_LEGACY')
        elif DST.is_file():
            print('RESULT=ALREADY_PRESENT')
        else:
            print('FAIL 找不到旧版程序密钥，也没有传入明文密钥')
            return 3

    got = verify()
    print('KEY_OK len=%d prefix=%s***' % (len(got), got[:7]))
    return 0


if __name__ == '__main__':
    sys.exit(main())

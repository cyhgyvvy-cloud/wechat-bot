# -*- coding: utf-8 -*-
"""独立语音发送引擎：不依赖任何外部程序。

链路：
  1. breeze TTS 合成（wav）
  2. miniaudio 把音频播到指定的输出设备（默认 CABLE Input）
  3. 微信窗口置前，按住「按住说话」热键开始录音
  4. 播完、松开热键，微信把录音发成语音气泡

依赖：requests、miniaudio、pywin32（只用于解开本机的 DPAPI 密钥）。
"""
from __future__ import annotations

import array
import ctypes
import time
from pathlib import Path

DEFAULTS = {
    'tts_base': 'https://api.breeze.blue/v1',
    'tts_model': 'breeze-tts-2-multilingual',
    'tts_voice': 'voc_u3nxkd5578wc',
    'out_device': 'CABLE Input',
    'hotkey': 'Ctrl+Tab',
    'lead_in_ms': 400,
    'tail_ms': 600,
    'synth_timeout': 30,
}

VK = {
    'ctrl': 0x11, 'control': 0x11, 'shift': 0x10, 'alt': 0x12, 'win': 0x5B,
    'tab': 0x09, 'space': 0x20, 'enter': 0x0D, 'esc': 0x1B, 'capslock': 0x14,
}

_user32 = ctypes.windll.user32


class VoiceNativeError(RuntimeError):
    pass


# ---------------------------------------------------------------- 配置 / 密钥
def cfg_of(voice_cfg: dict) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = (voice_cfg or {}).get(k)
        if v not in (None, ''):
            out[k] = v
    return out


def key_path(bot_dir) -> Path:
    return Path(bot_dir) / 'breeze.key'


def load_key(bot_dir) -> str:
    p = key_path(bot_dir)
    if not p.is_file():
        raise VoiceNativeError(
            '缺少 TTS 密钥文件 %s（请运行「生成语音密钥.bat」）' % p.name)
    try:
        import win32crypt
    except ImportError as exc:
        raise VoiceNativeError('缺少 pywin32，无法解密密钥：%s' % exc)
    blob = bytes.fromhex(p.read_text(encoding='ascii').strip())
    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode('utf-8')


# ---------------------------------------------------------------- TTS
def synth(text: str, key: str, cfg: dict, log=print) -> bytes:
    import requests

    url = '%s/text-to-speech/%s' % (str(cfg['tts_base']).rstrip('/'), cfg['tts_voice'])
    body = {'text': str(text), 'model_id': cfg['tts_model']}
    headers = {'Authorization': 'Bearer ' + key}
    t0 = time.monotonic()
    try:
        r = requests.post(url, headers=headers, params={'output_format': 'wav'},
                          json=body, timeout=float(cfg['synth_timeout']))
    except Exception as exc:
        raise VoiceNativeError('TTS 请求失败：%s: %s' % (type(exc).__name__, exc))
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        raise VoiceNativeError('TTS 返回 HTTP %s：%s' % (r.status_code, r.text[:200]))
    log('语音合成：HTTP 200 / %d 字节 / %dms' % (len(r.content), ms))
    return r.content


# ---------------------------------------------------------------- 音频
def decode(wav_bytes: bytes):
    import miniaudio

    return miniaudio.decode(
        wav_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=2,
        sample_rate=48000,
    )


def playbacks():
    import miniaudio

    return miniaudio.Devices().get_playbacks()


def pick_playback(hint: str):
    """按名字挑输出设备；16 声道版本和采集端一律排除。"""
    want = str(hint or '').upper()
    devs = playbacks()
    if not devs:
        raise VoiceNativeError('系统里没有任何播放设备')
    narrowed = [d for d in devs
                if want in (d.get('name') or '').upper()
                and '16CH' not in (d.get('name') or '').upper()
                and 'OUTPUT' not in (d.get('name') or '').upper()]
    if not narrowed:
        narrowed = [d for d in devs if want in (d.get('name') or '').upper()]
    if not narrowed:
        names = '、'.join((d.get('name') or '?') for d in devs)
        raise VoiceNativeError('找不到输出设备「%s」，现有：%s' % (hint, names))
    return narrowed[0]


def feeder(samples, channels: int):
    """miniaudio 1.7x 的播放回调是「生成器」：每帧被 send(framecount)，需 yield 对应样本。

    播完以后继续喂静音，避免提前 StopIteration 让设备自行停止。
    """
    pos = 0
    total = len(samples)
    framecount = yield
    while True:
        n = max(1, int(framecount))
        if pos < total:
            chunk = samples[pos:pos + n * channels]
            pos += len(chunk)
        else:
            chunk = array.array('h', bytes(n * channels * 2))
        framecount = yield chunk


def play_and_hold(audio, device, combo: str, cfg: dict, hwnd=None, log=print) -> float:
    """开播同时按住录音热键；返回音频时长（秒）。"""
    import miniaudio

    dur = audio.num_frames / float(audio.sample_rate)
    if dur > 58:
        raise VoiceNativeError('音频 %.1fs 超过微信语音上限（60s）' % dur)
    mods, main = parse_combo(combo)

    if hwnd:
        try:
            _user32.SetForegroundWindow(int(hwnd))
        except Exception:
            pass
        time.sleep(0.25)

    dev = miniaudio.PlaybackDevice(
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=2,
        sample_rate=48000,
        device_id=device.get('id'),
        backends=[miniaudio.Backend.WASAPI],
    )
    feed = feeder(audio.samples, 2)
    next(feed)
    lead = max(0, int(cfg['lead_in_ms'])) / 1000.0
    tail = max(0, int(cfg['tail_ms'])) / 1000.0
    held = False
    try:
        for m in mods:
            key_down(m)
        key_down(main)
        held = True
        time.sleep(lead)
        dev.start(feed)
        time.sleep(dur + 0.2)
        time.sleep(tail)
    finally:
        if held:
            key_up(main)
            for m in reversed(mods):
                key_up(m)
        try:
            dev.stop()
            dev.close()
        except Exception:
            pass
    return dur


def parse_combo(combo: str):
    parts = [p.strip().lower() for p in str(combo or '').split('+') if p.strip()]
    if not parts:
        raise VoiceNativeError('录音热键未配置')
    main_name = parts[-1]
    if main_name not in VK:
        raise VoiceNativeError('无法识别的热键：%s' % combo)
    mods = []
    for p in parts[:-1]:
        if p not in VK:
            raise VoiceNativeError('无法识别的修饰键：%s' % p)
        mods.append(VK[p])
    return mods, VK[main_name]


def key_down(vk: int):
    _user32.keybd_event(vk, 0, 0, 0)


def key_up(vk: int):
    _user32.keybd_event(vk, 0, 2, 0)


# ---------------------------------------------------------------- 对外接口
def deliver(text: str, hwnd, prepare, voice_cfg: dict, bot_dir, log=print):
    """发送一条语音。返回 (是否可能已发出, 详情 dict)。

    语义保持一致：只要开始录音就返回 True，
    调用方据此决定是否跳过文字回退，避免重复发两条。
    """
    cfg = cfg_of(voice_cfg)
    t0 = time.monotonic()
    stage = '合成'
    held = False
    try:
        key = load_key(bot_dir)
        wav = synth(text, key, cfg, log)

        stage = '解码'
        audio = decode(wav)
        dur = audio.num_frames / float(audio.sample_rate)

        stage = '选设备'
        dev = pick_playback(cfg['out_device'])
        log('语音播放设备：%s（音频 %.1fs）' % (dev.get('name'), dur))

        stage = '准备会话'
        chat = prepare()
        if not chat:
            raise VoiceNativeError('无法确认目标账号或会话')

        stage = '录音'
        held = True
        play_and_hold(audio, dev, cfg['hotkey'], cfg, hwnd=hwnd, log=log)
        time.sleep(0.8)

        ms = int((time.monotonic() - t0) * 1000)
        log('语音已交给微信发送（会话「%s」，%.1fs，全程 %dms）' % (chat, dur, ms))
        return True, {
            'Status': 'sent_unconfirmed',
            'Chat': chat,
            'Seconds': round(dur, 2),
            'Device': dev.get('name'),
            'Ms': ms,
        }
    except Exception as exc:
        detail = {
            'Status': 'failed',
            'Error': str(exc),
            'Stage': stage,
        }
        log('语音未发出：阶段=%s 原因=%s' % (stage, exc))
        return False, detail


def deps(voice_cfg: dict, bot_dir) -> list:
    """返回缺失项列表，启动自检用。"""
    missing = []
    cfg = cfg_of(voice_cfg)
    if not key_path(bot_dir).is_file():
        missing.append('breeze.key（TTS 密钥）')
    try:
        import miniaudio  # noqa: F401
    except Exception:
        return missing + ['miniaudio']
    try:
        pick_playback(cfg['out_device'])
    except Exception as exc:
        missing.append('虚拟声卡（%s）' % str(exc)[:80])
    try:
        parse_combo(cfg['hotkey'])
    except Exception as exc:
        missing.append('热键（%s）' % exc)
    return missing


# ---------------------------------------------------------------- 自检
def selftest(play: bool = True) -> int:
    """不碰微信，只验证「合成 -> 解码 -> 指定设备播放」是否可用。"""
    import json

    bot = Path(__file__).resolve().parent
    raw = {}
    try:
        raw = (json.loads((bot / 'config.json').read_text(encoding='utf-8-sig')).get('voice') or {})
    except Exception:
        pass
    cfg = cfg_of(raw)
    print('TEST engine: native')
    miss = deps(raw, bot)
    print('TEST deps:', miss if miss else 'OK')
    if miss:
        return 2
    key = load_key(bot)
    print('TEST key:', key[:7] + '***')
    wav = synth('独立语音引擎自检，听到这句话就说明合成和播放都正常。', key, cfg)
    audio = decode(wav)
    print('TEST audio: %.2fs / %d frames / %d Hz' % (
        audio.num_frames / float(audio.sample_rate), audio.num_frames, audio.sample_rate))
    dev = pick_playback(cfg['out_device'])
    print('TEST device:', dev.get('name'))
    print('TEST hotkey:', cfg['hotkey'], parse_combo(cfg['hotkey']))
    if play:
        import miniaudio
        d = miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.SIGNED16, nchannels=2,
            sample_rate=48000, device_id=dev.get('id'),
            backends=[miniaudio.Backend.WASAPI])
        feed = feeder(audio.samples, 2)
        next(feed)
        d.start(feed)
        time.sleep(audio.num_frames / float(audio.sample_rate) + 0.3)
        d.stop()
        d.close()
        print('TEST played to device: OK')
    print('SELFTEST_OK')
    return 0


if __name__ == '__main__':
    import sys

    raise SystemExit(selftest('--no-play' not in sys.argv))

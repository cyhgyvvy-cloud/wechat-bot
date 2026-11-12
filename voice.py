# -*- coding: utf-8 -*-
"""微信「真·语音气泡」发送：两条实现并存。

native（默认）：本机自己完成全链路
  1. 调 TTS 接口合成 wav
  2. 用 miniaudio 把音频播到虚拟声卡的 CABLE Input
  3. 在微信窗口按住录音热键，播完松开，微信自动发送

legacy（旧实现，保留以便切回）：把活交给外部启动器
  1. 写 job {"Text":..., "Hwnd":...}，由启动器合成音频
  2. 合成完成写 <job>.ready
  3. 调用方确认目标会话已打开，再写 <job>.go {"Chat": 当前会话名}
  4. 启动器把音频播到 VB-CABLE → 按住录音热键 → 松开发送
  5. 结果写 <job>.result

legacy 不重写协议，直接加载外部目录下 bridge/voice_reply.py 的 deliver()。
它只依赖标准库，root 传该目录即可，不依赖启动器主程序在跑。

依赖（缺一不可，缺失时会明确报错并回退文字）：
  * native：VB-CABLE + breeze.key + 微信录音热键
  * legacy：D:\\Bots\\LegacyVoice 下的启动器、voice-send.json（Enabled=true）、
    voice-send-breeze.key（DPAPI 加密，当前用户可解）
  * 已安装 VB-Audio Virtual Cable（CABLE Input / Output）
"""
import importlib.util
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

DEFAULT_ROOT = r'D:\Bots\LegacyVoice'

# 语音引擎：
#   native  本机独立实现（breeze TTS + miniaudio 播放 + 按键录音）
#   legacy  旧实现，交给外部启动器干活
ENGINE_DEFAULT = 'native'


def engine_of(cfg) -> str:
    try:
        name = str((cfg or {}).get('engine') or ENGINE_DEFAULT).strip().lower()
    except Exception:
        name = ENGINE_DEFAULT
    return name if name in ('native', 'legacy') else ENGINE_DEFAULT

# 明确索要语音 / 明确拒绝（策略与旧版实现保持一致）
REQUEST = re.compile(
    r"(?:发|来|说|讲|回复)(?:给我|个|一条|一句|句|段|一段|条|一下|点|一段话|个回复|次|一)?\s*语音"
    r"|用语音(?:回|说|讲|唱|念|读|播|哼)|语音(?:回我|回复|说一句)"
    r"|说句话|说一句|说两句|念给我听|念出来|读出来|说出来")
NEGATIVE = re.compile(r"不要|别(?:再)?|不想|不用|不能|怎么|如何|他说|她说|有人说|关闭|取消|[\"“”‘’]")
TEXT_ONLY = re.compile(r"(?:用|只要|只用).{0,3}(?:文字|文本)|(?:文字|文本)回复|别发语音|不要语音")

VOICE_HINT = (
    '\n\n【语音回复】本轮你的回复会被合成语音，以微信语音消息发给对方。\n'
    '- 你确实能发微信语音，绝对不要说「我发不了语音」「我不能说话」「只能用文字」。\n'
    '- 写成适合朗读的自然口语：2~4 句短句，不要括号动作描写、不要引号歌词、'
    '不要列点或长段落。\n'
    '- 语音是朗读文字，不是唱歌。被要求唱歌时可以俏皮地说明自己只会念、不会唱，'
    '或者轻轻念两句歌词，但不要声称自己真的唱了歌。'
)

_SEND_LOCK = threading.Lock()
_STATE = {'mtime': None, 'cfg': {}, 'loaded': False}
_REPLY = None


def requested(text: str) -> bool:
    """用户是否明确要求用语音回复。"""
    t = str(text or '')
    return len(t) <= 500 and not NEGATIVE.search(t) and bool(REQUEST.search(t))


def wants_text_only(text: str) -> bool:
    return bool(TEXT_ONLY.search(str(text or '')))


def load_reply_module(root: str | Path):
    """惰性加载外部 voice_reply.py（纯标准库，可独立加载）。"""
    global _REPLY
    if _REPLY is None:
        p = Path(root) / 'bridge' / 'voice_reply.py'
        if not p.is_file():
            raise RuntimeError(f'外部语音模块不存在：{p}')
        spec = importlib.util.spec_from_file_location('legacy_voice_reply', p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _REPLY = mod
    return _REPLY


def check_dependencies(root: str | Path, voice_cfg=None) -> list[str]:
    """返回缺失项列表（空 = 依赖齐全）。不打印任何密钥内容。"""
    cfg = voice_cfg if voice_cfg is not None else (
        _STATE['cfg'] if _STATE['loaded'] else {})
    if engine_of(cfg) == 'native':
        try:
            import voice_native
        except Exception as exc:
            return ['voice_native 模块（%s: %s）' % (type(exc).__name__, exc)]
        return voice_native.deps(cfg, Path(__file__).resolve().parent)
    r = Path(root)
    missing = []
    # 启动器文件名随外部程序而异，写在 voice.launcher 里，不硬编码
    exe = str(cfg.get('launcher') or 'Launcher.exe')
    if not (r / exe).is_file():
        missing.append(exe)
    vs = r / 'data' / 'voice-send.json'
    if not vs.is_file():
        missing.append('data/voice-send.json')
    else:
        try:
            if json.loads(vs.read_text(encoding='utf-8-sig')).get('Enabled') is not True:
                missing.append('voice-send.json 的 Enabled 不是 true')
        except Exception as exc:
            missing.append(f'voice-send.json 不可读（{type(exc).__name__}）')
    if not (r / 'data' / 'voice-send-breeze.key').is_file():
        missing.append('data/voice-send-breeze.key（TTS 密钥）')
    return missing


def read_config(cfg_path: str | Path) -> dict:
    """读取本项目 config.json 的 voice 段，按 mtime 缓存（改配置无需重启）。"""
    p = Path(cfg_path)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return _STATE['cfg'] if _STATE['loaded'] else {}
    if mt != _STATE['mtime']:
        _STATE['mtime'] = mt
        try:
            vc = json.loads(p.read_text(encoding='utf-8-sig')).get('voice') or {}
            _STATE['cfg'] = vc
            _STATE['loaded'] = True
            print(f'语音回复：启用={bool(vc.get("enabled", False))} '
                  f'自动概率={float(vc.get("chance", 0)):.0%}（改 config 立即生效）',
                  flush=True)
        except Exception as exc:
            print(f'[警告] 语音配置读取失败，沿用上次值：{exc}',
                  file=sys.stderr, flush=True)
    return _STATE['cfg']


def should_reply_by_voice(text: str, is_group: bool, cfg: dict,
                          rng=__import__('random').random) -> tuple[bool, str]:
    """决定本轮是否走语音。返回 (是否发语音, 原因)。"""
    if not cfg.get('enabled', False):
        return False, 'disabled'
    if not (cfg.get('groups', True) if is_group else cfg.get('private', True)):
        return False, 'chat_type_disabled'
    if wants_text_only(text):
        return False, 'user_requested_text'
    if requested(text):
        return True, 'explicit_voice_request'
    try:
        chance = max(0.0, min(1.0, float(cfg.get('chance', 0))))
    except (TypeError, ValueError):
        chance = 0.0
    if chance > 0 and rng() < chance:
        return True, 'probability_hit'
    return False, 'probability_miss_or_disabled'


def _make_prepare(gui, who: str, log):
    """确认微信当前打开的就是目标会话，返回 UIA 读到的会话名。"""
    def prepare():
        try:
            uia = gui._get_uia()
            if uia is None:
                return ''
            target = str(who or '').strip()
            cur = str(uia.current_chat() or '').strip()
            if cur != target and target not in cur:
                if not uia.open_chat(target):
                    return ''
                cur = str(uia.current_chat() or '').strip()
            if not cur or (cur != target and target not in cur):
                return ''
            return cur
        except Exception as exc:
            log('语音 prepare 失败：' + str(exc))
            return ''
    return prepare


def send(text: str, who: str, gui, cfg_path: str | Path,
         log=print, timeout_note='') -> tuple[bool, dict]:
    """发送一条真·语音消息。返回 (是否可能已开始录音, 结果详情)。

    调用方应在返回 True 时**不要再发文字**，避免重复回复；
    返回 False 时应回退到普通文字发送。
    """
    cfg = read_config(cfg_path)
    if engine_of(cfg) == 'native':
        return _send_native(text, who, gui, cfg, log, timeout_note)
    return _send_legacy(text, who, gui, cfg, log, timeout_note)


def _send_native(text: str, who: str, gui, cfg: dict,
                 log=print, timeout_note='') -> tuple[bool, dict]:
    """独立引擎：本机合成 + 指定设备播放 + 按键录音，不依赖外部程序。"""
    try:
        import voice_native
    except Exception as exc:
        return False, {'Status': 'failed', 'Error': f'加载 voice_native 失败：{exc}'}
    here = Path(__file__).resolve().parent
    missing = voice_native.deps(cfg, here)
    if missing:
        return False, {'Status': 'failed', 'Error': '语音依赖缺失：' + '、'.join(missing)}
    try:
        hwnd = int(getattr(gui, 'main_hwnd', 0) or 0)
    except Exception:
        hwnd = 0
    if not hwnd:
        return False, {'Status': 'failed', 'Error': '微信主窗口句柄不可用'}
    prepare = _make_prepare(gui, who, log)
    with _SEND_LOCK:
        ok, detail = voice_native.deliver(text, hwnd, prepare, cfg, here, log=log)
    if timeout_note:
        detail['Note'] = timeout_note
    return bool(ok), detail


def _send_legacy(text: str, who: str, gui, cfg: dict,
                 log=print, timeout_note='') -> tuple[bool, dict]:
    """旧引擎：把活交给外部启动器（保留以便随时切回）。"""
    root = cfg.get('legacy_dir') or DEFAULT_ROOT
    missing = check_dependencies(root, cfg)
    if missing:
        return False, {'Status': 'failed', 'Error': '语音依赖缺失：' + '、'.join(missing)}
    try:
        reply = load_reply_module(root)
    except Exception as exc:
        return False, {'Status': 'failed', 'Error': f'加载外部语音模块失败：{exc}'}

    try:
        hwnd = int(getattr(gui, 'main_hwnd', 0) or 0)
    except Exception:
        hwnd = 0
    if not hwnd:
        return False, {'Status': 'failed', 'Error': '微信主窗口句柄不可用'}

    prepare = _make_prepare(gui, who, log)

    outcome = {}
    # 外部启动器的 manifest 声明 requireAdministrator，而机器人以普通
    # 权限运行，直接 CreateProcess 会报 WinError 740。兼容性层 RunAsInvoker 让子进程
    # 以当前用户权限启动（不弹 UAC）；实测它能启动且所需操作都不需要真正提权。
    prev_compat = os.environ.get('__COMPAT_LAYER')
    os.environ['__COMPAT_LAYER'] = 'RunAsInvoker'
    try:
        with _SEND_LOCK:
            started = reply.deliver(str(root), text, hwnd, prepare, log,
                                   outcome=outcome, send_lock=None)
        if timeout_note:
            outcome['Note'] = timeout_note
        return bool(started), outcome
    except Exception as exc:
        outcome.setdefault('Status', 'failed')
        outcome.setdefault('Error', f'{type(exc).__name__}: {exc}')
        return False, outcome
    finally:
        if prev_compat is None:
            os.environ.pop('__COMPAT_LAYER', None)
        else:
            os.environ['__COMPAT_LAYER'] = prev_compat

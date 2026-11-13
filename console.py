# -*- coding: utf-8 -*-
"""微信机器人 · 控制台

一个窗口管三件事：
  1. 启停机器人（显示状态、PID、运行时长）
  2. 实时看日志（bot.log 增量刷新，另有错误日志切换）
  3. 改常用配置（群名、私聊名单、表情/语音几率、主动消息）

机器人本体：优先用同目录的 bin/微信机器人/微信机器人.exe，
找不到则回落到源码目录里的 bot.py + 虚拟环境 python。
"""
import json
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import paths
import memstore

APP_TITLE = '微信机器人 · 控制台'
BOT_NAME = '微信机器人'
POLL_MS = 1200
MAX_LOG_LINES = 4000
CREATE_NO_WINDOW = 0x08000000
READY_MARK = '正在监听'      # bot.py 打出这行才算「能回消息了」
WECHAT_NAMES = ('weixin.exe', 'wechat.exe')
RETRY_MAX = 6                # 启动即退时的自动重试次数
RETRY_GAP = 8.0              # 每次重试间隔（秒）
BOT_DIR = paths.ROOT         # 机器人在哪（Console 初始化时按实际目录覆盖）


def cache_summary():
    """读机器人写的 cache_stats.json；超过一小时没更新就别显示旧数字。"""
    try:
        p = Path(BOT_DIR) / 'cache_stats.json'
        if not p.is_file():
            return ''
        d = json.loads(p.read_text(encoding='utf-8-sig'))
        if time.time() - float(d.get('updated') or 0) > 3600:
            return ''
        return ' · 缓存命中 %.0f%%' % float(d.get('recent_rate') or 0)
    except Exception:
        return ''


# ---------------------------------------------------------------- 定位
def locate_bot():
    """返回 (命令列表, 工作目录, 说明)；找不到返回 (None, None, 说明)。"""
    root = paths.ROOT
    if getattr(sys, 'frozen', False):
        for d in (root / 'bin' / BOT_NAME, root / BOT_NAME, root):
            exe = d / (BOT_NAME + '.exe')
            if exe.is_file():
                return [str(exe)], str(d), str(exe)
        return None, None, '没找到 %s.exe，请确认它和本程序在同一目录（或 bin 子目录）' % BOT_NAME

    bot_py = root / 'bot.py'
    if not bot_py.is_file():
        return None, None, '没找到 bot.py'
    py = root / '.venv' / 'Scripts' / 'python.exe'
    if not py.is_file():
        py = Path(sys.executable)
    return [str(py), str(bot_py)], str(root), '%s bot.py' % py.name


def wechat_started():
    """微信是否已经起来了（进程存在即算）。起来了才值得拉起机器人。"""
    try:
        import psutil
    except Exception:
        return True          # 探测不了就别卡着不启动
    for p in psutil.process_iter(['name']):
        try:
            if (p.info.get('name') or '').lower() in WECHAT_NAMES:
                return True
        except Exception:
            continue
    return False


def _pid_hit(pid):
    """这个 pid 是不是本机器人（顺手带回启动时间）。"""
    try:
        import psutil
        p = psutil.Process(pid)
        name = (p.name() or '').lower()
        ok = name == (BOT_NAME + '.exe').lower() or 'python' in name
        return (pid, p.create_time()) if ok else None
    except Exception:
        return None


def find_running():
    """找出正在跑的机器人进程，返回 [(pid, 启动时间)]。

    先读 bot.pid（机器人启动时自己写，1ms）；读不到才扫进程 —— 带 cmdline
    扫一遍全系统进程要 2 秒，每秒轮询一次会把界面卡死。
    """
    try:
        pid = int((BOT_DIR / 'bot.pid').read_text(encoding='utf-8').strip())
    except Exception:
        pid = 0
    if pid:
        hit = _pid_hit(pid)
        if hit:
            return [hit]
    try:
        import psutil
    except Exception:
        return []
    hits = []
    exe_name = (BOT_NAME + '.exe').lower()
    for p in psutil.process_iter(['pid', 'name', 'create_time']):
        try:
            name = (p.info.get('name') or '').lower()
            if name == exe_name:
                hits.append((p.info['pid'], p.info.get('create_time') or 0))
            elif 'python' in name:
                # 只有 python 进程才值得去读命令行（数量很少）
                cmd = ' '.join(p.cmdline() or []).lower()
                if 'bot.py' in cmd:
                    hits.append((p.info['pid'], p.info.get('create_time') or 0))
        except Exception:
            continue
    return hits


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return '%d 小时 %d 分' % (h, m)
    if m:
        return '%d 分 %d 秒' % (m, s)
    return '%d 秒' % s


# ---------------------------------------------------------------- 开机自启
AUTORUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
AUTORUN_NAME = 'WeChatBot'


def get_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTORUN_KEY) as k:
            value, _ = winreg.QueryValueEx(k, AUTORUN_NAME)
        return bool(value)
    except Exception:
        return False


def set_autostart(on):
    """写当前用户的启动项，指向本程序并带 --autostart。"""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTORUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, AUTORUN_NAME, 0, winreg.REG_SZ,
                              '"%s" --autostart' % sys.executable)
        else:
            try:
                winreg.DeleteValue(k, AUTORUN_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- 界面
class Console:
    def __init__(self, root):
        self.root = root
        self._cmd, cwd, self._desc = locate_bot()
        self._cwd = cwd
        base = Path(cwd) if cwd else paths.ROOT
        global BOT_DIR
        BOT_DIR = base
        self.cfg_path = base / 'config.json'
        self.bot_log = base / 'bot.log'
        self.err_log = base / 'error.log'
        self._log_pos = 0
        self._log_file = self.bot_log
        self._started = None      # 已知的启动时间戳
        self._last_pids = []
        self.cfg = {}
        self.vars = {}
        self.silent = False       # 开机自启时静默，不弹错误框
        self._launch_at = 0.0     # 本次发起启动的时刻
        self._ready_at = None     # 机器人打出「正在监听」的时刻
        self._log_mark = 0        # 发起启动时 bot.log 的长度，用来只看新增内容
        self._retries = 0         # 启动即退时自动重试次数
        self._watch_id = None

        root.title(APP_TITLE)
        root.geometry('940x660')
        root.minsize(820, 560)
        try:
            root.iconbitmap(default='')
        except Exception:
            pass

        self._init_style()
        self._build_header()
        self._build_status()      # 先占住底部，避免被日志区的 expand 挤掉
        self._build_tabs()
        self.ensure_config()
        self.load_config()
        # 先让窗口画出来，再去做进程探测（首次没有 bot.pid 时要扫进程，稍慢）
        self.root.after(220, self.refresh_status)
        self._seek_tail()
        self.pump_log()

    # ------------------------------------------------------------ 样式
    def _init_style(self):
        style = ttk.Style()
        for theme in ('vista', 'winnative', 'clam'):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        base = tkfont.nametofont('TkDefaultFont')
        base.configure(family='Microsoft YaHei UI', size=9)
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 13, 'bold'))
        style.configure('State.TLabel', font=('Microsoft YaHei UI', 11, 'bold'))
        style.configure('Hint.TLabel', foreground='#666666')
        style.configure('Danger.TLabel', foreground='#c0392b')
        style.configure('Ok.TLabel', foreground='#1e8449')
        style.configure('Warn.TLabel', foreground='#b9770e')
        style.configure('Big.TButton', padding=(14, 6))

    # ------------------------------------------------------------ 头部
    def _build_header(self):
        bar = ttk.Frame(self.root, padding=(16, 12, 16, 6))
        bar.pack(fill='x')

        left = ttk.Frame(bar)
        left.pack(side='left')
        ttk.Label(left, text='微信机器人', style='Title.TLabel').pack(anchor='w')
        self.lbl_state = ttk.Label(left, text='●  检测中', style='State.TLabel')
        self.lbl_state.pack(anchor='w', pady=(3, 0))
        self.lbl_meta = ttk.Label(left, text='', style='Hint.TLabel')
        self.lbl_meta.pack(anchor='w')

        right = ttk.Frame(bar)
        right.pack(side='right')
        self.btn_start = ttk.Button(right, text='启动', width=9, style='Big.TButton',
                                    command=self.do_start)
        self.btn_start.pack(side='left', padx=(0, 6))
        self.btn_stop = ttk.Button(right, text='停止', width=9, style='Big.TButton',
                                   command=self.do_stop)
        self.btn_stop.pack(side='left', padx=(0, 6))
        self.btn_restart = ttk.Button(right, text='重启', width=9, style='Big.TButton',
                                      command=self.do_restart)
        self.btn_restart.pack(side='left')

    # ------------------------------------------------------------ 主体
    def _build_tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill='both', expand=True, padx=14, pady=(4, 8))
        self.nb = nb

        # ---- 日志页
        page_log = ttk.Frame(nb, padding=8)
        nb.add(page_log, text='  运行日志  ')

        top = ttk.Frame(page_log)
        top.pack(fill='x', pady=(0, 6))
        self.log_choice = tk.StringVar(value='bot.log')
        ttk.Radiobutton(top, text='运行日志', value='bot.log', variable=self.log_choice,
                        command=self.switch_log).pack(side='left')
        ttk.Radiobutton(top, text='错误日志', value='error.log', variable=self.log_choice,
                        command=self.switch_log).pack(side='left', padx=(10, 0))
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text='自动滚到底', variable=self.autoscroll).pack(side='left', padx=(16, 0))
        ttk.Button(top, text='清空显示', command=self.clear_view).pack(side='right')
        ttk.Button(top, text='打开文件', command=self.open_current_log).pack(side='right', padx=(0, 6))

        wrap = ttk.Frame(page_log)
        wrap.pack(fill='both', expand=True)
        mono = ('Consolas', 9) if 'Consolas' in tkfont.families() else ('Courier New', 9)
        self.txt = tk.Text(wrap, wrap='word', font=mono, bg='#1e1e1e', fg='#dcdcdc',
                           insertbackground='#dcdcdc', relief='flat', padx=8, pady=6,
                           state='disabled')
        sb = ttk.Scrollbar(wrap, orient='vertical', command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        # ---- 配置页
        page_cfg = ttk.Frame(nb, padding=8)
        nb.add(page_cfg, text='  配置  ')
        self.cfg_page = page_cfg
        self._build_config(page_cfg)

        # ---- 记忆页
        page_mem = ttk.Frame(nb, padding=8)
        nb.add(page_mem, text='  记忆  ')
        self.mem_page = page_mem
        self._build_memory(page_mem)
        nb.bind('<<NotebookTabChanged>>', self._on_tab_changed)

    def _on_tab_changed(self, _event=None):
        """切到记忆页时重新读一次库：机器人那边随时可能在写。"""
        try:
            if self.nb.index('current') == 2:
                self.refresh_memory()
        except Exception:
            pass

    def _build_memory(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill='x', pady=(0, 4))
        ttk.Label(bar, text='会话', style='Hint.TLabel').pack(side='left')
        self.mem_chat = tk.StringVar(value='全部')
        self.mem_combo = ttk.Combobox(bar, textvariable=self.mem_chat, width=14,
                                      state='readonly')
        self.mem_combo.pack(side='left', padx=(6, 10))
        self.mem_combo.bind('<<ComboboxSelected>>', lambda e: self.refresh_memory())
        ttk.Label(bar, text='搜索', style='Hint.TLabel').pack(side='left')
        self.mem_search = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.mem_search, width=14)
        ent.pack(side='left', padx=(6, 6))
        ent.bind('<Return>', lambda e: self.refresh_memory())
        ttk.Button(bar, text='查找', command=self.refresh_memory).pack(side='left')
        ttk.Button(bar, text='刷新', command=self.refresh_memory).pack(side='left', padx=(6, 0))

        # 单独一行放危险操作，避免和表格抢宽度被挤出窗口
        bar2 = ttk.Frame(parent)
        bar2.pack(fill='x', pady=(0, 6))
        ttk.Button(bar2, text='清空…', command=self.clear_memory).pack(side='right')
        ttk.Button(bar2, text='删除所选', command=self.del_memory).pack(side='right', padx=(0, 6))
        ttk.Label(bar2, text='选中一行或多行后可删除；清空是按当前会话来的',
                  style='Hint.TLabel').pack(side='left')

        wrap = ttk.Frame(parent)
        wrap.pack(fill='both', expand=True)
        cols = ('ts', 'chat', 'who', 'text', 'uses')
        self.mem_tree = ttk.Treeview(wrap, columns=cols, show='headings',
                                     selectmode='extended', height=12)
        heads = (('ts', '时间', 92, 'center'),
                 ('chat', '会话', 92, 'w'),
                 ('who', '谁', 72, 'w'),
                 ('text', '记住了什么', 330, 'w'),
                 ('uses', '用到', 44, 'center'))
        for key, title, width, anchor in heads:
            self.mem_tree.heading(key, text=title)
            self.mem_tree.column(key, width=width, anchor=anchor,
                                 stretch=(key == 'text'))
        sb = ttk.Scrollbar(wrap, orient='vertical', command=self.mem_tree.yview)
        self.mem_tree.configure(yscrollcommand=sb.set)
        self.mem_tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        self.lbl_mem = ttk.Label(parent, text='', style='Hint.TLabel')
        self.lbl_mem.pack(fill='x', pady=(6, 0))
        self.refresh_memory()

    # ------------------------------------------------------------ 记忆操作
    def _mem_ready(self):
        """把记忆库指向机器人所在目录，并返回是否可用。"""
        try:
            memstore.configure(BOT_DIR, (self.cfg or {}).get('memory') or {})
            return memstore.enabled(), memstore.stats()
        except Exception as exc:
            self.lbl_mem.configure(text='记忆库读取失败：%s' % exc)
            return False, {}

    def refresh_memory(self):
        if not hasattr(self, 'mem_tree'):
            return
        ok, st = self._mem_ready()
        self.mem_tree.delete(*self.mem_tree.get_children())
        if not ok:
            self.lbl_mem.configure(text='记忆未启用（配置 → 记忆 → 启用记忆 勾上并保存）')
            return
        chat = self.mem_chat.get()
        chat = '' if chat in ('全部', '') else chat
        rows = memstore.facts(chat, self.mem_search.get().strip())
        for f in rows:
            try:
                when = time.strftime('%m-%d %H:%M', time.localtime(f['ts']))
            except Exception:
                when = ''
            text = f['text']
            if f.get('kind') == 'diary':
                text = '[日记] ' + text
            self.mem_tree.insert('', 'end', iid=str(f['id']),
                                 values=(when, f['chat'], f['subject'] or '—',
                                         text, f['uses']))
        names = ['全部'] + memstore.chats()
        self.mem_combo.configure(values=names)
        if self.mem_chat.get() not in names:
            self.mem_chat.set('全部')
        tip = '共 %d 条记忆（日记 %d 条，显示 %d 条）· 历史对话 %d 条 · %s' % (
            st.get('facts', 0), st.get('diary', 0), len(rows),
            st.get('messages', 0), st.get('path', ''))
        self.lbl_mem.configure(text=tip)

    def del_memory(self):
        sel = self.mem_tree.selection()
        if not sel:
            return
        if not messagebox.askyesno(APP_TITLE, '删除选中的 %d 条记忆？' % len(sel)):
            return
        n = sum(1 for i in sel if memstore.delete_fact(int(i)))
        self.refresh_memory()
        self.system_log('--- 已删除 %d 条记忆' % n)

    def clear_memory(self):
        chat = self.mem_chat.get()
        chat = '' if chat in ('全部', '') else chat
        what = ('「%s」的全部记忆和对话历史' % chat) if chat else '所有会话的记忆和对话历史'
        if not messagebox.askyesno(APP_TITLE, '确定清空%s？\n\n清空后机器人就不记得这些事了。' % what):
            return
        n = memstore.clear(chat)
        self.refresh_memory()
        self.system_log('--- 已清空 %d 条记忆（%s）' % (n, chat or '全部'))

    def _build_config(self, parent):
        # 操作栏固定在底部，不随内容滚动
        bar = ttk.Frame(parent, padding=(6, 10, 6, 2))
        bar.pack(side='bottom', fill='x')
        ttk.Button(bar, text='保存配置', command=self.save_config,
                   style='Big.TButton').pack(side='left')
        ttk.Button(bar, text='重新载入', command=self.load_config).pack(side='left', padx=(8, 0))
        ttk.Button(bar, text='打开配置文件', command=self.open_config).pack(side='left', padx=(8, 0))
        self.lbl_save = ttk.Label(bar, text='', style='Ok.TLabel')
        self.lbl_save.pack(side='left', padx=(12, 0))

        canvas = tk.Canvas(parent, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        inner = ttk.Frame(canvas, padding=(6, 4))
        canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind_all('<MouseWheel>',
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), 'units'))

        def section(title):
            ttk.Label(inner, text=title, style='Title.TLabel').pack(anchor='w', pady=(14, 6))
            box = ttk.Frame(inner)
            box.pack(fill='x', padx=(4, 0))
            box.columnconfigure(1, weight=1)
            return box

        def entry_row(box, row, label, key, hint=''):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky='w', pady=4)
            var = tk.StringVar()
            self.vars[key] = var
            ttk.Entry(box, textvariable=var).grid(row=row, column=1, sticky='ew',
                                                 padx=(10, 10), pady=4)
            if hint:
                ttk.Label(box, text=hint, style='Hint.TLabel').grid(row=row, column=2,
                                                                   sticky='w')
            return var

        def check_row(box, row, label, key):
            var = tk.BooleanVar()
            self.vars[key] = var
            ttk.Checkbutton(box, text=label, variable=var).grid(row=row, column=0,
                                                               columnspan=2, sticky='w', pady=4)
            return var

        def scale_row(box, row, label, key):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky='w', pady=4)
            holder = ttk.Frame(box)
            holder.grid(row=row, column=1, sticky='ew', padx=(10, 10), pady=4)
            var = tk.IntVar()
            self.vars[key] = var
            lbl = ttk.Label(holder, width=5, anchor='e')

            def on_move(v):
                pct = int(float(v))
                var.set(pct)
                lbl.configure(text='%d%%' % pct)

            sc = ttk.Scale(holder, from_=0, to=100, orient='horizontal', command=on_move)
            sc.pack(side='left', fill='x', expand=True)
            lbl.pack(side='right', padx=(8, 0))
            self.vars[key + '__scale'] = sc
            self.vars[key + '__label'] = lbl
            return var

        # 基本
        box = section('基本')
        entry_row(box, 0, '群聊名称', 'group_name', '要监听的微信群')
        entry_row(box, 1, '机器人自称', 'bot_name')
        entry_row(box, 2, '接口地址', 'base_url')
        entry_row(box, 3, '模型名', 'model')
        auto_var = tk.BooleanVar(value=get_autostart())
        self.vars['autostart'] = auto_var
        ttk.Checkbutton(box, text='开机自动启动（登录后等微信起来，立即拉起机器人）',
                        variable=auto_var).grid(row=4, column=0, columnspan=3,
                                                sticky='w', pady=(8, 2))

        # 私聊
        box = section('私聊名单')
        ttk.Label(box, text='每行一个好友（备注名优先）。留空 = 不接私聊。',
                  style='Hint.TLabel').grid(row=0, column=0, columnspan=2, sticky='w')
        self.txt_private = tk.Text(box, height=5, wrap='none', font=('Microsoft YaHei UI', 9),
                                   relief='solid', borderwidth=1)
        self.txt_private.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(4, 2))
        ttk.Label(box, text='改完需重启才生效；表情/语音几率类是即时生效。',
                  style='Hint.TLabel').grid(row=2, column=0, columnspan=2, sticky='w')

        # 表情包
        box = section('表情包')
        check_row(box, 0, '启用表情包', 'sticker_enabled')
        scale_row(box, 1, '发送几率', 'sticker_chance')

        # 语音
        box = section('语音')
        check_row(box, 0, '启用语音气泡', 'voice_enabled')
        scale_row(box, 1, '语音几率', 'voice_chance')
        ttk.Label(box, text='语音需要 VB-CABLE + breeze 密钥 + 微信录音热键，缺一不可。',
                  style='Hint.TLabel').grid(row=2, column=0, columnspan=2, sticky='w')

        # 主动消息
        box = section('主动发消息')
        check_row(box, 0, '启用主动发消息', 'proactive_enabled')
        entry_row(box, 1, '活跃时段', 'active_hours', '如 08:30-23:00')

        # 记忆
        box = section('记忆')
        check_row(box, 0, '启用记忆（重启生效）', 'memory_enabled')
        check_row(box, 1, '按日记方式整理（每天一次，更省钱）', 'memory_diary')
        entry_row(box, 2, '每个会话保留历史', 'memory_keep', '条，重启生效')
        entry_row(box, 3, '长期记忆上限', 'memory_max_facts', '条/会话')
        entry_row(box, 4, '每次回忆', 'memory_recall', '条')
        entry_row(box, 5, '整理门槛', 'memory_every', '条消息才整理一次')
        ttk.Label(box, text='取消日记＝旧的事实卡片模式；日记片段压到 150 字内并按相关度召回。',
                  style='Hint.TLabel').grid(row=6, column=0, columnspan=2, sticky='w')
        ttk.Label(box, text='记忆存在 cache/memory.db，可在「记忆」页查看和删除。',
                  style='Hint.TLabel').grid(row=7, column=0, columnspan=2, sticky='w')

    def _build_status(self):
        bar = ttk.Frame(self.root, padding=(16, 0, 16, 10))
        bar.pack(side='bottom', fill='x')
        cmd, cwd, desc = self._cmd, self._cwd, self._desc
        text = ('程序：%s' % desc) if cmd else ('⚠ %s' % desc)
        self.lbl_path = ttk.Label(bar, text=text, style='Hint.TLabel')
        self.lbl_path.pack(side='left')
        ttk.Label(bar, text=str(self.cfg_path), style='Hint.TLabel').pack(side='right')

    # ------------------------------------------------------------ 进程
    def is_running(self):
        return bool(self._last_pids)

    def _detect_ready(self):
        """bot.log 的新增内容里出现「正在监听」= 已能回消息。

        只在本次启动写进去的部分里找，避免把上一次运行的日志当成这次就绪。
        """
        if self._ready_at is not None:
            return True
        try:
            size = self.bot_log.stat().st_size
        except OSError:
            return False
        if size <= self._log_mark:
            return False
        try:
            with open(self.bot_log, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(max(self._log_mark, size - 200 * 1024))
                chunk = f.read()
        except OSError:
            return False
        if READY_MARK not in chunk:
            return False
        self._ready_at = time.time()
        return True

    def _boot_breakdown(self):
        """读 bot.log 里最后一处「启动耗时」那行，原样带给状态栏。"""
        last = ''
        try:
            with open(self.bot_log, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(max(0, self.bot_log.stat().st_size - 64 * 1024))
                for line in f:
                    if line.startswith('启动耗时'):
                        last = line.strip()
        except OSError:
            pass
        return last

    def refresh_status(self):
        self._render(find_running())
        self.root.after(POLL_MS, self.refresh_status)

    def refresh_status_once(self):
        self._render(find_running())

    def _render(self, pids):
        self._last_pids = pids
        if pids:
            pid = pids[0][0]
            started = min(t for _, t in pids if t) if any(t for _, t in pids) else 0
            if not self._started:
                self._started = started
            up = fmt_duration(time.time() - self._started) if self._started else '—'
            extra = '（共 %d 个进程）' % len(pids) if len(pids) > 1 else ''
            ready = self._detect_ready()
            if ready:
                if self._launch_at and not getattr(self, '_told_ready', False):
                    self._told_ready = True
                    spent = max(0.0, self._ready_at - self._launch_at)
                    self.system_log('--- 已就绪，从发起到可回消息 %.1f 秒' % spent)
                    line = self._boot_breakdown()
                    if line:
                        self.system_log('    ' + line)
                boot = ''
                if self._launch_at and self._ready_at:
                    boot = ' · 启动 %.1fs' % max(0.0, self._ready_at - self._launch_at)
                self.lbl_state.configure(text='●  已就绪', style='Ok.TLabel')
                self.lbl_meta.configure(text='PID %d%s%s · 已运行 %s%s'
                                        % (pid, extra, boot, up, cache_summary()))
            else:
                waiting = ''
                if self._launch_at:
                    waiting = ' · 已等 %.0fs' % (time.time() - self._launch_at)
                self.lbl_state.configure(text='●  启动中', style='Warn.TLabel')
                self.lbl_meta.configure(text='PID %d%s%s，正在注册监听…' % (pid, extra, waiting))
            self.btn_start.state(['disabled'])
            self.btn_stop.state(['!disabled'])
            self.btn_restart.state(['!disabled'])
        else:
            self._started = None
            self._ready_at = None
            self._told_ready = False
            self.lbl_state.configure(text='●  已停止', style='Danger.TLabel')
            self.lbl_meta.configure(text='点「启动」拉起机器人')
            self.btn_start.state(['!disabled'])
            self.btn_stop.state(['disabled'])
            self.btn_restart.state(['disabled'])
        self.root.after(POLL_MS, self.refresh_status)

    def do_start(self):
        if find_running():
            return
        cmd, cwd, desc = self._cmd, self._cwd, self._desc
        if not cmd:
            messagebox.showerror(APP_TITLE, desc)
            return
        try:
            self._log_mark = self.bot_log.stat().st_size if self.bot_log.exists() else 0
        except OSError:
            self._log_mark = 0
        try:
            # 静默模式：失败别弹窗（弹一次阻塞 10 秒），交给下面自动重试
            env = dict(os.environ, WECHAT_BOT_QUIET='1')
            if getattr(sys, 'frozen', False):
                # 机器人 exe 自己会把输出写进 bot.log，不必再挂句柄
                subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                                 env=env, creationflags=CREATE_NO_WINDOW)
            else:
                out = open(self.bot_log, 'a', encoding='utf-8')
                err = open(self.err_log, 'a', encoding='utf-8')
                subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=err,
                                 stdin=subprocess.DEVNULL,
                                 env=env, creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, '启动失败：%s' % exc)
            return
        self._launch_at = time.time()
        self._ready_at = None
        self._told_ready = False
        self.system_log('--- 已发起启动：%s' % desc)
        try:
            self._err_mark = self.err_log.stat().st_size if self.err_log.exists() else 0
        except OSError:
            self._err_mark = 0
        self.root.after(2000, self._watch_start)

    def _fresh_error(self):
        try:
            if self.err_log.exists() and self.err_log.stat().st_size > self._err_mark:
                return self.tail_text(self.err_log, 1200)
        except OSError:
            pass
        return ''

    def _watch_start(self, attempt=0):
        """盯着「启动中 → 已就绪」。中途退出就自动重试，别让人手点。"""
        if self._detect_ready():
            self._retries = 0
            self.refresh_status_once()
            return
        if find_running():
            if attempt < 60:            # 最长约 2 分半，DB 很大时也够
                self.root.after(2500, lambda: self._watch_start(attempt + 1))
            return
        # 进程已经不在了 —— 启动即退
        fresh = self._fresh_error()
        permanent = ('config.json' in fresh) or ('secret.txt' in fresh)
        # 微信还没登录完 / 库还没就绪：这种继续等就会自己好，值得多等一会
        waiting_wechat = ('无法确定唯一' in fresh) or ('未找到微信数据库' in fresh
                                                      or '没有找到指定群聊' in fresh)
        cap = 45 if waiting_wechat else RETRY_MAX      # 45 × 8s ≈ 6 分钟
        if not permanent and self._retries < cap:
            self._retries += 1
            self.system_log('--- 启动后退出，%d 秒后自动重试（第 %d/%d 次）：%s'
                            % (RETRY_GAP, self._retries, cap,
                               (fresh.strip().splitlines() or [''])[0][:80]))
            self.root.after(int(RETRY_GAP * 1000), self.do_start)
            return
        if fresh and not self.silent:
            messagebox.showwarning(
                APP_TITLE,
                '机器人启动后立刻退出了。\n\n最近错误日志：\n' + fresh)
        elif not self.silent:
            messagebox.showwarning(
                APP_TITLE, '机器人启动后立刻退出了，error.log 里没有内容。')

    def autostart_launch(self):
        """开机自启：等微信起来就拉起机器人，不再固定干等。"""
        if find_running():
            return
        self.do_start()

    def autostart_watch(self, tick=0):
        """开机自启的等待逻辑：轮询微信进程，起来了就立刻拉起机器人。

        以前是固定等 90 秒 —— 微信 12 秒就起来了也得白等 78 秒。改成轮询后，
        开机到能回消息的时间基本只等于「微信启动 + 机器人自己那几秒」。
        """
        if find_running():
            return
        if wechat_started():
            if tick == 0:
                return self.root.after(1500, lambda: self.autostart_watch(tick + 1))
            self.system_log('--- 检测到微信已启动，稍候拉起机器人')
            self.root.after(1500, self.autostart_launch)
            return
        if tick == 0:
            self.system_log('--- 开机自启：等微信启动，就绪后立即拉起（最多等 15 分钟）')
        if tick >= 450:
            self.system_log('--- 等微信超过 15 分钟，先拉起机器人试试')
            self.root.after(500, self.autostart_launch)
            return
        self.root.after(2000, lambda: self.autostart_watch(tick + 1))

    def do_stop(self):
        pids = find_running()
        if not pids:
            return
        if not messagebox.askyesno(APP_TITLE, '确定停止机器人？'):
            return
        for pid, _ in pids:
            try:
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
            except Exception:
                pass
        self.system_log('--- 已停止 %d 个进程' % len(pids))
        self.root.after(1200, lambda: self.refresh_status_once())

    def do_restart(self):
        pids = find_running()
        for pid, _ in pids:
            try:
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
            except Exception:
                pass
        self.system_log('--- 正在重启')
        self.root.after(1500, self.do_start)

    # ------------------------------------------------------------ 日志
    def switch_log(self):
        self._log_file = self.err_log if self.log_choice.get() == 'error.log' else self.bot_log
        self._log_pos = 0
        self.clear_view()
        self._seek_tail()
        self.pump_log_once()

    def _seek_tail(self, limit=200 * 1024):
        """首次打开只读日志尾部，避免几 MB 历史一次灌进窗口。"""
        try:
            size = self._log_file.stat().st_size
        except OSError:
            return
        self._log_pos = max(0, size - limit)

    def clear_view(self):
        self.txt.configure(state='normal')
        self.txt.delete('1.0', 'end')
        self.txt.configure(state='disabled')

    def open_current_log(self):
        target = self._log_file
        if target.exists():
            os.startfile(str(target))
        else:
            messagebox.showinfo(APP_TITLE, '日志文件还不存在：%s' % target.name)

    def open_config(self):
        if self.cfg_path.exists():
            os.startfile(str(self.cfg_path))
        else:
            messagebox.showinfo(APP_TITLE, '配置文件不存在：%s' % self.cfg_path)

    def append(self, text):
        if not text:
            return
        self.txt.configure(state='normal')
        self.txt.insert('end', text)
        lines = int(self.txt.index('end-1c').split('.')[0])
        if lines > MAX_LOG_LINES:
            self.txt.delete('1.0', '%d.0' % (lines - MAX_LOG_LINES))
        self.txt.configure(state='disabled')
        if self.autoscroll.get():
            self.txt.see('end')

    def system_log(self, text):
        self.append('[控制台 %s] %s\n' % (time.strftime('%H:%M:%S'), text))

    def tail_text(self, path, limit=2000):
        try:
            data = path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return ''
        return data[-limit:]

    def pump_log_once(self):
        path = self._log_file
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._log_pos:
            self._log_pos = 0
            self.clear_view()
        if size == self._log_pos:
            return
        try:
            with path.open('r', encoding='utf-8', errors='replace') as f:
                f.seek(self._log_pos)
                chunk = f.read()
                self._log_pos = f.tell()
        except OSError:
            return
        self.append(chunk)

    def pump_log(self):
        self.pump_log_once()
        self.root.after(POLL_MS, self.pump_log)

    # ------------------------------------------------------------ 配置
    def ensure_config(self):
        """首次运行没有 config.json 时，复制一份模板，免得机器人起不来。"""
        if self.cfg_path.exists():
            return
        for src in (self.cfg_path.parent / 'config.example.json',
                    paths.BUNDLE / 'config.example.json'):
            try:
                if src.is_file() and src.resolve() != self.cfg_path.resolve():
                    self.cfg_path.write_bytes(src.read_bytes())
                    return
            except Exception:
                continue

    def load_config(self):
        try:
            self.cfg = json.loads(self.cfg_path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            self.cfg = {}
            self.lbl_save.configure(text='未找到 config.json', style='Danger.TLabel')
        except Exception as exc:
            self.cfg = {}
            self.lbl_save.configure(text='配置读取失败：%s' % exc, style='Danger.TLabel')
            return

        g = lambda k, d='': self.cfg.get(k, d)
        self.vars['group_name'].set(g('group_name'))
        self.vars['bot_name'].set(g('bot_name'))
        self.vars['base_url'].set(g('base_url'))
        self.vars['model'].set(g('model'))

        self.txt_private.delete('1.0', 'end')
        self.txt_private.insert('1.0', '\n'.join(self.cfg.get('private_chats') or []))

        st = self.cfg.get('sticker') or {}
        self.vars['sticker_enabled'].set(bool(st.get('enabled')))
        self._set_scale('sticker_chance', float(st.get('chance', 0.0)))

        vo = self.cfg.get('voice') or {}
        self.vars['voice_enabled'].set(bool(vo.get('enabled')))
        self._set_scale('voice_chance', float(vo.get('chance', 0.0)))

        pr = self.cfg.get('proactive') or {}
        self.vars['proactive_enabled'].set(bool(pr.get('enabled')))
        self.vars['active_hours'].set(pr.get('active_hours', ''))

        me = self.cfg.get('memory') or {}
        diary = str(me.get('mode', 'diary')) == 'diary'
        self.vars['memory_enabled'].set(bool(me.get('enabled', True)))
        self.vars['memory_diary'].set(diary)
        self.vars['memory_keep'].set(str(me.get('keep_messages', 400 if diary else 60)))
        self.vars['memory_max_facts'].set(str(me.get('max_facts', 120)))
        self.vars['memory_recall'].set(str(me.get('recall_limit', 4 if diary else 6)))
        self.vars['memory_every'].set(str(me.get(
            'diary_min_msgs' if diary else 'extract_every', 12 if diary else 16)))

        self.lbl_save.configure(text='已载入', style='Ok.TLabel')

    def _set_scale(self, key, value):
        pct = max(0, min(100, int(round(value * 100))))
        self.vars[key].set(pct)
        sc = self.vars.get(key + '__scale')
        if sc is not None:
            sc.set(pct)
        lbl = self.vars.get(key + '__label')
        if lbl is not None:
            lbl.configure(text='%d%%' % pct)

    def save_config(self):
        cfg = self.cfg if isinstance(self.cfg, dict) else {}
        cfg['group_name'] = self.vars['group_name'].get().strip()
        cfg['bot_name'] = self.vars['bot_name'].get().strip()
        cfg['base_url'] = self.vars['base_url'].get().strip()
        cfg['model'] = self.vars['model'].get().strip()
        cfg['private_chats'] = [x.strip() for x in
                                self.txt_private.get('1.0', 'end').splitlines() if x.strip()]

        cfg.setdefault('sticker', {})['enabled'] = bool(self.vars['sticker_enabled'].get())
        cfg['sticker']['chance'] = round(self.vars['sticker_chance'].get() / 100.0, 3)
        cfg.setdefault('voice', {})['enabled'] = bool(self.vars['voice_enabled'].get())
        cfg['voice']['chance'] = round(self.vars['voice_chance'].get() / 100.0, 3)
        cfg.setdefault('proactive', {})['enabled'] = bool(self.vars['proactive_enabled'].get())
        cfg['proactive']['active_hours'] = self.vars['active_hours'].get().strip()

        me = cfg.setdefault('memory', {})
        me['enabled'] = bool(self.vars['memory_enabled'].get())
        me['mode'] = 'diary' if bool(self.vars['memory_diary'].get()) else 'fact'
        for key, name, lo, hi in (('memory_keep', 'keep_messages', 6, 2000),
                                  ('memory_max_facts', 'max_facts', 10, 2000),
                                  ('memory_recall', 'recall_limit', 1, 20)):
            raw = str(self.vars[key].get()).strip()
            if raw.isdigit():
                me[name] = max(lo, min(hi, int(raw)))
        # 两种模式共用一个门槛数字，切换时不至于留着旧值
        raw = str(self.vars['memory_every'].get()).strip()
        if raw.isdigit():
            me['extract_every'] = me['diary_min_msgs'] = max(4, min(500, int(raw)))
        me.setdefault('db', 'cache/memory.db')

        try:
            if self.cfg_path.exists():
                backup = self.cfg_path.with_suffix('.json.bak')
                backup.write_bytes(self.cfg_path.read_bytes())
            self.cfg_path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8', newline='\n')
        except Exception as exc:
            self.lbl_save.configure(text='保存失败：%s' % exc, style='Danger.TLabel')
            messagebox.showerror(APP_TITLE, '保存失败：%s' % exc)
            return

        running = self.is_running()
        tip = '已保存'
        if running:
            tip += '（名单/群名需重启生效，几率类已即时生效）'

        want_auto = bool(self.vars['autostart'].get())
        if getattr(sys, 'frozen', False):
            try:
                set_autostart(want_auto)
            except Exception as exc:
                tip = '配置已保存，但开机自启设置失败：%s' % exc
        elif want_auto:
            tip = '配置已保存（开机自启在打包版里才生效）'

        self.lbl_save.configure(text=tip, style='Ok.TLabel')
        self.system_log('配置已保存')


def main():
    root = tk.Tk()
    app = Console(root)
    if '--autostart' in sys.argv:
        app.silent = True
        try:
            root.iconify()          # 开机自启时缩到任务栏，不挡桌面
        except Exception:
            pass
        root.after(300, app.autostart_watch)
    root.mainloop()


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""调整「主动发表情包」概率。改 config.json 后机器人立即生效，无需重启。

用法:
  python set_chance.py                      查看当前设置
  python set_chance.py 40                  全局设为 40%
  python set_chance.py --chat 测试群 60      只给「测试群」设 60%
  python set_chance.py --chat 测试群 --clear 删除该会话覆盖，回到全局值

注意:
  * 0% = 只在用户明确说「发个表情包」时才发；100% = 每条消息都尝试发。
  * 识图轮、严肃话题、同一会话正在发图时不会发，不受这个概率影响。
"""
import paths
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = paths.ROOT
CFG = ROOT / 'config.json'


def load() -> dict:
    return json.loads(CFG.read_text(encoding='utf-8-sig'))


def save(data: dict) -> None:
    # 原子替换，避免机器人恰好读到写一半的配置
    tmp = CFG.with_name(CFG.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    os.replace(tmp, CFG)


def show(data: dict) -> None:
    sc = data.get('sticker') or {}
    base = float(sc.get('chance', 0.25))
    print('主动发图概率（全局）：{:.0%}'.format(base))
    by = sc.get('chance_by_chat') or {}
    if by:
        print('按会话覆盖：')
        for k, v in by.items():
            print('  {} = {:.0%}'.format(k, float(v)))
    else:
        print('按会话覆盖：无（所有会话都用全局值）')
    print('说明：0% 仅明确索要时发，100% 每条都尝试发；改完立即生效，无需重启。')


def main() -> int:
    ap = argparse.ArgumentParser(description='调整主动发表情包概率（无需重启机器人）')
    ap.add_argument('percent', nargs='?', help='0-100 的百分比，省略则只查看')
    ap.add_argument('--chat', default=None,
                    help='只给这个会话设置（群名或私聊备注，需精确匹配）')
    ap.add_argument('--clear', action='store_true', help='删除 --chat 指定的会话覆盖')
    args = ap.parse_args()

    data = load()
    sc = data.setdefault('sticker', {})
    by = sc.setdefault('chance_by_chat', {})

    if args.clear:
        if not args.chat:
            ap.error('--clear 需要配合 --chat 使用')
        if args.chat not in by:
            print('会话「{}」没有单独设置，无需删除。'.format(args.chat))
        else:
            by.pop(args.chat)
            save(data)
            print('已删除「{}」的覆盖，恢复全局值。'.format(args.chat))
        show(data)
        return 0

    if args.percent is None:
        show(data)
        return 0

    try:
        val = float(args.percent)
    except ValueError:
        ap.error('百分比必须是 0-100 的数字')
    if not 0 <= val <= 100:
        ap.error('百分比必须在 0-100 之间')

    prob = round(val / 100, 4)
    if args.chat:
        by[args.chat] = prob
        target = '会话「{}」'.format(args.chat)
    else:
        sc['chance'] = prob
        target = '全局'
    save(data)
    print('已将{}主动发图概率设为 {:.0f}%（立即生效，无需重启）。'.format(target, val))
    show(data)
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    sys.exit(main())

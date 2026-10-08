#!/usr/bin/env python3
"""固定模板封面生成器。给标题出一张闲鱼商品主图,不让模型自由作画。

用法: python3 cover.py --title "商品标题" --out cover.jpg [--badge "秒发"] [--desc "补充一行"]
模板参数(配色/角标/尺寸)都在 TEMPLATE 里,要改风格只改这里,保持所有商品一个样式。
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TEMPLATE = {
    "size": (1080, 1440),          # 3:4 竖图,闲鱼首图推荐
    "bg_top": (24, 60, 122),       # 深蓝渐变上
    "bg_bottom": (49, 110, 160),   # 渐变下
    "title_color": (255, 255, 255),
    "title_size": 78,
    "title_lines": 5,              # 标题最多五行
    "badge_text": "网盘秒发",
    "badge_bg": (255, 196, 0),
    "badge_color": (51, 51, 51),
    "badge_size": 44,
    "footer_text": "自动发货 · 拍下秒发 · 包更新",
    "footer_size": 40,
    "footer_color": (255, 255, 255),
    "card_bg": (255, 255, 255),
    "card_alpha": 28,              # 标题卡透明度
    "font": "/System/Library/Fonts/STHeiti Medium.ttc",
}


def _font(size: int):
    return ImageFont.truetype(TEMPLATE["font"], size)


def _wrap(title: str, limit: int) -> list[str]:
    """中文按字宽折行,纯英文/数字按字符折。"""
    lines = []
    for paragraph in title.replace("\n", " ").split("  "):
        current = ""
        width = 0
        for ch in paragraph:
            w = 1 if ord(ch) > 0x2E80 else 0.55
            if width + w > limit and current:
                lines.append(current)
                current, width = ch, w
            else:
                current += ch
                width += w
        if current:
            lines.append(current)
    return lines[: TEMPLATE["title_lines"]]


def render(title: str, out: Path, badge: str = "", desc: str = "") -> Path:
    size = TEMPLATE["size"]
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img, "RGBA")

    # 背景渐变
    top, bottom = TEMPLATE["bg_top"], TEMPLATE["bg_bottom"]
    for y in range(size[1]):
        t = y / size[1]
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (size[0], y)], fill=color)

    # 标题半透明卡片
    card_margin = 70
    card_box = (card_margin, 150, size[0] - card_margin, size[1] - 320)
    draw.rounded_rectangle(card_box, radius=36, fill=(255, 255, 255, TEMPLATE["card_alpha"]))

    # 标题(自动缩字号防溢出)
    limit = 11
    font_size = TEMPLATE["title_size"]
    lines = _wrap(title, limit)
    while len(lines) >= TEMPLATE["title_lines"] and font_size > 56:
        font_size -= 6
        limit += 1
        lines = _wrap(title, limit)
    font = _font(font_size)
    y = card_box[1] + 90
    for line in lines:
        draw.text((card_box[0] + 60, y), line, font=font, fill=TEMPLATE["title_color"])
        y += int(font_size * 1.42)

    if desc:
        dfont = _font(46)
        for dline in textwrap.wrap(desc, 16)[:2]:
            draw.text((card_box[0] + 60, y + 40), dline, font=dfont, fill=(255, 255, 255, 230))
            y += 70

    # 左上角标
    badge = badge or TEMPLATE["badge_text"]
    bfont = _font(TEMPLATE["badge_size"])
    bw = draw.textlength(badge, font=bfont)
    draw.rounded_rectangle(
        (card_box[0], 60, card_box[0] + bw + 64, 130),
        radius=18, fill=TEMPLATE["badge_bg"],
    )
    draw.text((card_box[0] + 32, 70), badge, font=bfont, fill=TEMPLATE["badge_color"])

    # 底部说明
    ffont = _font(TEMPLATE["footer_size"])
    fw = draw.textlength(TEMPLATE["footer_text"], font=ffont)
    draw.text(((size[0] - fw) / 2, size[1] - 220), TEMPLATE["footer_text"], font=ffont, fill=TEMPLATE["footer_color"])

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--badge", default="")
    parser.add_argument("--desc", default="")
    args = parser.parse_args()
    path = render(args.title, Path(args.out), badge=args.badge, desc=args.desc)
    print(path)


if __name__ == "__main__":
    main()

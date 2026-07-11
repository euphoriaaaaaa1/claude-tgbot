#!/usr/bin/env python3
"""把 anchor 原图标准化为 832×1216 (2:3 portrait)。

用法：
    python3 anchor_prep.py <输入图> <输出名>
    例：python3 anchor_prep.py raw/chenlulu_raw.png chenlulu_anchor

行为（极简版）：
1. 居中按 2:3 比例裁切（832×1216）
2. 保存到 ~/resource/anchors/<名>.png

⚠️ 不做去水印。AI 水印 / 任何文字请在 raw 阶段用 Preview 手动避选裁掉。
   旧版本的"双角纯色填充"会形成色块，污染 img2img 的 latent。
"""
import sys
import os
from PIL import Image

OUT_W, OUT_H = 832, 1216
ANCHOR_DIR = os.path.expanduser("~/resource/anchors")


def center_crop_to_ratio(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    src_w, src_h = img.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        # 太宽 → 左右裁
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    elif src_ratio < target_ratio:
        # 太高 → 上下裁
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))
    return img.resize((target_w, target_h), Image.LANCZOS)


def main():
    if len(sys.argv) < 3:
        print("usage: anchor_prep.py <input> <output_name>", file=sys.stderr)
        sys.exit(2)
    src = sys.argv[1]
    if not os.path.isabs(src):
        src = os.path.join(ANCHOR_DIR, "raw", src) if not src.startswith("raw/") \
              else os.path.join(ANCHOR_DIR, src)
    if not os.path.exists(src):
        print(f"not found: {src}", file=sys.stderr); sys.exit(3)
    name = sys.argv[2]
    if name.endswith(".png"): name = name[:-4]

    img = Image.open(src).convert("RGB")
    print(f"  原图: {img.size}")
    img = center_crop_to_ratio(img, OUT_W, OUT_H)

    out = os.path.join(ANCHOR_DIR, f"{name}.png")
    img.save(out, "PNG", quality=95)
    print(f"  → {out} ({img.size})")


if __name__ == "__main__":
    main()

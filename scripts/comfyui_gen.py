#!/usr/bin/env python3
"""ComfyUI HTTP 生图客户端。

用法：python3 comfyui_gen.py <bot_id> "<英文 prompt>" [--out <路径>]

流程：
1. 读取 workflow JSON 模板
2. 替换 prompt 占位符 + 拼前缀 + 随机 seed
3. POST /prompt 排队
4. 轮询 /history/<prompt_id>
5. 从 ComfyUI/output/ 找输出图，复制到 bot 目录
"""
import sys
import os
import json
import time
import shutil
import secrets
import argparse
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader


def _http_post(url: str, data: dict, timeout: int = 30) -> dict:
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_get(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def submit(comfyui_url: str, workflow: dict, client_id: str = None) -> str:
    if client_id is None:
        client_id = secrets.token_hex(8)
    out = _http_post(f"{comfyui_url.rstrip('/')}/prompt",
                     {"prompt": workflow, "client_id": client_id})
    pid = out.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI prompt failed: {out}")
    return pid


def wait_done(comfyui_url: str, prompt_id: str, timeout: int = 600) -> dict:
    """轮询 history。返回 history[prompt_id] 节点输出。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            h = _http_get(f"{comfyui_url.rstrip('/')}/history/{prompt_id}")
            if h and prompt_id in h:
                entry = h[prompt_id]
                if entry.get("status", {}).get("completed"):
                    return entry
                last = entry
        except (urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(2)
    raise TimeoutError(f"ComfyUI 生图超时（{timeout}s）。最后状态: {last}")


def find_output_image(history_entry: dict, output_dir: str) -> str | None:
    """从 history 里找 SaveImage 节点输出的文件名。"""
    outputs = history_entry.get("outputs", {})
    for node_id, node_out in outputs.items():
        for img in node_out.get("images", []):
            sub = img.get("subfolder", "")
            fname = img.get("filename", "")
            if not fname:
                continue
            full = os.path.join(output_dir, sub, fname) if sub else os.path.join(output_dir, fname)
            if os.path.exists(full):
                return full
    return None


# 尺寸预设（ComfyUI 端 px / 总像素 ~1M 内符合 z-image-turbo 训练分辨率）
# img2img 模式下：anchor 会被等比缩放到该尺寸框内，输出尺寸=缩后 anchor 尺寸
# Mac MPS 后端慢，建议 img2img 用 quick 档
SIZE_PRESETS = {
    "tiny": (512, 768),            # 极速 2:3 — ~0.4MP，预览/快速发圈 (~25s on Mac)
    "quick": (640, 960),           # 标准 2:3 — ~0.6MP，朋友圈日常 (~45s)
    "portrait": (768, 1152),       # 高质 2:3 — ~0.88MP，重点作品 (~70s)
    "landscape": (1152, 768),      # 横图 3:2 — 风景/双人
    "square": (1024, 1024),        # 方图 1:1 — 头像/食物/物品
    "small": (512, 512),           # 小方图 — 快速预览/缩略
    "tall": (640, 1216),           # 长竖 ~1:1.9 — 全身/超长腿
    "wide": (1216, 640),           # 长横 ~1.9:1 — 屏幕宽景
    "wide16": (1280, 720),         # 精确 16:9 — 标准宽屏
}
DEFAULT_SIZE = "quick"  # 默认走标准档（img2img 模式下 ~45s）


def patch_workflow(workflow: dict, prompt: str, negative: str,
                   prompt_ph: str, neg_ph: str,
                   size: str = DEFAULT_SIZE,
                   init_image_name: str = None,
                   denoise: float = None) -> dict:
    """替换 workflow 占位符 + 随机 seed + 改尺寸 + 可选注入 init image / denoise。"""
    wf = json.loads(json.dumps(workflow))
    w, h = SIZE_PRESETS.get(size, SIZE_PRESETS[DEFAULT_SIZE])
    for node_id, node in wf.items():
        inputs = node.get("inputs", {})
        if "text" in inputs and isinstance(inputs["text"], str):
            t = inputs["text"]
            for ph, val in [(prompt_ph, prompt), (neg_ph, negative)]:
                if ph in t:
                    t = t.replace(f'"{ph}"', val).replace(ph, val)
            inputs["text"] = t
        if "seed" in inputs and isinstance(inputs["seed"], int):
            inputs["seed"] = secrets.SystemRandom().randint(1, 2**63 - 1)
        if node.get("class_type") in ("EmptySD3LatentImage", "EmptyLatentImage"):
            inputs["width"] = w
            inputs["height"] = h
        # img2img: 注入 LoadImage 文件名
        if node.get("class_type") == "LoadImage" and init_image_name:
            inputs["image"] = init_image_name
        # img2img: 注入 KSampler denoise
        if node.get("class_type") == "KSampler" and denoise is not None:
            inputs["denoise"] = denoise
    return wf


COMFYUI_INPUT_DIR = os.path.expanduser("~/Desktop/app/comfyui/ComfyUI/input")


def _stage_init_image(abs_path: str, target_size: tuple = None) -> str | None:
    """把 init-image 复制到 ComfyUI/input/ 用 unique 名字，返回文件名供 LoadImage 用。

    target_size=(target_w, target_h)：cover-crop 到精确目标尺寸。
    1) 如果 anchor 比例 != 目标比例 → 中心裁切到目标比例，再 resize
    2) 如果 anchor 比例 == 目标比例 → 直接 resize
    img2img 输出尺寸=init image 尺寸，必须严格匹配 SIZE_PRESETS，
    否则 --size wide 之类的指令传了也没用（anchor 是竖图等比缩放永远达不到 16:9）。
    宽高都对齐到 16 的倍数（z-image 官方硬约束，避免 latent 边缘错位）。
    """
    if not abs_path or not os.path.exists(abs_path):
        return None
    os.makedirs(COMFYUI_INPUT_DIR, exist_ok=True)

    if target_size:
        from PIL import Image
        img = Image.open(abs_path).convert("RGB")
        w0, h0 = img.size
        target_w, target_h = target_size
        # 对齐到 16 的倍数
        target_w = max(16, (target_w // 16) * 16)
        target_h = max(16, (target_h // 16) * 16)

        src_ratio = w0 / h0
        dst_ratio = target_w / target_h

        if abs(src_ratio - dst_ratio) > 0.01:
            # 比例不同 → cover-crop（中心裁切到目标比例）
            if src_ratio > dst_ratio:
                # 源更宽：裁掉左右
                new_w0 = int(h0 * dst_ratio)
                left = (w0 - new_w0) // 2
                img = img.crop((left, 0, left + new_w0, h0))
            else:
                # 源更高：裁掉上下
                new_h0 = int(w0 / dst_ratio)
                top = (h0 - new_h0) // 2
                img = img.crop((0, top, w0, top + new_h0))

        # 现在比例匹配，resize 到精确目标尺寸
        img = img.resize((target_w, target_h), Image.LANCZOS)
        fname = f"_init_{int(time.time())}_{secrets.token_hex(3)}.png"
        dest = os.path.join(COMFYUI_INPUT_DIR, fname)
        img.save(dest, "PNG")
        print(f"[comfyui] init image {w0}x{h0} → {target_w}x{target_h} "
              f"(cover-crop, ratio src={src_ratio:.2f} dst={dst_ratio:.2f})",
              file=sys.stderr)
        return fname

    # 不需要缩 → 直接 copy
    ext = os.path.splitext(abs_path)[1] or ".png"
    fname = f"_init_{int(time.time())}_{secrets.token_hex(3)}{ext}"
    dest = os.path.join(COMFYUI_INPUT_DIR, fname)
    shutil.copy2(abs_path, dest)
    return fname


def generate(bot_id: str, prompt: str, out_path: str = None,
              negative_extra: str = "", size: str = DEFAULT_SIZE,
              init_image: str = None, denoise: float = None) -> str | None:
    # 没显式传 init_image / denoise → 自动从 bot yml 读 anchor 字段
    # 让 Telegram worker 透明走 img2img，不需要拼参数
    bot_cfg_cache = None
    if init_image is None or denoise is None:
        try:
            bot_cfg_cache = config_loader.load_bot(bot_id)
            if init_image is None:
                ai = (bot_cfg_cache.get("anchor_image") or "").strip()
                if ai and os.path.exists(ai):
                    init_image = ai
            if denoise is None and init_image:
                d = bot_cfg_cache.get("anchor_denoise")
                if d is not None:
                    denoise = float(d)
        except Exception as e:
            print(f"[comfyui] 读 bot yml anchor 失败（继续走 txt2img）: {e}", file=sys.stderr)

    # 自动拼 yml.face_traits（仅当 prompt 涉及人像时；美食/风景/物品不拼）
    # 判断依据：prompt 里出现以下任一关键词即视为"涉及人像" → 拼 face_traits
    try:
        if bot_cfg_cache is None:
            bot_cfg_cache = config_loader.load_bot(bot_id)
        face_traits = (bot_cfg_cache.get("face_traits") or "").strip()
        # 人像关键词：booru 数量标签 / 视角 / 部位 / 身体描述 / 姿势 / 中文人物词
        # 人像关键词：booru 数量标签优先（最强信号）+ 明确人体部位/视角
        # 避免用 sitting/standing/lying 这种泛姿势词（猫狗物品也用）
        HUMAN_KEYWORDS = (
            # booru 数量标签（最强）
            "1girl", "1boy", "2girls", "2boys", "multiple_girls", "multiple_boys",
            "1woman", "1man",
            # 明确人体词
            "selfie", "portrait", "looking at viewer",
            "face focus", "breast focus", "ass focus", "crotch focus", "leg focus",
            "full body", "cowboy shot", "upper body", "mirror selfie",
            # 明确人体动作 phrase（不要单独 sitting/standing）
            "1girl sitting", "1girl standing", "1girl lying", "spread legs",
            "lifting skirt", "blowjob", "kneeling blowjob",
            # 中文人物词
            "自拍", "全身照", "半身照", "正面照", "侧脸", "脸部", "镜子前", "镜中",
            "人像", "肖像",
        )
        prompt_lower = prompt.lower()
        has_human = any(k.lower() in prompt_lower for k in HUMAN_KEYWORDS)
        if face_traits and has_human and face_traits not in prompt:
            prompt = f"{face_traits}, {prompt}"
            print(f"[comfyui] 拼 face_traits（含人像）: {face_traits[:60]}...", file=sys.stderr)
        elif face_traits and not has_human:
            print(f"[comfyui] 跳过 face_traits（非人像场景）", file=sys.stderr)
    except Exception:
        pass

    # 数量限定词检测：人像 prompt 没数量词时仅打 warning 不强制注入
    # （强制注入会破坏多人图场景，让 LLM/用户自己显式写 1girl/2girls/multiple_girls）
    try:
        COUNT_KEYWORDS = ("1girl", "2girls", "3girls", "multiple_girls", "multiple girls",
                          "1boy", "2boys", "multiple_boys", "1woman", "2women",
                          "no humans", "no_humans", "group", "crowd")
        if has_human:
            has_count = any(k.lower() in prompt_lower for k in COUNT_KEYWORDS)
            if not has_count:
                print(f"[comfyui] ⚠️ 人像 prompt 未含数量词（1girl/2girls 等）"
                      f"——横画面易出双胞胎，建议 LLM 显式写明", file=sys.stderr)
    except Exception:
        pass

    g = config_loader.load_global()
    img_cfg = (g.get("moments", {}) or {}).get("image_generation", {})
    cfy = img_cfg.get("comfyui", {})
    comfyui_url = cfy.get("url", "http://127.0.0.1:8188")
    output_dir = cfy.get("output_dir")

    # 有 init_image 且 workflow_img2img 配了 → 走 img2img；否则走 txt2img
    use_img2img = bool(init_image and os.path.exists(init_image)
                       and cfy.get("workflow_img2img")
                       and os.path.exists(cfy["workflow_img2img"]))
    workflow_path = cfy.get("workflow_img2img") if use_img2img else cfy.get("workflow")

    if not workflow_path or not os.path.exists(workflow_path):
        print(f"workflow 不存在: {workflow_path}", file=sys.stderr)
        return None
    print(f"[comfyui] workflow={'img2img' if use_img2img else 'txt2img'}: {workflow_path}",
          file=sys.stderr)

    pos_prefix = (img_cfg.get("positive_prefix") or "").strip()
    neg_prefix = (img_cfg.get("negative_prefix") or "").strip()
    neg = (neg_prefix + ", " + negative_extra).strip(", ") if negative_extra else neg_prefix
    full_pos = (pos_prefix + ", " + prompt).strip(", ") if pos_prefix else prompt

    with open(workflow_path, encoding="utf-8") as f:
        workflow = json.load(f)

    # img2img: 把 anchor 按 size 预设等比缩放，控制输出尺寸=控制速度
    target_size = SIZE_PRESETS.get(size, SIZE_PRESETS[DEFAULT_SIZE]) if init_image else None
    init_image_name = _stage_init_image(init_image, target_size) if init_image else None
    wf = patch_workflow(
        workflow, full_pos, neg,
        cfy.get("prompt_placeholder", "%prompt%"),
        cfy.get("negative_placeholder", "%negative_prompt%"),
        size=size,
        init_image_name=init_image_name,
        denoise=denoise,
    )

    pid = submit(comfyui_url, wf)
    print(f"[comfyui] submitted prompt_id={pid}", file=sys.stderr)

    entry = wait_done(comfyui_url, pid)
    src = find_output_image(entry, output_dir)
    if not src:
        print("找不到输出图", file=sys.stderr)
        return None

    if out_path is None:
        save_dir = fos.path.expanduser("~/resource/media/{bot_id}/comfyui")
        os.makedirs(save_dir, exist_ok=True)
        out_path = os.path.join(save_dir, f"comfyui_{int(time.time())}_{secrets.token_hex(4)}.png")

    shutil.copy2(src, out_path)
    return out_path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("bot_id")
    p.add_argument("prompt")
    p.add_argument("--out", help="输出路径，留空自动")
    p.add_argument("--neg", default="", help="额外负向提示词")
    p.add_argument("--size", default=DEFAULT_SIZE,
                   choices=list(SIZE_PRESETS.keys()),
                   help=f"尺寸预设 (默认 {DEFAULT_SIZE})")
    p.add_argument("--init-image", help="img2img 起点图绝对路径（需切到 img2img workflow）")
    p.add_argument("--denoise", type=float,
                   help="img2img denoise (0.3-0.7 保参考图特征；不传按 workflow 默认 0.55)")
    args = p.parse_args()

    out = generate(args.bot_id, args.prompt, args.out, args.neg, args.size,
                    init_image=args.init_image, denoise=args.denoise)
    if out:
        print(f"MEDIA: {out}")
    else:
        print("FAIL: 生图失败")
        sys.exit(1)


if __name__ == "__main__":
    main()

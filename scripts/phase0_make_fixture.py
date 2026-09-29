#!/usr/bin/env python3
"""Phase 0 随机码图片夹具生成器（OIDO-EXEC WP09 / E04）。

铁律：
- 随机码**只存在于像素中**；任何 sidecar/清单/日志不得含码。
- 每个入口独立随机码（答案不能跨入口复用）。
- ground truth 写入 evals/phase0/secrets/<entry>.txt（0600，已 gitignore），
  由 phase0_verify.py 对照；码值不出现在任何命令行输出。

用法：.venv/bin/python scripts/phase0_make_fixture.py --entry web
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "phase0"
FIXTURES = OUT / "fixtures"
SECRETS = OUT / "secrets"

# 去混淆字符集（无 0/O/1/I/L）
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 6
ENTRIES = ("claude_web", "claude_ios", "claude_code", "estomago")

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # Pillow ≥10.1：默认字体可指定大小（等宽，可读性足够）
    return ImageFont.load_default(size=size)


def make_fixture(entry: str) -> dict:
    assert entry in ENTRIES, f"entry must be one of {ENTRIES}"
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))

    img = Image.new("RGB", (800, 300), "white")
    draw = ImageDraw.Draw(img)
    font = _load_font(96)
    # 居中画码；不写任何提示文字（提示词走消息正文，不进图）
    bbox = draw.textbbox((0, 0), code, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((800 - w) / 2 - bbox[0], (300 - h) / 2 - bbox[1]), code, fill="black", font=font)

    FIXTURES.mkdir(parents=True, exist_ok=True)
    png = FIXTURES / f"{entry}.png"
    img.save(png, format="PNG", optimize=True)
    sha = hashlib.sha256(png.read_bytes()).hexdigest()

    SECRETS.mkdir(parents=True, exist_ok=True)
    secret = SECRETS / f"{entry}.txt"
    secret.write_text(code + "\n")
    secret.chmod(stat.S_IRUSR | stat.S_IWUSR)          # 0600：只有 owner 可读

    manifest = {
        "fixture_id": f"ph0_{entry}",
        "entry": entry,
        "png": str(png.relative_to(ROOT)),
        "png_sha256": sha,
        "code_length": CODE_LEN,
        "alphabet": "deconfused_uppercase_digit",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "答案只存在于像素中；sidecar/日志/清单不含码。每入口独立随机。",
    }
    (OUT / f"{entry}.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entry", choices=ENTRIES)
    ap.add_argument("--all", action="store_true", help="生成全部四入口夹具")
    args = ap.parse_args()
    if not args.all and not args.entry:
        ap.error("需要 --entry <入口> 或 --all")
    entries = ENTRIES if args.all else (args.entry,)
    for entry in entries:
        m = make_fixture(entry)
        print(f"fixture-ready entry={m['entry']} png={m['png']} sha256={m['png_sha256'][:12]}…")
    print("secrets 已写入 evals/phase0/secrets/（0600，不入 git，不打印）")


if __name__ == "__main__":
    main()

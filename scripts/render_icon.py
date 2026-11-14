"""从 taggycat.png 生成全套应用图标资源（纯 Pillow，不依赖 Qt）。

用法：
    python scripts/render_icon.py

输入：
    taggycat.png                    品牌主图（512x512，建议透明或纯白背景）

产出：
    taggycat.ico                    根目录多尺寸 ICO（PyInstaller / 快捷方式用）
    resources/icons/app.ico         同一份 ICO 的运行时副本
    resources/icons/app.png         256x256 主 PNG
    resources/icons/app_{size}.png  16/24/32/48/64/128/256 各尺寸 PNG（UI 运行时加载）
    resources/icons/taggycat.png    512x512 品牌图副本

纯白背景会自动做边缘泛洪透明化，避免小尺寸图标出现白方块；
猫身上的白色细节（胡须、胸口等被主体包围的区域）不受影响。
"""

from collections import deque
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "taggycat.png"
OUT = ROOT / "resources" / "icons"
SIZES = (16, 24, 32, 48, 64, 128, 256)
WHITE_THRESHOLD = 245


def cutout(src: Image.Image) -> Image.Image:
    """从四边泛洪去除连通的纯白背景，返回带 alpha 的 RGBA 图。"""
    im = src.convert("RGBA")
    w, h = im.size
    px = im.load()
    seen = bytearray(w * h)
    queue: deque[tuple[int, int]] = deque()
    for x in range(w):
        queue.append((x, 0))
        queue.append((x, h - 1))
    for y in range(h):
        queue.append((0, y))
        queue.append((w - 1, y))

    while queue:
        x, y = queue.popleft()
        idx = y * w + x
        if seen[idx]:
            continue
        r, g, b, _ = px[x, y]
        if min(r, g, b) < WHITE_THRESHOLD:
            continue
        seen[idx] = 1
        px[x, y] = (r, g, b, 0)
        if x > 0:
            queue.append((x - 1, y))
        if x < w - 1:
            queue.append((x + 1, y))
        if y > 0:
            queue.append((x, y - 1))
        if y < h - 1:
            queue.append((x, y + 1))
    return im


def main() -> None:
    if not SRC.is_file():
        raise SystemExit(f"缺少品牌主图：{SRC}")
    OUT.mkdir(parents=True, exist_ok=True)
    base = cutout(Image.open(SRC))
    base.save(OUT / "taggycat.png")

    images: dict[int, Image.Image] = {}
    for size in SIZES:
        img = base.resize((size, size), Image.Resampling.LANCZOS)
        images[size] = img
        img.save(OUT / f"app_{size}.png")
        print("wrote", OUT / f"app_{size}.png")
    images[256].save(OUT / "app.png")
    print("wrote", OUT / "app.png")

    # Pillow 会按 sizes 自动生成其余尺寸的帧，全部打包进一个 ICO
    ico_sizes = [(s, s) for s in SIZES]
    base.resize((256, 256), Image.Resampling.LANCZOS).save(
        ROOT / "taggycat.ico", format="ICO", sizes=ico_sizes
    )
    base.resize((256, 256), Image.Resampling.LANCZOS).save(
        OUT / "app.ico", format="ICO", sizes=ico_sizes
    )
    print("wrote", ROOT / "taggycat.ico")
    print("wrote", OUT / "app.ico")


if __name__ == "__main__":
    main()

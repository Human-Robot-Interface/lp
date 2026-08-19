#!/usr/bin/env python3
"""配布する紙面を1枚のPNGに組む.

版画のような合成はしない。主役は用意された絵（CAPTCHA画面）で、
下に帯を作ってQRと最小限の文字を置くだけにする。

  絵     … 紙面そのもの。同時にARの画像トラッキング用ターゲットになる
           （細部が多く非対称＝追跡に向く。QR単体をマーカーにしてはいけない）
  QR     … 全体への入口。読めることが最優先なので余白のある帯に単独で置く
  文字   … 絵が情報過多なので、ここは抑える

絵の中の空欄のチェックボックスを自動検出し、
ARでチェックを描き込むための座標を最後に出力する。

使い方:
  python tools/halftone_qr.py --image face.png --content "https://..." -o qr.png
  python tools/make_sheet.py --qr qr.png --out sheet.png --expect "https://..."
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\YuGothB.ttc",
    r"C:\Windows\Fonts\meiryob.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

# 用紙の縦横比（幅を1としたときの高さ）
PAPERS = {"a4": 297 / 210, "a5": 210 / 148, "b5": 257 / 182, "square": 1.0}
PAPER_MM = {"a4": 210, "a5": 148, "b5": 182, "square": 210}


def load_font(size: int, path: str | None = None):
    for cand in ([path] if path else []) + FONT_CANDIDATES:
        if cand and Path(cand).exists():
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def find_checkbox(img: Image.Image):
    """絵の中の「空欄の青い正方形」を探す。見つからなければ None。

    塗りつぶし率が低い（＝枠線だけ）正方形に近い連結成分を、
    画面の下半分から選ぶ。
    """
    try:
        import cv2
    except ImportError:
        return None

    a = np.asarray(img.convert("RGB")).astype(int)
    h, w = a.shape[:2]
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    blue = ((b > r + 30) & (b > g + 18) & (b > 90)).astype(np.uint8)

    n, _, stats, _ = cv2.connectedComponentsWithStats(blue, connectivity=8)
    best = None
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if area < 900 or cw < 25 or ch < 25:
            continue
        if not 0.75 < cw / ch < 1.33:      # 正方形に近いか
            continue
        if area / (cw * ch) > 0.5:         # 中身が空か（枠線だけか）
            continue
        if y < h * 0.6:                    # 下部にあるか
            continue
        if best is None or area > best[4]:
            best = (x, y, cw, ch, area)
    return best[:4] if best else None


def trim_border(img: Image.Image, tol: int = 14) -> Image.Image:
    """外周の一様な枠（グレーの余白など）を切り落とす。

    絵の外枠を残したまま紙面に載せると、枠の色が帯の白とぶつかって
    「2枚貼り合わせた」ように見えるため。
    """
    a = np.asarray(img.convert("RGB")).astype(int)
    corner = a[0, 0]
    diff = np.abs(a - corner).sum(axis=2)
    mask = diff > tol
    if not mask.any():
        return img
    ys, xs = np.nonzero(mask)
    return img.crop((int(xs.min()), int(ys.min()),
                     int(xs.max()) + 1, int(ys.max()) + 1))


def ar_coords(px: float, py: float, w: int, h: int):
    """紙面のピクセル座標を MindAR の座標へ変換する。

    MindAR はターゲット画像の幅を1とし、原点は中心、上が +Y。
    """
    return (px / w - 0.5, (0.5 - py / h) * (h / w))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="絵＋QRを1枚の配布用PNGに組む",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--qr", required=True, type=Path,
                   help="halftone_qr.py などが出力したQRのPNG")
    p.add_argument("--base", type=Path, default=Path("concept/qrimage.png"),
                   help="紙面の主役になる絵")
    p.add_argument("-o", "--out", type=Path, default=Path("sheet.png"))

    p.add_argument("--paper", default="a4", choices=list(PAPERS))
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--margin", type=float, default=0.06,
                   help="紙面の余白（幅に対する割合）")
    p.add_argument("--keep-border", action="store_true",
                   help="絵の外周の一様な枠を切り落とさない")

    p.add_argument("--title", default="人とロボットの境界線")
    p.add_argument("--subtitle", default="THE LINE BETWEEN HUMAN AND MACHINE")
    p.add_argument("--lead", default="あなたには顔が、機械には住所が見えています。")
    p.add_argument("--note", default="この欄は、紙の上では埋められません。")

    p.add_argument("--bg", default="auto",
                   help="紙面の地色。auto は絵の四隅の色を拾い、絵が紙に溶けるようにする")
    p.add_argument("--fg", default="#111111")
    p.add_argument("--accent", default="#D0021B")
    p.add_argument("--font", default=None)

    p.add_argument("--expect", default=None,
                   help="QRに入れたはずの文字列。読取確認で内容まで照合する")
    p.add_argument("--mark", action="store_true",
                   help="検出したチェックボックスに枠を描く（確認用・本番では外す）")
    a = p.parse_args(argv)

    for f in (a.qr, a.base):
        if not f.exists():
            p.error(f"ファイルが見つかりません: {f}")

    # ---- 絵 ---------------------------------------------------------------
    base = Image.open(a.base).convert("RGB")
    if not a.keep_border:
        base = trim_border(base)

    # 地色。auto なら絵の四隅から拾う。絵の外枠と紙の地色が一致すると
    # 「画像を貼った」感じが消えて1枚の紙面として読める
    if a.bg == "auto":
        px = base.load()
        corners = [px[0, 0], px[base.width - 1, 0],
                   px[0, base.height - 1], px[base.width - 1, base.height - 1]]
        bg = tuple(int(sum(c[i] for c in corners) / 4) for i in range(3))
    else:
        bg = a.bg

    # ---- 用紙 -------------------------------------------------------------
    mm = PAPER_MM[a.paper]
    W = int(round(mm / 25.4 * a.dpi))
    H = int(round(W * PAPERS[a.paper]))
    sheet = Image.new("RGB", (W, H), bg)

    margin = int(W * a.margin)
    gap = int(W * 0.035)

    art_w = W - margin * 2
    art_h = round(base.height * art_w / base.width)
    art = base.resize((art_w, art_h), Image.LANCZOS)
    art_x, art_y = margin, margin
    sheet.paste(art, (art_x, art_y))

    band_y = art_y + art_h + gap
    band_h = H - margin - band_y
    if band_h < W * 0.16:
        print(f"警告: 下の帯が {band_h}px しかありません。"
              f"--margin を上げるか、縦長の用紙にしてください")

    qr_side = max(64, min(band_h, int(W * 0.36)))

    # ---- QR ---------------------------------------------------------------
    qr = Image.open(a.qr).convert("RGB").resize((qr_side, qr_side), Image.LANCZOS)
    qx, qy = margin, band_y + (band_h - qr_side) // 2
    sheet.paste(qr, (qx, qy))

    # ---- 文字 -------------------------------------------------------------
    d = ImageDraw.Draw(sheet)
    tx = qx + qr_side + gap
    f_title = load_font(int(W * 0.042), a.font)
    f_sub = load_font(int(W * 0.013), a.font)
    f_lead = load_font(int(W * 0.019), a.font)
    f_note = load_font(int(W * 0.015), a.font)

    ty = qy
    d.text((tx, ty), a.title, font=f_title, fill=a.fg)
    ty += int(W * 0.056)
    d.text((tx, ty), a.subtitle, font=f_sub, fill=a.accent)
    ty += int(W * 0.040)
    d.text((tx, ty), a.lead, font=f_lead, fill=a.fg)
    ty += int(W * 0.034)
    d.text((tx, ty), a.note, font=f_note, fill="#666666")

    # 帯の区切り。地色より少し暗い線を引く
    rule = tuple(max(0, c - 26) for c in bg) if isinstance(bg, tuple) else "#DDDDDD"
    d.line([(margin, band_y - gap // 2), (W - margin, band_y - gap // 2)],
           fill=rule, width=2)

    # ---- チェックボックスの位置 -------------------------------------------
    box = find_checkbox(base)
    scale = art_w / base.width
    coords = None
    if box:
        bx, by, bw, bh = box
        cx = art_x + (bx + bw / 2) * scale
        cy = art_y + (by + bh / 2) * scale
        coords = (ar_coords(cx, cy, W, H), bw * scale / W)
        if a.mark:
            d.rectangle([art_x + bx * scale, art_y + by * scale,
                         art_x + (bx + bw) * scale, art_y + (by + bh) * scale],
                        outline=(0, 255, 0), width=6)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(a.out)

    # ---- 報告 -------------------------------------------------------------
    print(f"出力      : {a.out} ({W}x{H}px)")
    print(f"用紙      : {a.paper.upper()} 幅{mm}mm / {a.dpi}dpi")
    print(f"絵        : {art_w}x{art_h}px（{art_w / a.dpi * 25.4:.0f}"
          f"x{art_h / a.dpi * 25.4:.0f}mm）")
    qr_mm = qr_side / a.dpi * 25.4
    print(f"QR        : {qr_side}px = {qr_mm:.0f}mm 角"
          f"{'' if qr_mm >= 60 else '  ← 小さい。60mm以上を推奨'}")

    print(f"読取確認  : {verify(sheet, a.expect)}")

    if coords:
        (X, Y), size = coords
        print("\n--- ARオーバーレイ座標（ar/index.html に貼る） ---")
        print(f"チェックボックス中心: x={X:+.4f}  y={Y:+.4f}  幅={size:.4f}")
        print('  <a-entity id="check" position='
              f'"{X:.3f} {Y:.3f} 0.01" scale="0 0 0">')
        print(f'  ※ チェック記号の大きさは {size:.3f} 前後にすると枠に収まります')
    else:
        print("\nチェックボックスを検出できませんでした（OpenCV未導入か、絵に青い空枠がない）")

    print("\n※ 印刷はマット紙で。光沢紙は反射でQRが読めなくなります")
    print("※ targets.mind は、この出力PNGそのものから生成してください")
    return 0


def verify(img: Image.Image, expected: str | None) -> str:
    """組み上げた紙面からQRが読めるか確認する。"""
    try:
        import cv2
    except ImportError:
        return "OpenCV未導入のためスキップ"
    det = cv2.QRCodeDetector()
    for width in (img.width, 1800, 1200, 900, 600):
        if width > img.width:
            continue
        small = img.convert("L").resize(
            (width, round(img.height * width / img.width)), Image.LANCZOS)
        try:
            data, _, _ = det.detectAndDecode(np.asarray(small))
        except Exception:
            data = ""
        if data and (expected is None or data == expected):
            return f"OK（{width}px幅で読取）→ {data[:60]}"
    return "!! 読み取れませんでした"


if __name__ == "__main__":
    raise SystemExit(main())

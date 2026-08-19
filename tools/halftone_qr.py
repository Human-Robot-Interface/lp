#!/usr/bin/env python3
"""ハーフトーンQR生成器.

任意の画像と任意のQR内容から、
「人間の目には画像に見え、機械にはQRとして読める」1枚を作る。

原理 (Chu et al. 2013 "Halftone QR Codes" の簡易版):
  1モジュールを s x s のサブピクセルに分割する。QRデコーダはモジュール中心
  しかサンプリングしないので、中心の core x core だけをQRの色に固定し、
  残りのサブピクセルを元画像のハーフトーン(誤差拡散ディザ)で埋める。
  ファインダ/タイミング/アライメント/形式情報は全サブピクセルを固定して
  検出安定性を確保する。

  ただし中心を固定するだけでは読めない。デコーダは局所的な二値化を挟むため、
  モジュール1つ分の平均輝度がQRの明暗と矛盾すると中心の値ごと潰される。
  そこで「絵の平均がQRと食い違うモジュールだけ」を margin の分だけ補正する。
  一致しているモジュールの階調はそのまま残るので絵が保たれる。

使い方:
  python tools/halftone_qr.py --image face.png --content "https://example.com" -o out.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import segno
from PIL import Image, ImageEnhance, ImageOps
from segno import consts

# 暗モジュールを表す segno の型定数
DARK_TYPES = frozenset({
    consts.TYPE_FINDER_PATTERN_DARK,
    consts.TYPE_ALIGNMENT_PATTERN_DARK,
    consts.TYPE_TIMING_DARK,
    consts.TYPE_FORMAT_DARK,
    consts.TYPE_VERSION_DARK,
    consts.TYPE_DATA_DARK,
    consts.TYPE_DARKMODULE,
})

# データ領域(=画像を載せてよい場所)以外はすべて機能パターンとして全面固定する
DATA_TYPES = frozenset({consts.TYPE_DATA_DARK, consts.TYPE_DATA_LIGHT})

# デコード検証に使う「1モジュールあたりのpx数」。実写での実用下限は4前後
PPM_LEVELS = (12, 8, 6, 4, 3)


# --------------------------------------------------------------------------
# QR
# --------------------------------------------------------------------------

def build_matrix(content: str, ecc: str, version, mask):
    """QRを生成し、(qr, dark, is_data) を返す。dark/is_data は bool 行列。"""
    qr = segno.make(content, error=ecc, version=version, mask=mask,
                    micro=False, boost_error=False)
    rows = list(qr.matrix_iter(scale=1, border=0, verbose=True))
    n = len(rows)
    dark = np.zeros((n, n), dtype=bool)
    is_data = np.zeros((n, n), dtype=bool)
    for y, row in enumerate(rows):
        for x, t in enumerate(row):
            dark[y, x] = t in DARK_TYPES
            is_data[y, x] = t in DATA_TYPES
    return qr, dark, is_data


def resolve_version(content: str, ecc: str, want_version):
    """指定バージョンで作れるか試し、データが入らなければ自動に落とす。

    絵の解像度はモジュール数で決まるため、短いURLでも大きめのバージョンを
    使ったほうが絵はきれいに出る。
    """
    if want_version is None:
        return None
    for v in range(int(want_version), 41):
        try:
            segno.make(content, error=ecc, version=v, micro=False,
                       boost_error=False)
            return v
        except Exception:
            continue
    return None


def choose_mask(content: str, ecc: str, version, block_mean: np.ndarray):
    """8種のマスクのうち、絵柄と最も一致するものを選ぶ。

    マスクを変えてもQRの内容は同じだが暗モジュールの配置が変わるため、
    元画像に近い配置を選ぶだけで絵の見え方がはっきり改善する。
    """
    best, best_err = None, None
    for m in range(8):
        try:
            _, dark, is_data = build_matrix(content, ecc, version, m)
        except Exception:
            continue
        if dark.shape != block_mean.shape:
            continue
        want = np.where(dark, 0.0, 1.0)
        err = float((((block_mean - want) ** 2) * is_data).sum())
        if best_err is None or err < best_err:
            best, best_err = m, err
    return best


def damage_data(dark: np.ndarray, is_data: np.ndarray, ratio: float,
                seed: int) -> int:
    """データ領域を帯状に潰す（意図的な「壊れたQR」演出）。

    よく言われる「レベルHは30%復元できる」は誤り訂正符号の割合であって
    許容被害率ではない。位置が未知の誤りは1つ訂正するのにEC符号を2つ消費する
    ため、実際に耐えられるのはコードワードの15%程度。実測でも塊で潰して
    10%が限界だった（--damage 0.10 前後まで）。

    また、ECCは「コードワード(8モジュール)単位」で復元するので、同じ被害率でも
    バラバラに反転させるとコードワードを広く汚して復元できない。塊で潰せば
    汚れるコードワード数が減るうえ、見た目も欠損/検閲らしくなる。
    機能パターンには触れないので検出自体は安定する。
    """
    if ratio <= 0:
        return 0
    rng = np.random.default_rng(seed)
    n = dark.shape[0]
    budget = int(is_data.sum() * ratio)
    hit = np.zeros_like(is_data)
    guard = 0
    while hit.sum() < budget and guard < 4000:
        guard += 1
        w = int(rng.integers(4, 13))
        h = int(rng.integers(2, 7))
        y = int(rng.integers(0, max(1, n - h)))
        x = int(rng.integers(0, max(1, n - w)))
        blk = np.zeros_like(is_data)
        blk[y:y + h, x:x + w] = True
        blk &= is_data
        if (hit | blk).sum() > budget:
            continue
        hit |= blk
    dark[hit] = True  # 黒く潰す
    return int(hit.sum())


# --------------------------------------------------------------------------
# 画像
# --------------------------------------------------------------------------

def prepare_target(path: Path, size: int, contrast: float, brightness: float,
                   invert: bool, fit: str, normalize: bool,
                   tone: tuple[float, float]) -> np.ndarray:
    """元画像を size x size のグレースケール float 配列 (0=黒, 1=白) にする。"""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "P"):
        # 透過は白背景に載せる（QRの下地は白なので）
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("L")

    if fit == "cover":
        img = ImageOps.fit(img, (size, size), method=Image.LANCZOS,
                           centering=(0.5, 0.4))
    else:  # contain: 余白は白
        img = ImageOps.contain(img, (size, size), method=Image.LANCZOS)
        canvas = Image.new("L", (size, size), 255)
        canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
        img = canvas

    if invert:
        img = ImageOps.invert(img)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)

    arr = np.asarray(img, dtype=np.float64) / 255.0

    if normalize:
        # 平均を中間調へ寄せる。QRは明暗ほぼ半々なので、絵も中間調のほうが乗る
        lo, hi = arr.min(), arr.max()
        if hi - lo > 1e-6:
            arr = (arr - lo) / (hi - lo)
        arr = np.clip(arr + (0.5 - arr.mean()), 0.0, 1.0)

    t_lo, t_hi = tone
    return t_lo + arr * (t_hi - t_lo)


def apply_margin(target: np.ndarray, mod_val: np.ndarray, s: int,
                 margin: float) -> np.ndarray:
    """QRと矛盾するモジュールだけを補正する。

    モジュール単位の平均輝度を、明モジュールなら 0.5+margin 以上、
    暗モジュールなら 0.5-margin 以下に押し込む。すでに条件を満たす
    モジュールには触れないので、絵の階調が残る。
    """
    if margin <= 0:
        return target
    n = mod_val.shape[0]
    want_light = mod_val > 0.5
    out = target
    for _ in range(4):  # クリップで崩れる分を数回で収束させる
        bm = out.reshape(n, s, n, s).mean(axis=(1, 3))
        goal = np.where(want_light,
                        np.maximum(bm, 0.5 + margin),
                        np.minimum(bm, 0.5 - margin))
        delta = goal - bm
        if np.abs(delta).max() < 1e-3:
            break
        out = np.clip(out + np.repeat(np.repeat(delta, s, axis=0), s, axis=1),
                      0.0, 1.0)
    return out


def dither(target: np.ndarray, forced_mask: np.ndarray, forced_val: np.ndarray,
           mode: str) -> np.ndarray:
    """固定サブピクセルを考慮した誤差拡散ディザ。返り値は 0/1 の float 配列。

    固定画素も「その値で出力した」ものとして誤差を周囲に配るため、
    QRによって強制された黒白が周辺の階調で補償される。
    """
    h, w = target.shape

    if mode == "none":
        out = (target >= 0.5).astype(np.float64)
        out[forced_mask] = forced_val[forced_mask]
        return out

    if mode == "ordered":
        bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                          [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float64)
        thr = (bayer + 0.5) / 16.0
        thr = np.tile(thr, (h // 4 + 1, w // 4 + 1))[:h, :w]
        out = (target >= thr).astype(np.float64)
        out[forced_mask] = forced_val[forced_mask]
        return out

    # Floyd-Steinberg
    out = np.zeros((h, w), dtype=np.float64)
    buf = target.copy()
    for y in range(h):
        row_f, row_v, row_o = forced_mask[y], forced_val[y], out[y]
        for x in range(w):
            old = buf[y, x]
            new = row_v[x] if row_f[x] else (1.0 if old >= 0.5 else 0.0)
            row_o[x] = new
            err = old - new
            if x + 1 < w:
                buf[y, x + 1] += err * 0.4375
            if y + 1 < h:
                if x > 0:
                    buf[y + 1, x - 1] += err * 0.1875
                buf[y + 1, x] += err * 0.3125
                if x + 1 < w:
                    buf[y + 1, x + 1] += err * 0.0625
    return out


# --------------------------------------------------------------------------
# 合成・検証
# --------------------------------------------------------------------------

def hex_to_rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise argparse.ArgumentTypeError(f"色は #RRGGBB 形式で指定してください: {s}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def compose(bits: np.ndarray, s: int, quiet_zone: int, subpixel_px: int,
            fg, bg) -> Image.Image:
    """0/1配列に余白を付けて着色し、最終解像度へ最近傍拡大する。"""
    grid = bits.shape[0]
    qz = quiet_zone * s
    canvas = np.ones((grid + qz * 2, grid + qz * 2), dtype=np.float64)
    canvas[qz:qz + grid, qz:qz + grid] = bits

    rgb = np.empty(canvas.shape + (3,), dtype=np.uint8)
    for c in range(3):
        rgb[..., c] = np.where(canvas < 0.5, fg[c], bg[c])
    img = Image.fromarray(rgb, "RGB")
    return img.resize((img.width * subpixel_px, img.height * subpixel_px),
                      Image.NEAREST)


def verify(img: Image.Image, expected: str, total_modules: int
           ) -> list[tuple[int, bool]]:
    """「1モジュール何pxで撮られたか」の水準ごとにデコードできるか検証する。

    読取可否を決めるのは絶対解像度ではなくモジュールあたりのpx数なので、
    カメラとの距離の目安としてそのまま使える。
    """
    try:
        import cv2
    except ImportError:
        return []
    det = cv2.QRCodeDetector()
    gray = img.convert("L")
    results = []
    for ppm in PPM_LEVELS:
        width = int(total_modules * ppm)
        if width > gray.width:
            continue
        small = gray.resize((width, round(gray.height * width / gray.width)),
                            Image.LANCZOS)
        try:
            data, _, _ = det.detectAndDecode(np.asarray(small))
        except Exception:
            data = ""
        results.append((ppm, data == expected))
    return results


# --------------------------------------------------------------------------

def render(a, dark, is_data, target_base, margin):
    """1つの margin でハーフトーンQRを描画する。"""
    n, s, core = dark.shape[0], a.subpixels, a.core
    lo, hi = (s - core) // 2, (s - core) // 2 + core
    grid = n * s

    mod_val = np.where(dark, 0.0, 1.0)  # 0=前景(暗) / 1=背景(明)
    forced_mask = np.zeros((grid, grid), dtype=bool)
    forced_val = np.zeros((grid, grid), dtype=np.float64)

    for y in range(n):
        for x in range(n):
            ys, xs = y * s, x * s
            v = mod_val[y, x]
            if is_data[y, x]:
                forced_mask[ys + lo:ys + hi, xs + lo:xs + hi] = True
                forced_val[ys + lo:ys + hi, xs + lo:xs + hi] = v
                if a.plus:
                    # 中心から上下左右へ腕を伸ばし、モジュールの平均を強める
                    forced_mask[ys:ys + s, xs + lo:xs + hi] = True
                    forced_val[ys:ys + s, xs + lo:xs + hi] = v
                    forced_mask[ys + lo:ys + hi, xs:xs + s] = True
                    forced_val[ys + lo:ys + hi, xs:xs + s] = v
            else:
                # 機能パターンは全サブピクセルを固定して検出を安定させる
                forced_mask[ys:ys + s, xs:xs + s] = True
                forced_val[ys:ys + s, xs:xs + s] = v

    target = target_base
    if a.bias > 0:
        mod_grid = np.repeat(np.repeat(mod_val, s, axis=0), s, axis=1)
        target = target * (1.0 - a.bias) + mod_grid * a.bias
    target = apply_margin(target, mod_val, s, margin)

    bits = dither(target, forced_mask, forced_val, a.dither)
    return compose(bits, s, a.quiet_zone, a.subpixel_px,
                   hex_to_rgb(a.fg), hex_to_rgb(a.bg))


def parse_margin(v):
    if isinstance(v, str) and v.lower() == "auto":
        return "auto"
    try:
        f = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError("--margin は数値か auto を指定してください")
    if not 0.0 <= f <= 0.5:
        raise argparse.ArgumentTypeError("--margin は 0〜0.5 の範囲です")
    return f


def build_parser():
    p = argparse.ArgumentParser(
        description="任意の画像＋任意の内容からハーフトーンQRを生成する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--image", required=True, type=Path,
                   help="埋め込む画像 (任意のPNG/JPG)")
    p.add_argument("--content", required=True, help="QRに入れる文字列 (URLなど)")
    p.add_argument("-o", "--out", type=Path, default=Path("halftone_qr.png"),
                   help="出力PNGパス")
    p.add_argument("--preview", type=Path, default=None,
                   help="離れて見たときの見え方を確認する縮小画像の出力先")

    g = p.add_argument_group("QR")
    g.add_argument("--ecc", default="h", choices=list("lmqh"),
                   help="誤り訂正レベル。ハーフトーン化するので h 推奨")
    g.add_argument("--version", default="15",
                   help="QRバージョン(1-40)/auto。大きいほど絵は細かく、モジュールは小さい")
    g.add_argument("--mask", type=int, default=None,
                   help="マスクパターン(0-7)。省略時は絵に最も合うものを自動選択")
    g.add_argument("--quiet-zone", type=int, default=4,
                   help="余白(モジュール数)。4未満は非推奨")

    g = p.add_argument_group("解像度")
    g.add_argument("--subpixels", type=int, default=5,
                   help="1モジュールの分割数 s (奇数)。5が標準、3は粗いが軽い")
    g.add_argument("--core", type=int, default=1,
                   help="モジュール中心の固定領域 core x core (奇数)")
    g.add_argument("--plus", action="store_true",
                   help="中心から十字に固定領域を広げる。読取は安定するが絵は粗くなる")
    g.add_argument("--subpixel-px", type=int, default=4,
                   help="サブピクセル1つの出力px")

    g = p.add_argument_group("絵づくり")
    g.add_argument("--margin", type=parse_margin, default="auto",
                   help="読取マージン。auto は読める中で最も絵が濃い値を自動探索する")
    g.add_argument("--target-ppm", type=int, default=6, choices=PPM_LEVELS,
                   help="auto が満たすべき読取水準(1モジュールあたりpx)。"
                        "小さいほど厳しく、絵は薄くなる")
    g.add_argument("--dither", default="floyd",
                   choices=("floyd", "ordered", "none"), help="ハーフトーンの方式")
    g.add_argument("--contrast", type=float, default=1.3, help="画像のコントラスト")
    g.add_argument("--brightness", type=float, default=1.0, help="画像の明るさ")
    g.add_argument("--invert", action="store_true", help="画像を白黒反転する")
    g.add_argument("--fit", default="cover", choices=("cover", "contain"),
                   help="画像の収め方")
    g.add_argument("--tone-low", type=float, default=0.02, help="使用する明度の下限")
    g.add_argument("--tone-high", type=float, default=0.98, help="使用する明度の上限")
    g.add_argument("--no-normalize", action="store_true",
                   help="中間調への自動正規化を切る")
    g.add_argument("--bias", type=float, default=0.0,
                   help="画像全体をQRの明暗へ一様に引き寄せる強さ(0-1)。通常は0でよい")

    g = p.add_argument_group("見た目・出力")
    g.add_argument("--fg", default="#000000", help="前景色 #RRGGBB")
    g.add_argument("--bg", default="#FFFFFF", help="背景色 #RRGGBB")
    g.add_argument("--damage", type=float, default=0.0,
                   help="データ領域を塊で潰す割合。「壊れたQR」演出用。"
                        "実測の限界は0.10前後で、ハーフトーンと併用するとさらに下がる")
    g.add_argument("--seed", type=int, default=0, help="damage の乱数シード")
    g.add_argument("--print-mm", type=float, default=None,
                   help="想定印刷サイズ(mm)。モジュールサイズの妥当性を判定する")
    g.add_argument("--no-verify", action="store_true", help="デコード検証をスキップ")
    return p


def main(argv=None) -> int:
    p = build_parser()
    a = p.parse_args(argv)

    if a.subpixels % 2 == 0 or a.subpixels < 3:
        p.error("--subpixels は 3 以上の奇数にしてください")
    if a.core % 2 == 0 or a.core < 1 or a.core > a.subpixels:
        p.error("--core は 1 以上 --subpixels 以下の奇数にしてください")
    if a.tone_low >= a.tone_high:
        p.error("--tone-low は --tone-high より小さくしてください")
    if not a.image.exists():
        p.error(f"画像が見つかりません: {a.image}")

    s = a.subpixels
    want_version = None if str(a.version).lower() == "auto" else a.version
    try:
        version = resolve_version(a.content, a.ecc, want_version)
        qr, dark, is_data = build_matrix(a.content, a.ecc, version, a.mask)
    except Exception as e:
        print(f"QRを生成できません: {e}", file=sys.stderr)
        return 1
    n = dark.shape[0]

    target_base = prepare_target(a.image, n * s, a.contrast, a.brightness,
                                 a.invert, a.fit, not a.no_normalize,
                                 (a.tone_low, a.tone_high))

    if a.mask is None:
        block_mean = target_base.reshape(n, s, n, s).mean(axis=(1, 3))
        picked = choose_mask(a.content, a.ecc, qr.version, block_mean)
        if picked is not None:
            qr, dark, is_data = build_matrix(a.content, a.ecc, qr.version, picked)

    flipped = damage_data(dark, is_data, a.damage, a.seed)
    total_modules = n + a.quiet_zone * 2

    # margin を決める。auto は「読める中で最も絵が濃い(=marginが小さい)」値を探す
    if a.margin == "auto":
        chosen, img, res = None, None, []
        for m in [round(0.04 + 0.02 * i, 2) for i in range(14)]:
            cand = render(a, dark, is_data, target_base, m)
            cres = verify(cand, a.content, total_modules)
            if not cres:  # OpenCV が無い場合は検証できないので既定値で確定
                chosen, img, res = 0.12, render(a, dark, is_data, target_base, 0.12), []
                break
            if all(ok for ppm, ok in cres if ppm >= a.target_ppm):
                chosen, img, res = m, cand, cres
                break
        if chosen is None:
            chosen = 0.30
            img = render(a, dark, is_data, target_base, chosen)
            res = verify(img, a.content, total_modules)
            print("警告: 目標の読取水準に届きませんでした。--plus を付ける、"
                  "--version を下げる、コントラストの高い画像を使う のいずれかを"
                  "検討してください", file=sys.stderr)
        margin = chosen
    else:
        margin = a.margin
        img = render(a, dark, is_data, target_base, margin)
        res = [] if a.no_verify else verify(img, a.content, total_modules)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    img.save(a.out)
    if a.preview:
        a.preview.parent.mkdir(parents=True, exist_ok=True)
        pw = total_modules * 3
        img.resize((pw, pw), Image.LANCZOS).resize(
            (pw * 3, pw * 3), Image.NEAREST).save(a.preview)

    free = s * s - (core_area := (a.core * a.core if not a.plus
                                  else a.core * s * 2 - a.core * a.core))
    print(f"出力       : {a.out}  ({img.width}x{img.height}px)")
    print(f"QR         : version {qr.version} / ECC {a.ecc.upper()} / "
          f"mask {qr.mask} / {n}x{n} modules")
    print(f"絵の解像度 : {n * s}x{n * s} サブピクセル "
          f"(1モジュールあたり {free}/{s * s} が絵に使える)")
    print(f"margin     : {margin:.2f}"
          f"{' (自動選択)' if a.margin == 'auto' else ''}")
    if flipped:
        print(f"damage     : データモジュール {flipped} 個を塊で潰した "
              f"({a.damage:.0%}) — 読めるかは下のデコード検証で判断すること")

    if a.print_mm:
        mod_mm = a.print_mm / total_modules
        dpi = img.width / (a.print_mm / 25.4)
        verdict = "OK" if mod_mm >= 0.8 else (
            "やや小さい" if mod_mm >= 0.6 else "小さすぎ")
        print(f"印刷       : {a.print_mm:.0f}mm角 → 1モジュール {mod_mm:.2f}mm "
              f"/ {dpi:.0f}dpi 相当 … {verdict}")
    else:
        print(f"印刷目安   : 余白込み {total_modules} モジュール。"
              f"1モジュール0.8mm確保なら {total_modules * 0.8:.0f}mm角以上で印刷")

    if res:
        line = "  ".join(f"{ppm}px/mod:{'OK' if ok else 'NG'}" for ppm, ok in res)
        print(f"デコード検証: {line}")
        ok_levels = [ppm for ppm, ok in res if ok]
        if not ok_levels:
            print("  !! 一度も読めていません。--margin を上げる / --plus を付ける "
                  "を試してください", file=sys.stderr)
            return 1
        print(f"  → 1モジュール {min(ok_levels)}px 以上で撮れば読める "
              f"(実写での実用下限は 4px/mod 目安)")
    elif not a.no_verify:
        print("検証       : OpenCV未導入のためスキップ (pip install opencv-python-headless)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

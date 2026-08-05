"""Console visual contract, enforced by code rather than by discipline.

The token system in `console/static/css/base.css` used to be aspirational: 70 of
78 font sizes were literals spread over 19 values, twelve font weights were in
use, and the sidebar carried a second hand-written palette. These tests keep
that from coming back, and additionally verify two palette invariants:

  1. a token's hue angle matches across themes (OKLCH dH <= 12 deg) while
     lightness inverts;
  2. the light page background is never pure white.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

CSS_DIR = Path(__file__).resolve().parents[1] / "src/tracecast/console/static/css"
LAYERS = ["base.css", "layout.css", "components.css", "forms.css",
          "reports.css", "responsive.css"]
# base.css owns the token definitions; every other layer must reference them.
CONSUMERS = [name for name in LAYERS if name != "base.css"]


def read(name: str) -> str:
    return (CSS_DIR / name).read_text(encoding="utf-8")


def lines(name: str):
    return list(enumerate(read(name).splitlines(), 1))


def fail_lines(hits, rule: str) -> str:
    body = "\n".join(f"  {name}:{no}  {text.strip()}" for name, no, text in hits)
    return f"{rule}\n{body}\n\nToken definitions and invariants live in css/base.css."


# --------------------------------------------------------------------------
# Scale discipline
# --------------------------------------------------------------------------

def test_no_literal_font_sizes():
    hits = [(name, no, text) for name in CONSUMERS for no, text in lines(name)
            if re.search(r"font-size:\s*[0-9.]+(px|pt|em|rem)", text)]
    assert not hits, fail_lines(hits, "字号必须用 --text-* token，不得写字面值：")


def test_no_literal_font_weights():
    hits = [(name, no, text) for name in CONSUMERS for no, text in lines(name)
            if re.search(r"font-weight:\s*[0-9]+", text)]
    assert not hits, fail_lines(hits, "字重必须用 --w-* token（只有 4 档）：")


def test_no_literal_colours_outside_base():
    pattern = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(")
    hits = [(name, no, text) for name in CONSUMERS for no, text in lines(name)
            if pattern.search(text)]
    assert not hits, fail_lines(hits, "颜色必须用语义 token，不得写字面色值：")


def test_no_offscale_border_radius():
    allowed = {"50%", "0", "inherit"}
    hits = []
    for name in CONSUMERS:
        for no, text in lines(name):
            for match in re.finditer(r"border-radius:\s*([^;}]+)", text):
                value = match.group(1).strip()
                if "var(--radius" in value or value in allowed:
                    continue
                # hairline decorations (<=2px) are not scale members
                if re.fullmatch(r"[0-2]px", value):
                    continue
                if re.fullmatch(r"var\(--radius[a-z-]*\)( 0)+", value):
                    continue
                if all(part in allowed or "var(--radius" in part
                       for part in value.split()):
                    continue
                hits.append((name, no, text))
    assert not hits, fail_lines(hits, "圆角必须用 --radius-* token：")


def test_every_var_is_defined():
    css = "\n".join(read(name) for name in LAYERS)
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    missing = sorted(used - defined - {"--run-depth"})
    assert not missing, f"引用了未定义的 token: {missing}"


def test_textual_input_types_share_the_form_control_contract():
    css = read("forms.css")
    for selector in ("input[type=text]", "input[type=password]", "input[type=url]"):
        assert selector in css, f"{selector} 必须复用标准表单控件样式"


def test_no_dead_tokens():
    css = "\n".join(read(name) for name in LAYERS)
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    # --rail is an alias documented as "same value as --canvas" in the dark
    # theme and is read by layout.css; everything else must be consumed.
    dead = sorted(defined - used - {"--run-depth"})
    assert not dead, f"定义了但无人使用的 token（删掉或用起来）: {dead}"


# --------------------------------------------------------------------------
# Palette: colour space helpers
# --------------------------------------------------------------------------

def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(hex_value: str) -> tuple[int, int, int]:
    h = hex_value.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def oklch(hex_value: str) -> tuple[float, float, float]:
    r, g, b = (_linear(v) for v in _rgb(hex_value))
    long = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    med = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    short = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (v ** (1 / 3) for v in (long, med, short))
    lightness = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return lightness * 100, math.hypot(a, bb), math.degrees(math.atan2(bb, a)) % 360


def contrast(a: str, b: str) -> float:
    def lum(hex_value: str) -> float:
        r, g, bl = (_linear(v) for v in _rgb(hex_value))
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def theme_tokens(selector: str) -> dict[str, str]:
    """Pull `--name: #hex;` pairs out of one base.css rule block."""
    css = read("base.css")
    start = css.index(selector) + len(selector)
    depth, end = 1, start   # the selector's own "{" is already consumed
    for index in range(start, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    block = css[start:end]
    return {name: value.lower()
            for name, value in re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", block)}


@pytest.fixture(scope="module")
def palettes() -> tuple[dict[str, str], dict[str, str]]:
    dark = theme_tokens(":root {")
    light = theme_tokens('[data-theme="light"] {')
    assert dark and light, "base.css 主题块解析失败"
    return dark, light


# --------------------------------------------------------------------------
# Palette rules
# --------------------------------------------------------------------------

def test_both_themes_define_the_same_tokens(palettes):
    dark, light = palettes
    assert sorted(dark) == sorted(light), (
        "两个主题必须绑定同一组 token 名 —— 差异："
        f"{sorted(set(dark) ^ set(light))}")


def test_hue_locked_across_themes(palettes):
    dark, light = palettes
    drift = []
    for name in dark:
        dl, dc, dh = oklch(dark[name])
        ll, lc, lh = oklch(light[name])
        if dc <= 0.02 or lc <= 0.02:   # near-neutral: hue carries no meaning
            continue
        delta = abs((dh - lh + 180) % 360 - 180)
        if delta > 12:
            drift.append(f"{name}: 暗 {dh:.0f}° vs 亮 {lh:.0f}° (Δ{delta:.0f}°)")
    assert not drift, "同名 token 跨主题色相漂移 >12°：\n  " + "\n  ".join(drift)


def test_chromatic_tokens_invert_lightness(palettes):
    dark, light = palettes
    bad = []
    for name in dark:
        if not name.startswith(("--accent", "--ok", "--warn", "--err")):
            continue
        if name.endswith(("-soft", "-border")) or name == "--on-accent":
            continue
        dl, dc, _ = oklch(dark[name])
        ll, lc, _ = oklch(light[name])
        if dc <= 0.02 or lc <= 0.02:
            continue
        if dl <= ll:
            bad.append(f"{name}: 暗 L*{dl:.0f} 应亮于 亮 L*{ll:.0f}")
    assert not bad, "暗色模式下的语义色必须是两者中较亮的一个：\n  " + "\n  ".join(bad)


def test_light_background_is_not_pure_white(palettes):
    _, light = palettes
    for name in ("--canvas", "--rail", "--panel"):
        assert light[name] != "#ffffff", (
            f"{name} 不能是纯白：卡片层级需要页面底色之上仍有余量")
    # --raised is the topmost card layer and must still sit above --panel
    assert oklch(light["--raised"])[0] > oklch(light["--panel"])[0] > oklch(light["--canvas"])[0]


# text token -> surfaces it is allowed to sit on. --dim is deliberately absent
# from --raised (it lands at 4.47, just under AA).
TEXT_ON_SURFACE = {
    "--fg": ("--canvas", "--panel", "--raised"),
    "--soft": ("--canvas", "--panel"),
    "--dim": ("--canvas", "--panel"),
    "--reading": ("--canvas", "--panel"),
    "--accent-text": ("--canvas", "--panel", "--raised"),
    "--ok": ("--canvas", "--panel", "--ok-soft"),
    "--warn": ("--canvas", "--panel", "--warn-soft"),
    "--err": ("--canvas", "--panel", "--err-soft"),
}


@pytest.mark.parametrize("theme_index,label", [(0, "dark"), (1, "light")])
def test_text_meets_wcag_aa(palettes, theme_index, label):
    palette = palettes[theme_index]
    failures = []
    for text, surfaces in TEXT_ON_SURFACE.items():
        for surface in surfaces:
            ratio = contrast(palette[text], palette[surface])
            if ratio < 4.5:
                failures.append(f"{label} {text} on {surface}: {ratio:.2f} < 4.5")
    assert not failures, "正文对比度不足 AA：\n  " + "\n  ".join(failures)


@pytest.mark.parametrize("theme_index,label", [(0, "dark"), (1, "light")])
def test_solid_button_ink_is_readable(palettes, theme_index, label):
    palette = palettes[theme_index]
    ratio = contrast(palette["--on-accent"], palette["--accent"])
    assert ratio >= 4.5, (
        f"{label}: --on-accent 落在 --accent 上只有 {ratio:.2f}。"
        "暗色下 accent 是亮蓝，实心按钮需要墨色字而不是白字。")


@pytest.mark.parametrize("theme_index,label", [(0, "dark"), (1, "light")])
def test_dividers_are_visible(palettes, theme_index, label):
    palette = palettes[theme_index]
    ratio = contrast(palette["--border"], palette["--canvas"])
    assert ratio >= 1.25, f"{label}: --border 对 --canvas 只有 {ratio:.2f}，分隔线看不见"

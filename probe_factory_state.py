#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_factory_state.py — 读出已安装的 DeepSeek Harness 桌面版的「官方原厂态」。

DSH 桌面版把 profile 的初始化逻辑烧在 Electron 的 app.asar 里（/lib/main.js），
其中包含：

  PROFILE_TEMPLATES        每个内置 profile 模板的初始 bundle 层
  PROFILE_PATCH_TEMPLATE   cordis.patch.yml 的初始内容（空数组 []）
  PROFILE_PNPM_WORKSPACE   pnpm-workspace.yaml 的初始内容
  initProfile()            首次初始化时落地的三个文件

DSH 是 developer preview，官方原厂态可能随版本改变。所以「还原」之前先探测，
而不是硬编码一份 2026-10 的记忆。

本脚本只读，不改动任何东西。

用法:
  python probe_factory_state.py                 # 自动定位已安装的 app.asar
  python probe_factory_state.py --asar <path>   # 指定 app.asar
  python probe_factory_state.py --json          # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# app.asar 的常见位置（%VAR% / ~ 会被展开）
CANDIDATES = {
    "win32": [
        r"%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\app.asar",
        r"%PROGRAMFILES%\DeepSeek Harness\resources\app.asar",
    ],
    "darwin": [
        "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar",
        "~/Applications/DeepSeek Harness.app/Contents/Resources/app.asar",
    ],
    "linux": [
        "/opt/DeepSeek Harness/resources/app.asar",
        "/opt/deepseek-harness/resources/app.asar",
        "~/.local/share/deepseek-harness/resources/app.asar",
    ],
}

MAX_HEADER = 1 << 26  # 64 MiB，asar 的 JSON 头最大给到这里


def find_asar(explicit: str | None = None) -> Path | None:
    """定位 app.asar；explicit 优先，其次按平台候选路径，最后扫一轮常见目录。"""
    if explicit:
        p = Path(os.path.expandvars(explicit)).expanduser()
        return p if p.is_file() else None

    seen: list[Path] = []
    for raw in CANDIDATES.get(sys.platform, []):
        p = Path(os.path.expandvars(raw)).expanduser()
        seen.append(p)
        if p.is_file():
            return p

    # 兜底：扫一遍可能装桌面版的目录
    guesses: list[Path] = []
    if sys.platform == "win32":
        for root in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES", "")):
            base = Path(root) / "Programs" if root and "LOCALAPPDATA" in root else Path(root or ".")
            if base.is_dir():
                guesses += [d / "resources" / "app.asar" for d in base.glob("*Harness*")]
    elif sys.platform == "darwin":
        guesses += [Path("/Applications").glob("*Harness*.app/Contents/Resources/app.asar")]
    for g in guesses:
        for p in ([g] if isinstance(g, Path) else g):
            if p.is_file():
                return p
    return None


def _read_header(fh) -> tuple[dict, int]:
    """解析 asar 头，返回 (header_dict, 数据区起始偏移)。"""
    fh.seek(0)
    probe = fh.read(64)
    brace = probe.find(b"{")
    if brace < 0:
        raise ValueError("找不到 asar 的 JSON 头，文件可能不是 asar")

    size = 1 << 20
    while size <= MAX_HEADER:
        fh.seek(brace)
        buf = fh.read(size)
        text = buf.decode("utf-8", "ignore")
        try:
            header, end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            size <<= 1
            continue
        # 头里非 ASCII 都被转义成 \uXXXX，所以字符数 == 字节数；仍按字节安全换算
        return header, brace + len(text[:end].encode("utf-8"))
    raise ValueError("asar JSON 头解析失败（超过 %d 字节）" % MAX_HEADER)


def _resolve(files: dict, member: str) -> dict:
    node = files
    for part in member.strip("/").split("/"):
        if "files" in node:
            node = node["files"]
        node = node[part]
    return node


def read_member(asar: Path, member: str) -> bytes:
    """从 asar 里读出某个成员文件的原始字节。"""
    with asar.open("rb") as fh:
        header, base = _read_header(fh)
        entry = _resolve(header["files"], member)
        fh.seek(base + int(entry["offset"]))
        return fh.read(int(entry["size"]))


def _unescape_template(literal: str) -> str:
    """把 JS 模板字符串里的转义还原：\\` -> ` ，\\\\ -> \\ 。"""
    return literal.replace("\\`", "`").replace("\\\\", "\\")


def read_factory(asar: Path) -> dict:
    """从 app.asar 提取原厂态。"""
    main_js = read_member(asar, "/lib/main.js").decode("utf-8", "ignore")

    templates_block = re.search(r"const PROFILE_TEMPLATES = \{(.*?)\n\};", main_js, re.S)
    templates: dict[str, list[str]] = {}
    if templates_block:
        for name, bundles in re.findall(
            r'(?:^|\n)\s*"?([A-Za-z0-9_-]+)"?:\s*\{\s*bundles:\s*\[([^\]]*)\]',
            templates_block.group(1),
        ):
            templates[name] = re.findall(r'"([^"]+)"', bundles)

    patch = re.search(r"const PROFILE_PATCH_TEMPLATE = `(.*?)`;", main_js, re.S)
    workspace = re.search(r"const PROFILE_PNPM_WORKSPACE = `(.*?)`;", main_js, re.S)

    return {
        "asar": str(asar),
        "profile_templates": templates,
        "patch_template": _unescape_template(patch.group(1)) if patch else None,
        "pnpm_workspace": _unescape_template(workspace.group(1)) if workspace else None,
        "js_files_created": ["package.json", "cordis.patch.yml", "pnpm-workspace.yaml"],
    }


def describe(factory: dict) -> str:
    out: list[str] = []
    out.append("app.asar: %s" % factory["asar"])
    out.append("")
    out.append("内置 profile 模板 → 初始 bundle 层：")
    for name, bundles in factory["profile_templates"].items():
        out.append("  %-12s %s" % (name, ", ".join(bundles)))
    out.append("")
    out.append("桌面版注：桌面 profile 不是独立模板，由 Electron 端按 web 模板初始化")
    out.append("（见 app.asar 里 createPluginProfile → initProfile(WEB_PROFILE.bundles)），")
    out.append("所以桌面 profile 的原厂 bundle 层 = %s"
               % ", ".join(factory["profile_templates"].get("web", ["<未解析>"])))
    out.append("")
    out.append("initProfile() 首次落地这三个文件：%s" % ", ".join(factory["js_files_created"]))
    out.append("")
    out.append("cordis.patch.yml 初始内容 ——")
    out.append((factory["patch_template"] or "<未解析>").rstrip("\n"))
    out.append("")
    out.append("pnpm-workspace.yaml 初始内容 ——")
    out.append((factory["pnpm_workspace"] or "<未解析>").rstrip("\n"))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="读 DeepSeek Harness 桌面版的原厂 profile 态")
    ap.add_argument("--asar", help="app.asar 路径（默认自动定位）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    asar = find_asar(args.asar)
    if not asar:
        print("× 没找到 DeepSeek Harness 的 app.asar。用 --asar 手动指定，例如：", file=sys.stderr)
        print("  --asar \"%LOCALAPPDATA%\\Programs\\DeepSeek Harness\\resources\\app.asar\"", file=sys.stderr)
        return 2

    try:
        factory = read_factory(asar)
    except Exception as exc:  # noqa: BLE001
        print("× 解析失败：%s" % exc, file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(factory, ensure_ascii=False, indent=2))
    else:
        print(describe(factory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

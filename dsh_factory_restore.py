#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh_factory_restore.py — 把 DeepSeek Harness 桌面版的插件 profile 还原成「刚安装时的官方状态」。

背景
----
DSH 的架构是 everything-is-a-plugin：记忆、工具、界面、甚至 agent loop 都是插件层。
装几个社区插件之后，profile 目录里就会堆满「策略 / 开关 / 组合」，而官方**没有**任何
「恢复到初始状态」的按钮（只有 Electron 端的隐藏 safe-mode 会清 bundle 层）。

官方原厂态（由 probe_factory_state.py 从 app.asar 里读出来，不是猜的）
--------------------------------------------------------------------
  profiles/<name>/package.json          dependencies 为空 {}
                                        bundles = [@deepseek-ai/dsh-base, @deepseek-ai/dsh-web-app]
  profiles/<name>/cordis.patch.yml      4 行注释 + 空数组 []
  profiles/<name>/pnpm-workspace.yaml   packages / nodeLinker / autoInstallPeers
  （没有 node_modules、pnpm-lock.yaml、.plugin-manager）

本脚本做的事
------------
  1. 备份整个 profile（含第三方插件与配置）+ 用户侧数据 → 备份目录
  2. 退出桌面程序（优雅关闭，失败则强制结束进程树）
  3. 写回上面三个原厂文件，删掉 node_modules / pnpm-lock.yaml / .plugin-manager / 生成的 cordis.yml
  4. 把插件产生的数据目录（notes/、speech-to-text/）移进备份（--keep-* 可保留）
  5. 重启桌面程序，并复核还原结果

不做的事：不碰 .credentials.yaml、sessions/、storages/、skills/（这些是你的数据，只会备份、不会删）。

用法
----
  python dsh_factory_restore.py                    # 打印计划后要求确认
  python dsh_factory_restore.py --dry-run          # 只看计划，不动任何东西
  python dsh_factory_restore.py --yes              # 不提问直接还原（会先备份）
  python dsh_factory_restore.py --yes --keep-notes # notes/ 留在原地
  python dsh_factory_restore.py --yes --no-relaunch

回滚：把备份目录里的 profile-before/ 覆盖回 ~/.dsh/profiles/<name>/ 即可。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

APP_NAME = "DeepSeek Harness"
WIN_EXE = "DeepSeek Harness.exe"

# 原厂态兜底常量（探测不到 app.asar 时使用；探测到了则以已安装版本为准）
FACTORY_BUNDLES_FALLBACK = ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]
PATCH_TEMPLATE_FALLBACK = (
    "# Your patch layer for this dsh profile, applied after every bundle layer:\n"
    "# a top-level YAML array of loader patch entries (id-targeted config\n"
    "# overrides, disables, and insert lists; `!!js` expressions allowed).\n"
    "[]\n"
)
WORKSPACE_FALLBACK = "packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n"

# profile 里的「非原厂残留」，还原时删除
PROFILE_JUNK = ["node_modules", "pnpm-lock.yaml", ".plugin-manager", ".dsh-module-fallback"]
# 自动生成的组合树快照：启动时会重建，还原时顺手删掉
PROFILE_GENERATED = ["cordis.yml"]
# 插件产生、随插件一起下架的数据目录（移进备份，不删除）
HOME_PLUGIN_DIRS = ["notes", "speech-to-text"]
# 用户数据：只备份，绝不删
HOME_KEEP = [".credentials.yaml", ".anonymous-user-id", "sessions", "storages", "skills"]


# --------------------------------------------------------------------------- #
# 环境探测
# --------------------------------------------------------------------------- #
def resolve_dsh_home(explicit: str | None) -> Path:
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser()
    if os.environ.get("DSH_HOME"):
        return Path(os.environ["DSH_HOME"]).expanduser()
    return Path.home() / ".dsh"


def app_exe_candidates() -> list[Path]:
    """桌面程序可执行文件/包的候选路径。"""
    if sys.platform == "win32":
        roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", "")]
        cands = []
        for root in roots:
            if not root:
                continue
            base = Path(root) / "Programs"
            cands.append(base / APP_NAME / WIN_EXE)
            cands.append(Path(root) / APP_NAME / WIN_EXE)
        return cands
    if sys.platform == "darwin":
        return [Path("/Applications") / f"{APP_NAME}.app", Path.home() / "Applications" / f"{APP_NAME}.app"]
    return [Path("/opt") / APP_NAME / "deepseek-harness", Path.home() / ".local/share/deepseek-harness/deepseek-harness"]


def find_app_exe(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(os.path.expandvars(explicit)).expanduser()
        return p if p.exists() else None
    for p in app_exe_candidates():
        if p.exists():
            return p
    return None


def _run_quiet(args: list[str]) -> tuple[int, bytes]:
    """跑一条命令，原始字节返回（Windows 上 tasklist/taskkill 是本地代码页输出，
    用 text=True 会因 GBK 解码炸掉读取线程，所以一律按字节处理）。"""
    try:
        proc = subprocess.run(args, capture_output=True, timeout=60)
        return proc.returncode, proc.stdout or b""
    except Exception:  # noqa: BLE001
        return 1, b""


def app_is_running(explicit_exe: Path | None) -> bool:
    if sys.platform == "win32":
        _, raw = _run_quiet(["tasklist", "/FI", f"IMAGENAME eq {WIN_EXE}"])
        # 只比 ASCII 的进程名，完全不依赖输出编码
        return WIN_EXE.lower().encode() in raw.lower()
    code, _ = _run_quiet(["pgrep", "-f", APP_NAME])
    return code == 0


def stop_app() -> bool:
    """优雅关闭 → 仍存活则强制结束进程树。返回是否已无进程。"""
    if sys.platform == "win32":
        _run_quiet(["taskkill", "/IM", WIN_EXE])
    elif sys.platform == "darwin":
        _run_quiet(["osascript", "-e", f'quit app "{APP_NAME}"'])
    else:
        _run_quiet(["pkill", "-f", APP_NAME])
    time.sleep(7)
    if app_is_running(None):
        if sys.platform == "win32":
            _run_quiet(["taskkill", "/F", "/T", "/IM", WIN_EXE])
        else:
            _run_quiet(["pkill", "-f", APP_NAME])
        time.sleep(5)
    return not app_is_running(None)


def start_app(exe: Path | None) -> bool:
    if exe is None:
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(exe))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", APP_NAME])
        else:
            subprocess.Popen([str(exe)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 原厂态
# --------------------------------------------------------------------------- #
def factory_state(source: str, asar_hint: str | None) -> tuple[list[str], str, str, str]:
    """返回 (bundles, patch_template, workspace_template, 来源说明)。"""
    if source != "builtin":
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import probe_factory_state as probe  # type: ignore

            asar = probe.find_asar(asar_hint)
            if asar:
                data = probe.read_factory(asar)
                bundles = (data.get("profile_templates") or {}).get("web")
                patch = data.get("patch_template")
                workspace = data.get("pnpm_workspace")
                if bundles and patch and workspace:
                    return bundles, patch, workspace, f"探测自已安装版本 ({asar.name})"
            if source == "probe":
                raise RuntimeError("探测失败：没找到或解析不了 app.asar")
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001
            if source == "probe":
                raise
    return FACTORY_BUNDLES_FALLBACK, PATCH_TEMPLATE_FALLBACK, WORKSPACE_FALLBACK, "内置常量兜底"


def build_manifest(profile_name: str, bundles: list[str]) -> str:
    return json.dumps({
        "name": f"dsh-profile-{profile_name}",
        "private": True,
        "dependencies": {},
        "dsh": {"profile": {"bundles": list(bundles)}},
    }, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- #
# 还原动作
# --------------------------------------------------------------------------- #
def write_pristine(profile_dir: Path, bundles: list[str], patch: str, workspace: str) -> list[str]:
    written = []
    (profile_dir / "package.json").write_text(build_manifest(profile_dir.name, bundles), encoding="utf-8")
    written.append("package.json")
    (profile_dir / "cordis.patch.yml").write_text(patch, encoding="utf-8")
    written.append("cordis.patch.yml")
    (profile_dir / "pnpm-workspace.yaml").write_text(workspace, encoding="utf-8")
    written.append("pnpm-workspace.yaml")
    return written


def remove_junk(profile_dir: Path) -> list[str]:
    removed = []
    for name in PROFILE_JUNK + PROFILE_GENERATED:
        target = profile_dir / name
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        removed.append(name)
    return removed


def backup_everything(dsh_home: Path, profile_dir: Path, backup_dir: Path,
                      keep_plugin_dirs: set[str]) -> dict[str, str]:
    """备份 profile 与用户数据；插件数据目录是「移动」（可 --keep-* 保留在原地）。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    moved: dict[str, str] = {}

    shutil.copytree(profile_dir, backup_dir / "profile-before", symlinks=True, dirs_exist_ok=True)
    moved["profile-before"] = str(backup_dir / "profile-before")

    for name in HOME_KEEP:
        src = dsh_home / name
        if not src.exists():
            continue
        dst = backup_dir / f"{name}-before"
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        moved[f"{name}-before"] = str(dst)

    for name in HOME_PLUGIN_DIRS:
        src = dsh_home / name
        if not src.exists():
            continue
        if name in keep_plugin_dirs:
            moved[f"{name} (保留在原地)"] = str(src)
            continue
        dst = backup_dir / name
        shutil.move(str(src), str(dst))
        moved[name] = str(dst)
    return moved


def verify(profile_dir: Path, bundles: list[str]) -> list[str]:
    """复核还原结果，返回问题清单（空 = 全部通过）。"""
    problems: list[str] = []
    manifest_path = profile_dir / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"package.json 读不出来/不是合法 JSON：{exc}"]

    deps = manifest.get("dependencies") or {}
    if deps:
        problems.append(f"package.json 里还剩第三方依赖：{', '.join(deps)}")
    got = (manifest.get("dsh", {}).get("profile", {}) or {}).get("bundles")
    if got != bundles:
        problems.append(f"bundle 层不是原厂列表：{got}")

    patch_path = profile_dir / "cordis.patch.yml"
    if not patch_path.exists():
        problems.append("cordis.patch.yml 丢了")
    else:
        rows = [ln for ln in patch_path.read_text(encoding="utf-8").splitlines()
                if ln.lstrip().startswith("- ")]
        if rows:
            problems.append(f"patch 层还有 {len(rows)} 行自定义条目")

    for name in PROFILE_JUNK:
        if (profile_dir / name).exists():
            problems.append(f"{name} 还在")
    return problems


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把 DeepSeek Harness 桌面版的插件 profile 还原成刚安装时的官方状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dsh-home", help="DSH 主目录（默认 $DSH_HOME 或 ~/.dsh）")
    ap.add_argument("--profile", default="desktop", help="要还原的 profile 名（默认 desktop）")
    ap.add_argument("--backup-dir", help="备份目录（默认 ~/dsh-factory-restore-backups/<时间戳>）")
    ap.add_argument("--app", help="桌面程序可执行文件路径（默认自动定位）")
    ap.add_argument("--asar", help="app.asar 路径，用于探测原厂态")
    ap.add_argument("--factory-source", choices=["auto", "probe", "builtin"], default="auto",
                    help="原厂态来源：auto=能探测就探测（默认），probe=必须探测，builtin=用内置常量")
    ap.add_argument("--keep-notes", action="store_true", help="notes/ 留在原地（默认移进备份）")
    ap.add_argument("--keep-speech-to-text", action="store_true", help="speech-to-text/ 留在原地")
    ap.add_argument("--no-stop", action="store_true", help="不退出桌面程序（不推荐：它可能写回配置）")
    ap.add_argument("--no-relaunch", action="store_true", help="还原后不重启桌面程序")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不动任何东西")
    ap.add_argument("--yes", action="store_true", help="跳过确认")
    ap.add_argument("--json", action="store_true", help="额外输出机器可读摘要")
    args = ap.parse_args(argv)

    dsh_home = resolve_dsh_home(args.dsh_home)
    profile_dir = dsh_home / "profiles" / args.profile
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(os.path.expandvars(args.backup_dir)).expanduser() if args.backup_dir \
        else Path.home() / "dsh-factory-restore-backups" / stamp
    keep = set()
    if args.keep_notes:
        keep.add("notes")
    if args.keep_speech_to_text:
        keep.add("speech-to-text")

    print("=" * 68)
    print("DSH 桌面版出厂还原")
    print("=" * 68)
    print(f"DSH 主目录 : {dsh_home}")
    print(f"目标 profile: {profile_dir}")
    print(f"备份目录   : {backup_dir}")

    if not profile_dir.is_dir():
        print(f"\n× 找不到 profile 目录 {profile_dir}\n"
              "  —— 路径不对？用 --dsh-home 指定 DSH 主目录。", file=sys.stderr)
        return 2

    try:
        bundles, patch, workspace, origin = factory_state(args.factory_source, args.asar)
    except Exception as exc:  # noqa: BLE001
        print(f"\n× 原厂态探测失败：{exc}", file=sys.stderr)
        return 3
    print(f"原厂态来源 : {origin}")
    print(f"原厂 bundle: {', '.join(bundles)}")

    exe = find_app_exe(args.app)
    running = app_is_running(exe)
    print(f"桌面程序   : {exe or '未找到'}（{'运行中' if running else '未运行'}）")

    plan: list[str] = ["备份 profile + 用户数据（凭证/会话/工作区/技能只备份不删）"]
    if not args.no_stop:
        plan.append("退出桌面程序（优雅关闭 → 强制结束进程树）")
    plan.append("写回原厂三件套（package.json / cordis.patch.yml / pnpm-workspace.yaml）")
    plan.append(f"删除：{'、'.join(PROFILE_JUNK + PROFILE_GENERATED)}")
    for name in HOME_PLUGIN_DIRS:
        if (dsh_home / name).exists():
            plan.append(f"{name}/ → {'保留原地' if name in keep else '移进备份'}")
    if not args.no_relaunch:
        plan.append("重启桌面程序")
    plan.append("复核还原结果")
    print("\n将执行：")
    for i, item in enumerate(plan, 1):
        print(f"  {i}. {item}")

    if args.dry_run:
        print("\n(--dry-run：什么都没动)")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("\n× 需要 --yes 才能执行（当前不是交互终端）。", file=sys.stderr)
            return 4
        if input("\n确认执行？输入 yes 继续：").strip().lower() != "yes":
            print("已取消。")
            return 0

    summary: dict[str, object] = {"profile": str(profile_dir), "backup": str(backup_dir),
                                  "factory_origin": origin, "bundles": bundles}

    if not args.no_stop:
        ok = stop_app()
        print(f"\n[1] 退出桌面程序：{'✓ 已全部退出' if ok else '× 还有进程在跑（继续，但可能被写回）'}")

    print("[2] 备份 ...")
    moved = backup_everything(dsh_home, profile_dir, backup_dir, keep)
    for k, v in moved.items():
        print(f"      {k} → {v}")
    summary["backup_moved"] = moved

    print("[3] 写回原厂态 ...")
    written = write_pristine(profile_dir, bundles, patch, workspace)
    print(f"      写入 {', '.join(written)}")

    print("[4] 清理插件残留 ...")
    removed = remove_junk(profile_dir)
    print(f"      删除 {', '.join(removed) if removed else '（无）'}")
    summary["removed"] = removed

    if not args.no_relaunch:
        started = start_app(exe)
        print(f"[5] 重启桌面程序：{'✓ 已启动' if started else '× 没找到/启动失败，请手动打开'}")
        time.sleep(8)
    else:
        print("[5] 跳过重启（--no-relaunch）")

    problems = verify(profile_dir, bundles)
    print("\n复核：")
    if problems:
        for p in problems:
            print(f"  × {p}")
    else:
        print("  ✓ 原厂态已就位：依赖为空、bundle 层只有 dsh-base + dsh-web-app、patch 层为空、无插件残留")

    print(f"\n备份在 : {backup_dir}")
    print( "回滚   : 把该目录下的 profile-before/ 覆盖回 " + str(profile_dir))
    summary["problems"] = problems
    if args.json:
        print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

# DSH 出厂还原 · dsh-factory-restore

> 一句话：把 DeepSeek Harness 桌面版被插件改花的 profile，退回「刚安装时的官方状态」。

---

## 为什么需要它

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`）是 DeepSeek 的
agent harness，架构是 **everything-is-a-plugin** —— 模型、工具、沙箱、界面、甚至 agent loop
都是插件层。好处是随便换；代价是：

- 装几个社区插件之后，profile 目录里堆满「主策略 / 增强组件 / 开关」，界面里到处都是设置页；
- 官方**没有**「恢复到初始状态」的按钮（Electron 端只有一个隐藏 safe-mode，且它只清 bundle 层，
  不清配置、不删依赖）。

本项目把这件事做成一条命令，并且在动手**之前**先从已安装的版本里读出「原厂态」到底是什么。

## 官方原厂态是什么（实测得出，不是猜的）

`probe_factory_state.py` 直接解析已安装的 `app.asar`，从 `/lib/main.js` 里取出官方的
`PROFILE_TEMPLATES` / `PROFILE_PATCH_TEMPLATE` / `PROFILE_PNPM_WORKSPACE`：

| 文件 | 原厂内容 |
|---|---|
| `profiles/<name>/package.json` | `dependencies: {}`，`bundles: ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]` |
| `profiles/<name>/cordis.patch.yml` | 4 行注释 + 空数组 `[]` |
| `profiles/<name>/pnpm-workspace.yaml` | `packages: - .` + `nodeLinker: hoisted` + `autoInstallPeers: false` |

官方 `initProfile()` 首次初始化时**只写这三个文件** —— profile 里没有 `node_modules`、
没有 `pnpm-lock.yaml`、没有 `.plugin-manager`。

另外：**桌面 profile 没有自己的模板**，是 Electron 端按 **web 模板**初始化的
（`app.asar` 里 `createPluginProfile → initProfile(WEB_PROFILE.bundles)`）。

## 快速开始

```bash
# 1) 先看计划，不动任何东西
python dsh_factory_restore.py --dry-run

# 2) 真还原：先整份备份 → 退出桌面程序 → 写回原厂态 → 重启 → 复核
python dsh_factory_restore.py --yes
```

参数：

```
--dsh-home PATH         DSH 主目录（默认 $DSH_HOME 或 ~/.dsh）
--profile NAME          要还原的 profile（默认 desktop）
--backup-dir PATH       备份目录（默认 ~/dsh-factory-restore-backups/<时间戳>）
--app PATH              桌面程序可执行文件（默认自动定位）
--asar PATH             app.asar 路径，用于探测原厂态
--factory-source MODE   auto | probe | builtin（默认 auto：能探测就探测）
--keep-notes            notes/ 留在原地（默认移进备份）
--keep-speech-to-text   speech-to-text/ 留在原地（语音模型，可能几百 MB）
--no-stop               不退出桌面程序（不推荐：它可能把配置写回）
--no-relaunch           还原后不重启桌面程序
--yes                   跳过确认
--json                  额外打印机器可读摘要
```

## 它做了什么 / 不做什么

**做：**

1. 备份 ① 整个 profile（含第三方插件与全部配置）② 用户数据（凭证、会话、工作区状态、技能）
   ③ 插件数据（`notes/`、`speech-to-text/`）
2. 退出桌面程序（优雅关闭 → 不成功则强制结束进程树）
3. 写回原厂三件套；删掉 `node_modules`、`pnpm-lock.yaml`、`.plugin-manager`、
   `.dsh-module-fallback`，以及剩余的 profile 根入口列表 `cordis.yml`
   （它是「一段说明 + 空数组」，缺失时启动会自动重建 —— 真正的组合树是按
   `bundles → cordis.patch.yml` 现场拼的，不在这个文件里）
4. 重启桌面程序
5. 复核：依赖是否为空、bundle 层是否只剩两个官方包、patch 层是否为空、插件残留是否清干净

**不做：** 不删 `.credentials.yaml`、`sessions/`、`storages/`、`skills/`（只备份，不动它们）；
不联网；不上传任何东西。

## 验证

脚本最后会打印「复核」段。想手工再确认一遍：

```bash
# 桌面版自带 CLI（Windows 示例路径）—— 期望输出：没有任何依赖
"$LOCALAPPDATA/Programs/DeepSeek Harness/resources/runtime/cli/bin/dsh.cmd" \
    plugin --profile desktop list --depth=0
```

```bash
# 对照已安装版本的原厂态定义
python probe_factory_state.py
```

## 回滚

```bash
cp -r <备份目录>/profile-before/.  ~/.dsh/profiles/desktop/
```

## 注意事项

- **必须先退出桌面程序**：它持有 profile 写锁，运行中改配置可能被写回。脚本默认帮你退出
  （也默认帮你重启）。
- DSH 是 **developer preview**，官方明说会有兼容性破坏。所以脚本默认先探测**已安装版本**的原厂态，
  探测不到才退回内置常量，并在输出里写清楚来源。
- 语音模型目录（`speech-to-text/`）默认**移进备份而不是删除**；要留在原地用 `--keep-speech-to-text`。
- 本工具只针对 **DSH 桌面版**（Electron 管理的 profile）。CLI 的 `web` / `headless` / `sdk` 等
  profile 可以直接用 `dsh --from-default-profile <name>` 重建，不需要这个脚本。

## 实测环境

- Windows 11 · DeepSeek Harness 桌面版（Electron 包 `@deepseek-ai/dsh-desktop 0.2.0-rc.2`）
- 还原脚本用一套模拟的 `$DSH_HOME` 做过端到端测试（含幂等重跑）
- 相关实测笔记见 [`docs/environment-notes.md`](docs/environment-notes.md)，
  一次真实还原的审计清单见 [`docs/restore-audit-example.md`](docs/restore-audit-example.md)

## 许可

MIT © 北面艺术家 / North Artist

---

### English (short)

`dsh-factory-restore` reverts a **DeepSeek Harness desktop** profile to the state the
installer creates: `bundles = [@deepseek-ai/dsh-base, @deepseek-ai/dsh-web-app]`, an empty
`cordis.patch.yml`, and no third-party dependencies.

It does not trust a hard-coded snapshot of "factory": `probe_factory_state.py` reads the
official definition straight out of the installed `app.asar`, and the restore script warns
if the discovered state differs. Then it backs everything up, quits the desktop app,
rewrites the three profile files, deletes plugin leftovers, relaunches and re-verifies.

Python 3.10+, standard library only. MIT.

# 一次真实还原的审计清单（示例）

下面是本工具第一次真实使用时的记录（已脱敏；用于说明「还原」到底动了什么）。
环境：Windows 11 + DeepSeek Harness 桌面版（`@deepseek-ai/dsh-desktop 0.2.0-rc.2`）。

## 还原前的 profile 状态

`profiles/desktop/package.json`：

- `dependencies`：5 个社区插件
  - `dsh-mnemon`（分层记忆：3 类记忆 × 5 策略 × 9 个第三方 provider，每个组件各带一个设置页）
  - `dsh-notes-plugin`（会话感知笔记）
  - `dsh-reasoning-effort`（推理档位控件）
  - `dsh-ui-harmonizer`（界面统一）
  - `dshmarket`（可视化插件市场）
- `bundles`：上述 5 个 + 4 个官方 experimental 层
  （定时任务 `schedule`、自动审查 `auto-review`、多智能体 `agent-team-profile`、语音输入 `voice-input`）

`cordis.patch.yml`：11 组用户层配置，包括

- 默认模型与推理档位（`agent-default-model`）
- 自定义模型表（`llm-deepseek-account`：上下文窗口、模态、prompt 更新策略…）
- 界面项（主题、聊天视图、设置页完成状态）
- 记忆插件的完整开关组（召回策略、embedding 开关、写入模式、记忆 tab…）

其余目录：`node_modules/`（21 MB）、`pnpm-lock.yaml`、`.plugin-manager/`、生成的 `cordis.yml`、
`notes/`、`speech-to-text/`（231 MB 语音模型）。

## 还原动作

| 动作 | 对象 |
|---|---|
| 备份（完整保留） | `profiles/desktop/` 整份、`skills/`、`sessions/`、`storages/workspace.json`、`.credentials.yaml` |
| 移进备份 | `notes/`、`speech-to-text/`（231 MB，未删除） |
| 重写 | `package.json`、`cordis.patch.yml`、`pnpm-workspace.yaml` → 原厂内容 |
| 删除 | `node_modules/`、`pnpm-lock.yaml`、`.plugin-manager/`、根入口列表 `cordis.yml`（启动会重建） |
| 退出/重启 | 桌面程序退出（优雅关闭未生效 → 强制结束进程树）→ 还原后重启 |

## 复核结果

- `dsh plugin --profile desktop list --depth=0` → **无任何依赖**
- `package.json` → `dependencies: {}`，`bundles` 只剩 `@deepseek-ai/dsh-base` + `@deepseek-ai/dsh-web-app`
- `cordis.patch.yml` → 只剩 4 行注释 + `[]`（patch 条目 0 行）
- `cordis.yml`（重启后自动重建）→ 回到出厂的那份「空入口列表」
- 全库 grep：`mnemon` / `notes` / `market` / `harmonizer` / `reasoning-effort` / `experimental` 全部为 0

## 代价（要有心理准备）

还原到出厂态之后，界面上的个性化（主题、模型档位、完成过的欢迎向导）会回到默认，
需要重新设置一遍 —— 这正是「刚安装的样子」的含义。

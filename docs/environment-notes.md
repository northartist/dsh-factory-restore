# DSH 实测笔记（Windows）

写脚本时踩到的、以及官方文档没直说的事，记在这里。

## 目录结构（DSH 主目录 = `$DSH_HOME`，默认 `~/.dsh`）

```
~/.dsh/
├─ .credentials.yaml        登录凭证（永远不要上传/提交）
├─ .anonymous-user-id
├─ profiles/
│  └─ desktop/              ← 桌面版使用的 profile
│     ├─ package.json       profile 清单：dependencies + dsh.profile.bundles（唯一挂载源）
│     ├─ cordis.patch.yml   用户补丁层（hot-reload）
│     ├─ pnpm-workspace.yaml
│     ├─ cordis.yml         profile 根入口列表（说明注释 + `[]`）；缺失时启动会重建，
│     │                     真正的组合树是按 bundles → cordis.patch.yml 现场拼的，不在这里
│     ├─ node_modules/      第三方插件（原厂态不存在）
│     └─ .plugin-manager/   插件管理器的操作日志（原厂态不存在）
├─ skills/                  agent 技能（用户侧）
├─ sessions/                会话记录 jsonl（用户侧）
├─ storages/                工作区状态（用户侧）
├─ notes/                   某些插件（如笔记类）的数据 —— 随插件下架
└─ speech-to-text/          语音输入插件的模型（可能几百 MB）—— 随插件下架
```

`profiles/desktop/package.json` 里的 `dsh.profile.bundles` 是**有序的层列表**：
先 `@deepseek-ai/dsh-base`，再 app bundle，然后第三方 bundle，最后才是 `cordis.patch.yml` 用户层。
`bundles` 里没有的包即使装在 `node_modules` 里也不会被加载。

## 桌面版自带的 CLI

路径（Windows）：`%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd`

```bash
# 能用的：
dsh.cmd --help
dsh.cmd plugin --profile desktop list --depth=0      # 转发 pnpm（只读，实测可用）
dsh.cmd plugin --profile desktop add <pkg|path>      # 装插件
dsh.cmd plugin --profile desktop remove <pkg>        # 卸插件

# 不能用的（会被明确拒绝）：
dsh.cmd --profile desktop --dump-config
#   → error: profile "desktop" is managed exclusively by the Electron application
```

也就是说：**desktop profile 只有插件管理能过 CLI，boot / 配置导出必须走桌面程序。**

## Windows 上写这类脚本的坑

1. **`taskkill` 在 git-bash 里不能写 `//IM`**：MSYS 会把 `/IM` 当路径转换，写 `//IM` 又会被
   `taskkill` 判为非法选项。用 PowerShell 最稳：
   ```bash
   powershell -NoProfile -Command 'Get-Process -Name "DeepSeek Harness" | Stop-Process -Force'
   # 或者在 bash 里临时关掉参数转换：
   MSYS2_ARG_CONV_EXCL='*' taskkill /F /T /IM "DeepSeek Harness.exe"
   ```
2. **`cmd //c start "" "xxx.exe"` 在 MSYS 下会掉进交互 shell**（`start` 没执行，命令挂住）。
   启动 GUI 一律用：
   ```bash
   powershell -NoProfile -Command 'Start-Process -FilePath "C:\...\DeepSeek Harness.exe"'
   # Python 里：os.startfile(path)
   ```
3. **别用 `subprocess.run(..., text=True)` 读 `tasklist` / `taskkill` 的输出**：中文 Windows 是
   GBK，会抛 `UnicodeDecodeError` 把读取线程打挂（脚本看起来"没报错但结果不对"）。
   按字节处理最稳：
   ```python
   raw = subprocess.run(["tasklist", "/FI", "IMAGENAME eq DeepSeek Harness.exe"],
                        capture_output=True).stdout or b""
   running = b"deepseek harness.exe" in raw.lower()
   ```
4. **关不掉就用进程树**：Electron 主进程不在时其它 renderer 可能还在，`taskkill /F /T /IM` 更干净。

## 别把 secret 带出去

- `~/.dsh/.credentials.yaml` 是登录凭证；
- profile 的 `cordis.patch.yml` 里可能被插件塞进 API key（例如嵌入模型 endpoint 的 `apiKey`）。

要分享 profile 内容之前先扫一遍这几个文件。

## 怎么看「插件装上之后到底挂载了什么」（组合树）

桌面 profile 被 Electron 独占，`--dump-config` 会被拒绝。想看组合树就造一个临时 profile：

```bash
dsh probe --from-default-profile web --dump-config > /tmp/a.yml   # 新建 probe 并打印组合树
dsh plugin --profile probe add dsh-mnemon                        # 装插件
dsh probe --dump-config > /tmp/b.yml                             # 再看一遍
diff <(grep -o 'id: .*' /tmp/a.yml) <(grep -o 'id: .*' /tmp/b.yml)
rm -rf ~/.dsh/profiles/probe                                     # 用完删掉
```

验证「用户补丁层能不能覆盖插件的数组配置」也是用这个办法（实测：能，数组是整体替换）：
把 profile 的 `cordis.patch.yml` 复制进临时 profile，再 dump 一次比对。

## `git push` 静默挂死（Windows + Git Credential Manager）

症状：`git ls-remote` 秒回，但 `git push` 一直没输出、也不报错，能挂几分钟。
原因通常是**凭据链里有会弹界面的 helper**（Git for Windows 系统级挂的
Git Credential Manager），在非交互 shell 里它等不到人就一直等；如果 store 里
第一条 token 又恰好失效，就更容易走到它。

绕过（一次性凭据，不落盘）：

```bash
export GITHUB_TOKEN=$(...取有效 token...)
git -c credential.helper= \
    -c credential.helper='!f() { echo username=<你的用户名>; echo password=$GITHUB_TOKEN; }; f' \
    push -u origin main
```

或者修好凭据文件：`~/.git-credentials` 里 `store` helper **只用第一条匹配行**，
把有效 token 放到第一行（失效的在前面会让所有 push 认证失败）。


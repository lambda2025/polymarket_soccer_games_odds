# Polymarket 英超 / 西甲 / 德甲实时赔率 CSV 采集器

脚本先从 Gamma API 的体育元数据读取英超、西甲、德甲对应的赛事系列，再读取各联赛未关闭的比赛市场；足球胜平负通常由三个二元 Yes/No 市场组成，脚本只订阅三个市场各自的 Yes 结果。行情通过 Polymarket CLOB 公共 WebSocket 获取，并将每次变动追加到 CSV。无需 API Key。

本项目已适配 Windows（Python 3.9 或更高版本，开发时使用 Python 3.13）。下文的路径以本项目目录 `D:\path\to\project_poly` 为例，请按实际位置替换。

## 安装（Windows）

在项目目录中打开 PowerShell 或命令提示符：

```powershell
cd D:\path\to\project_poly
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

依赖只有 `requests` 和 `websocket-client`。**本机已经完成上述安装**（`.venv` 已存在），可直接跳到下面的运行步骤；换电脑或删除 `.venv` 后才需要重新执行。

后续命令都直接调用 `.venv` 里的解释器，因此不需要先激活虚拟环境。若确实要激活，PowerShell 用 `.\.venv\Scripts\Activate.ps1`（若提示禁止运行脚本，先在当前窗口执行 `Set-ExecutionPolicy -Scope Process Bypass`），命令提示符用 `.venv\Scripts\activate.bat`。

## 运行（Windows）

先检查市场是否能被识别：

```powershell
cd D:\path\to\project_poly
.\.venv\Scripts\python.exe polymarket_epl_odds.py --discovery-only
```

能列出比赛和结果后，启动 CSV 采集：

```powershell
.\.venv\Scripts\python.exe polymarket_epl_odds.py --output polymarket_odds.csv
```

采集会持续运行，按 `Ctrl+C` 停止并保存 CSV。

每次启动都会新建一个 CSV，文件名自动附加本地生成时间，例如 `polymarket_odds_20261019_233436.csv`。`--output` 指定的是时间戳前的文件名主体；重复启动时间戳相同则会自动加序号。相对路径的 `--output` 会以脚本所在目录为基准，所以在任何目录下启动，CSV 都会落在项目目录里。

### 一键启动（Windows）

双击项目目录中的 `run_polymarket_odds.bat` 即可启动同一采集命令。窗口会一直采集；按 `Ctrl+C` 停止，CSV 保存在项目目录中，脚本结束后窗口会等待按键再关闭。

按 `Ctrl+C` 时，cmd 可能额外询问“终止批处理操作吗(Y/N)?”。此时 CSV 已经保存完毕：按 `Y` 直接关闭窗口，按 `N` 则会回到“Collector finished.”之后再关闭。

> `.bat` 必须保持纯 ASCII 内容：cmd.exe 按 ANSI 字节解析批处理文件，UTF-8 中文会把命令行撕碎。中文提示和日志由 Python 输出，控制台里显示正常。

### macOS（原来的启动器）

`run_polymarket_odds.command` 仍保留给 macOS 使用，并已改为自动定位自身所在目录（不再写死 `/Users/...` 这类绝对路径）。Windows 上请用 `.bat`。

## 网络与代理（重要）

脚本会自动读取 Windows 系统代理（“设置 → 网络和 Internet → 代理”里配置的地址，Clash、V2Ray 等工具通常就是它），并把该代理同时用于 Gamma API 和 CLOB WebSocket；读不到时退回环境变量 `HTTPS_PROXY` / `HTTP_PROXY`，再退回直连。

在本机实测：

- `https://gamma-api.polymarket.com` 直连可用；
- `wss://ws-subscriptions-clob.polymarket.com` 直连会超时，**必须走本地代理**（当前系统代理为 `http://127.0.0.1:7897`）。

因此关闭代理软件后，脚本可能只能发现市场、收不到行情。相关开关：

```powershell
# 手动指定代理（覆盖系统设置）
.\.venv\Scripts\python.exe polymarket_epl_odds.py --proxy http://127.0.0.1:7897

# 强制直连
.\.venv\Scripts\python.exe polymarket_epl_odds.py --no-proxy
```

WebSocket 连接失败时，程序会自动在“代理 / 直连”两种方式之间轮换重试。

## 常用参数

默认仅在行情更新时写入 CSV。如需每 10 秒记录所有已发现结果（即便价格没变）：

```powershell
.\.venv\Scripts\python.exe polymarket_epl_odds.py --snapshot-seconds 10
```

运行时默认每 60 秒输出一次状态日志，包含 WebSocket 连接状态、最近收到消息的时间、消息数量、新增 CSV 行数和输出文件路径。可用 `--status-seconds 30` 将状态日志间隔改为 30 秒。若连接保持但长时间没有收到消息，会输出警告。

所有参数：

| 参数 | 说明 |
| --- | --- |
| `--output` | 输出 CSV 主体名，默认 `polymarket_odds.csv` |
| `--snapshot-seconds` | 固定间隔写入全部赔率，0 表示只在变动时写入 |
| `--status-seconds` | 状态日志间隔，默认 60 |
| `--discovery-only` | 只列出可识别市场，不连 WebSocket |
| `--proxy` | 指定 HTTP(S) 代理 |
| `--no-proxy` | 忽略系统代理，全部直连 |

当前脚本启动时扫描一次市场；如赛事期间出现新市场，重新启动脚本即可订阅新 token。

## CSV 说明

每个联赛采集从启动时起未来 20 天内开球、且至少有可识别 1X2 市场的比赛。筛选优先使用体育市场的 `gameStartTime`，并以赛事级 `startTime` / `eventDate` 作为后备；启动日志会显示各联赛纳入的比赛数。CSV 的“比赛时间”列按运行电脑的本地时区显示；“主客双方”列按 Polymarket 足球赛事标题顺序列出主队和客队。完全相同的行会去重，并按德甲、英超、西甲的顺序以及比赛时间升序排列。

CSV 每行汇总一场比赛，表头为 `比赛时间, 主客双方, 联赛, 联赛轮次, 主队胜率, 客队胜率, 平局概率`。胜率和概率以 0–1 小数表示，使用买卖价中间值；缺少买卖两侧时使用最新成交价。仅当主胜、客胜和平局三项都有概率时才写入 CSV，因此每行均包含完整的 1X2 概率。完全重复的行会按这七列去重。文件使用带 BOM 的 UTF-8 编码，可直接用 Excel 打开；若 Excel 正打开着该文件，写入会暂时失败并在控制台提示，关闭后会自动恢复。

来源：Polymarket Gamma API 与 CLOB Market WebSocket 官方文档。

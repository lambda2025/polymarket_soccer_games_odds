# Polymarket 英超 / 西甲 / 德甲实时赔率 CSV 采集器

脚本先从 Gamma API 的体育元数据读取英超、西甲、德甲对应的赛事系列，再读取各联赛未关闭的比赛市场；足球胜平负通常由三个二元 Yes/No 市场组成，脚本只订阅三个市场各自的 Yes 结果。行情通过 Polymarket CLOB 公共 WebSocket 获取，并将每次变动追加到 CSV。无需 API Key。

## 安装与运行

先进入项目目录，下面生成的虚拟环境和 CSV 文件都会保存在该目录中：

```bash
cd macos
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

每次启动采集都会新建一个 CSV，文件名自动附加本地生成时间，例如 `polymarket_odds_20261012_213000.csv`。`--output` 指定的是时间戳前的文件名主体；重复启动时间戳相同则会自动加序号。

市场检查步骤：

请在项目目录重新运行市场检查：

```bash
cd macos
source .venv/bin/activate
python polymarket_epl_odds.py --discovery-only
```

如果能列出比赛和结果，再启动 CSV 采集：

```bash
python polymarket_epl_odds.py --output polymarket_odds.csv
```

采集命令会生成带本地时间戳的 CSV 文件。

### macOS 一键启动

双击项目目录中的 `run_polymarket_odds.command` 即可启动同一采集命令。采集会持续运行；按 Control-C 停止。CSV 仍保存在项目目录中，终端窗口会等待按回车后关闭。

默认仅在行情更新时写入 CSV。如需每 10 秒记录所有已发现结果（即便价格没变）：

```bash
cd macos
source .venv/bin/activate
python polymarket_epl_odds.py --snapshot-seconds 10
```

运行时默认每 60 秒输出一次状态日志，包含 WebSocket 连接状态、最近收到消息的时间、消息数量、新增 CSV 行数和输出文件路径。可用 `--status-seconds 30` 将状态日志间隔改为 30 秒。若连接保持但长时间没有收到消息，会输出警告。

当前脚本启动时扫描一次市场；如赛事期间出现新市场，重新启动脚本即可订阅新 token。

每个联赛采集从启动时起未来 20 天内开球、且至少有可识别 1X2 市场的比赛。筛选优先使用体育市场的 `gameStartTime`，并以赛事级 `startTime` / `eventDate` 作为后备；启动日志会显示各联赛纳入的比赛数。CSV 的“比赛时间”列按运行电脑的本地时区显示；“主客双方”列按 Polymarket 足球赛事标题顺序列出主队和客队。完全相同的行会去重，并按德甲、英超、西甲的顺序以及比赛时间升序排列。

CSV 每行汇总一场比赛，表头为 `比赛时间, 主客双方, 联赛, 联赛轮次, 主队胜率, 客队胜率, 平局概率`。胜率和概率以 0–1 小数表示，使用买卖价中间值；缺少买卖两侧时使用最新成交价。仅当主胜、客胜和平局三项都有概率时才写入 CSV，因此每行均包含完整的 1X2 概率。完全重复的行会按这七列去重。

来源：Polymarket Gamma API 与 CLOB Market WebSocket 官方文档。

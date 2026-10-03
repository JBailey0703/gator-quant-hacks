# Webull 量化回测示例 · 参赛者使用指南

> 面向 **Gator Quant Hacks 2026 · Systematic Trading 赛道** 的参赛同学。
>
> 这份文档假设你**会写一点 Python**，但**不需要**任何量化金融背景。跟着一步步走，
> 大约 15 分钟就能跑出你的第一个回测结果，还会自动生成一份好看的交互式图表报告。

---

## 1. 这个项目能帮你做什么

比赛要求你用历史数据构建并优化一个交易策略，评委会看你的**夏普比率、最大回撤、换手率**等指标。

这个项目已经帮你把最麻烦的「数据管道」和「回测引擎」都接好了，你**只需要专注写策略逻辑**：

- **拿数据**：内置 `WebullData`，直接从 Webull OpenAPI 拉美股/ETF 的历史 K 线，不用自己写爬虫或处理 CSV。
- **跑回测**：基于成熟的开源回测框架 [backtrader](https://github.com/mementum/backtrader)，自动帮你算收益、最大回撤、**夏普比率**、胜率、逐笔盈亏。
- **看图表**：每次回测自动生成一份交互式 HTML 报告（K 线 + 指标 + 买卖点 + 资金曲线 + KPI 卡片）。
- **两个现成策略示例**：一个双均线策略、一个多标的动量轮动策略，照着改就能上手。
- **（进阶）模拟实盘**：可以让策略像真实盘一样逐根 K 线运行，甚至接到真实交易接口下单。

> 一句话：**你写策略，框架负责其余部分。**
>
> 注意：比赛评分**只看回测结果**，模拟盘/实盘不计分。所以先把回测跑通就够拿分了，
> 模拟实盘和真实下单是给有余力的同学玩的进阶内容。

---

## 2. 项目结构速览

整个项目分成两块：可复用的 **`webull_bt` 库**（数据/交易/工具，你一般不用改），
和 **`examples/` 示例**（入口脚本 + 策略，这才是你要动手的地方）。

```
backtrader-example/
├── webull_bt/                  # 可复用库（一般不用改）
│   ├── feed.py                 #   核心数据 Feed：WebullData
│   ├── broker.py               #   真实交易 Broker：WebullBroker（进阶）
│   ├── timeutils.py            #   时区 / 交易时段工具
│   ├── visualize.py            #   Plotly 图表报告生成
│   └── logging_utils.py        #   统一日志配置
├── examples/                   # 示例（你主要在这里写代码）
│   ├── backtest/
│   │   ├── main.py             #   【回测入口】← 先从这里开始
│   │   └── .env                #   回测配置（填你的凭证）
│   ├── live/
│   │   ├── main.py             #   模拟实盘 / 真实交易入口（进阶）
│   │   └── .env                #   实盘配置
│   └── strategies/             #   策略文件夹 ← 你写的策略放这里
│       ├── dual_ma.py          #     示例①：双均线（单/多标的）
│       └── portfolio.py        #     示例②：多标的动量轮动
├── docs/                       # 文档（含本指南）
└── pyproject.toml              # 依赖声明
```

---

## 3. 环境准备

### 3.1 你需要什么

- **Python 3.11 或更高版本**（在终端运行 `python3 --version` 确认）
- 一套 **Webull OpenAPI 凭证**（`app_key` + `app_secret`），比赛主办方或 Webull 现场支持会发给你
- 一个终端（macOS/Linux 用 Terminal，Windows 用 PowerShell 或 Git Bash）

### 3.2 安装依赖

项目用 [`uv`](https://docs.astral.sh/uv/)（一个又快又省心的 Python 包管理器）管理环境。**推荐用 uv**：

```bash
# 进入项目目录
cd backtrader-example

# 一条命令搞定虚拟环境 + 依赖安装
uv sync
```

如果你更习惯传统的 `pip`：

```bash
cd backtrader-example
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

依赖都写在 `pyproject.toml` 里：`backtrader`（回测引擎）、`webull-openapi-python-sdk`（数据/交易接口）、
`python-dotenv`（读取配置），以及生成图表报告用的 `plotly` 和 `pandas`。

---

## 4. 填入你的凭证

项目把「回测」和「实盘」分成了两个独立入口，各自读自己目录下的 `.env` 配置文件。
**先跑回测就够了，只需要配 `examples/backtest/.env`。**

打开 `examples/backtest/.env`，把凭证换成你自己的：

```dotenv
WEBULL_APP_KEY=你的_app_key
WEBULL_APP_SECRET=你的_app_secret
WEBULL_API_ENDPOINT=api.webull.com
WEBULL_REGION_ID=us
```

> ⚠️ **凭证是敏感信息，别硬编码进代码，别提交到 Git。** `.env` 文件已经在 `.gitignore` 里，
> 提交代码前请再确认一遍。访问美股/ETF 行情需要有效的行情订阅权限，否则接口可能返回 403。

---

## 5. 跑出你的第一个回测

配好凭证后，从项目根目录运行回测入口：

```bash
uv run python examples/backtest/main.py
# 或（已激活虚拟环境时）
python examples/backtest/main.py
```

跑完你会在终端看到一份结果摘要，长这样：

```
============================================================
[Backtest] Result Summary
============================================================
Starting cash    : 100000.00      # 初始资金
Final cash       : 103250.00      # 结束资金
Net P&L          : 3250.00 (3.25%) # 净盈亏 / 收益率
Max drawdown     : 4.10% (4100.00) # 最大回撤
Sharpe ratio     : 1.2500 (annualized)  # 夏普比率（年化）★ 比赛重点指标
Total trades     : 12 (won=7, lost=5, win rate=58.33%)  # 交易次数 / 胜率
Trades net P&L   : 3250.00
============================================================
```

后面还会打印**逐笔交易明细**（每笔的开仓/平仓价、盈亏、持仓时长）。

同时，项目会自动在 `examples/backtest/` 目录下生成一份 **`backtest_report.html`** 交互式图表报告
——用浏览器打开，能看到 K 线图、均线叠加、买卖点标记、资金曲线和回撤，以及顶部的 KPI 卡片。

🎉 恭喜，你已经完成一次完整回测了。接下来就是改配置、改策略去优化这些指标。

---

## 6. 改配置调整回测（不用写代码）

`examples/backtest/.env` 里几乎所有常用的东西都能改。改完直接重跑上面的命令即可。

| 配置项 | 作用 | 常用取值 |
| --- | --- | --- |
| `WEBULL_SYMBOLS` | 交易哪些标的（逗号分隔，一个或多个） | `AAPL` 或 `AAPL,MSFT,GOOG` |
| `WEBULL_CATEGORY` | 证券类型 | `US_STOCK`（美股） |
| `WEBULL_TIMESPAN` | K 线粒度 | `D`=日线、`M1`=1分钟、`M5`=5分钟… |
| `WEBULL_COUNT` | 拉多少根 K 线 | 最大 1200（`M1` 最大 1650） |
| `WEBULL_FROMDATE` / `WEBULL_TODATE` | 回测时间范围（可选） | ISO 8601，建议带时区 |
| `WEBULL_STRATEGY` | 用哪个策略（填**文件名**，不含 `.py`） | `dual_ma`（默认）或 `portfolio` |
| `WEBULL_STRATEGY_PARAMS` | 覆盖策略参数（不用改代码） | `short_period=10,long_period=30` |
| `WEBULL_VISUALIZE` | 是否生成 HTML 图表报告 | `true`（默认）/ `false` |
| `WEBULL_VISUALIZE_OUTPUT` | 报告输出文件名 | `backtest_report.html`（默认） |
| `WEBULL_DISPLAY_TZ` | 日志/图表显示时区 | `America/New_York`（默认，美东） |
| `LOG_LEVEL` | 日志详细程度 | `INFO`（默认）、`DEBUG`（看每根 bar） |

关于 K 线粒度：`WEBULL_TIMESPAN` 支持 `M1/M5/M15/M30/M60/M120/M240/D/W/M/Y`。
想做**分钟级回测**就用 `M1`，想快速验证策略思路就用 `D`（日线，数据量小、跑得快）。

时间范围示例（美东时间某个交易日的盘中）：

```dotenv
WEBULL_TIMESPAN=M1
WEBULL_FROMDATE=2026-09-15T09:30:00-04:00
WEBULL_TODATE=2026-09-15T16:00:00-04:00
```

不填时间范围时，默认按 `WEBULL_COUNT` 拉「最近 N 根」K 线。

---

## 7. 两个示例策略

策略文件都放在 `examples/strategies/` 里。`WEBULL_STRATEGY` 填的就是**策略文件名**（不含 `.py`）。
每个策略文件末尾都有一行 `STRATEGY_CLASS = ...`，入口就是靠它找到你的策略类的——
你自己新增策略时照抄这行即可。

### 7.1 双均线策略 `dual_ma.py`（默认）

最经典的入门策略，逻辑一句话说清（对 `WEBULL_SYMBOLS` 里**每个标的各自独立**运行）：

- **金叉买入**：短期均线上穿长期均线时，如果空仓就买入。
- **死叉平仓**：短期均线下穿长期均线时，如果持仓就清仓。

两个可调参数：`short_period`（默认 5，短期均线周期）、`long_period`（默认 20，长期均线周期）。

在 `examples/backtest/.env` 里：

```dotenv
WEBULL_STRATEGY=dual_ma
WEBULL_SYMBOLS=AAPL,TSLA
# 可选：调参数，不用改代码
WEBULL_STRATEGY_PARAMS=short_period=10,long_period=30
```

### 7.2 多标的动量轮动策略 `portfolio.py`

同时管理一篮子股票，演示「横截面动量轮动」这种真正需要多标的的玩法：

1. 每隔 `rebalance_days` 根 K 线做一次再平衡（不是每根都调仓）。
2. 算每只标的过去 `lookback` 根 K 线的收益率，作为「动量分数」。
3. 按分数排序，只保留分数为正的，取前 `top_n` 只。
4. 把资金等权分配到选中的标的（单只不超过 `max_weight`），没选中的自动清仓。

在 `examples/backtest/.env` 里：

```dotenv
WEBULL_STRATEGY=portfolio
WEBULL_SYMBOLS=AAPL,MSFT,GOOG,AMZN
# 可选：调参数
WEBULL_STRATEGY_PARAMS=lookback=15,rebalance_days=3,top_n=2,max_weight=0.5
```

可调参数：`lookback`（动量窗口）、`rebalance_days`（再平衡间隔）、`top_n`（最多持有几只）、
`max_weight`（单只权重上限）。跑完除了交易明细，还会打印每次再平衡的排名和选中结果。

---

## 8. 写你自己的策略

这才是比赛真正的重点。步骤很简单：

1. 在 `examples/strategies/` 下新建一个文件，比如 `my_strategy.py`。
2. 写一个继承 `bt.Strategy` 的类，实现 `next()`（每根新 K 线调用一次）。
3. **文件末尾加一行** `STRATEGY_CLASS = 你的策略类`。
4. 在 `.env` 里把 `WEBULL_STRATEGY` 设成文件名 `my_strategy`，重跑即可。

最小骨架 `examples/strategies/my_strategy.py`：

```python
import backtrader as bt

class MyStrategy(bt.Strategy):
    params = dict(period=14)   # 你的可调参数（可被 WEBULL_STRATEGY_PARAMS 覆盖）

    def __init__(self):
        # 在这里定义指标，例如一条 RSI
        self.rsi = bt.ind.RSI(period=self.p.period)

    def next(self):
        # 每根 K 线调用一次；self.data.close[0] 是当前收盘价
        if not self.position:                 # 当前空仓
            if self.rsi < 30:                 # 超卖 -> 买入
                self.buy()
        elif self.rsi > 70:                   # 超买 -> 平仓
            self.close()

# 入口靠这一行找到你的策略类，别忘了写！
STRATEGY_CLASS = MyStrategy
```

然后：

```dotenv
WEBULL_STRATEGY=my_strategy
```

常用的 backtrader API：

- `self.data.close[0]` / `open[0]` / `high[0]` / `low[0]` / `volume[0]`：当前 bar 的行情，`[-1]` 是上一根。
- `self.buy()` / `self.sell()` / `self.close()`：下单 / 平仓。
- `self.position`：当前持仓（`if not self.position:` 判断是否空仓）。
- `bt.ind.SMA` / `RSI` / `CrossOver` …：内置的一大堆技术指标。
- backtrader 官方文档：<https://www.backtrader.com/docu/>

> 提示：本项目的 `WebullData` 还多提供了一条 `trading_session` 线，标记每根 bar 属于
> 盘前/盘中/盘后（`PRE`/`RTH`/`ATH`/`OVN`）。需要时用 `webull_bt.timeutils.decode_trading_session()`
> 解码，参考 `dual_ma.py` 里的用法。日线及以上没有这个信息。
>
> 想让你的自定义指标出现在图表报告里，可以参考 `dual_ma.py`：把指标存进 `self.inds`
> 字典，报告生成器会自动把均线类指标叠加到 K 线上。

---

## 9. 在自己的脚本里直接用 Feed（不依赖 main.py）

如果你想完全自己搭回测流程，`WebullData` 可以从 `webull_bt` 包直接导入。凭证和客户端由你构建，
Feed 只负责取数据：

```python
import backtrader as bt
from webull.core.client import ApiClient
from webull.data.data_client import DataClient
from webull_bt import WebullData   # 从 webull_bt 包导入

# 1. 构建 DataClient（凭证建议从环境变量读取，别硬编码）
api_client = ApiClient(app_key, app_secret, "us")
api_client.add_endpoint("us", "api.webull.com")
data_client = DataClient(api_client)

# 2. 把 feed 加进 cerebro
cerebro = bt.Cerebro()
cerebro.adddata(
    WebullData(
        dataname="AAPL",
        data_client=data_client,   # 必填
        category="US_STOCK",
        timespan="D",
        count=200,
    )
)
cerebro.addstrategy(MyStrategy)
cerebro.broker.setcash(100000.0)
cerebro.run()
```

`WebullData` 的完整参数说明见 `docs/README.md`（「Feed 参数」一节）。

---

## 10. （进阶）模拟实盘与真实下单

> 比赛不看这部分成绩，属于有余力再玩的加分探索。**务必先在 Sandbox/测试环境验证**，别拿真钱试错。

- `examples/live/main.py` 是模拟实盘入口，用后台线程**轮询**历史 K 线接口，模拟行情逐根到达来驱动策略。
- 默认 `WEBULL_USE_BROKER=0`，只跑策略、**不下单**。
- 把 `examples/live/.env` 里的 `WEBULL_USE_BROKER` 改成 `1` 才会接真实交易接口下单，`WEBULL_ENV`
  可在 `prod` / `sandbox` 间切换凭证与账户。

运行：

```bash
python examples/live/main.py   # Ctrl+C 退出
```

⚠️ **风险提示**：`WEBULL_USE_BROKER=1` 后策略信号会**真实提交订单**。交易 API 需要单独申请权限。
细节与 Broker 的订单类型/状态映射请看 `docs/README.md` 的「交易 Broker」一节。

---

## 11. 常见问题

**Q：接口返回 403 / 401？**
A：多半是凭证不对或没有行情订阅权限。检查 `WEBULL_APP_KEY`/`WEBULL_APP_SECRET`
是否填对、`WEBULL_API_ENDPOINT` 是否正确，并确认你的账号有对应的美股行情权限。

**Q：`invalid WEBULL_STRATEGY=...` 报错？**
A：`WEBULL_STRATEGY` 填的是 `examples/strategies/` 下的**文件名**（不含 `.py`），
且那个文件末尾必须有 `STRATEGY_CLASS = 你的策略类` 这一行。

**Q：回测没有产生任何交易？**
A：可能是数据太少（`WEBULL_COUNT` 太小）指标还没「预热」，或策略信号在这段行情里没被触发。
先把 `WEBULL_COUNT` 调大、`LOG_LEVEL=DEBUG` 看看每根 bar 的情况。

**Q：不想生成 HTML 报告 / 报告生成失败影响回测吗？**
A：设 `WEBULL_VISUALIZE=false` 可跳过。就算报告生成失败也不会影响回测本身，摘要照常打印。

**Q：图表和日志里的时间是什么时区？**
A：默认美东时间（`America/New_York`），会自动处理夏令时。改 `WEBULL_DISPLAY_TZ` 可切换。

**Q：分钟级回测和日线回测怎么选？**
A：验证策略思路、迭代快用日线（`D`）；接近比赛要求的精细回测用分钟线（`M1`）。分钟数据量大，
第一次拉取会慢一些。

---

祝你在 Gator Quant Hacks 玩得开心，跑出漂亮的夏普比率 📈

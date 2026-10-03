# backtrader + Webull OpenAPI 数据 Feed 示例

基于 [Webull OpenAPI](https://developer.webull.com/apis/docs) 的行情接口，为
[backtrader](https://github.com/mementum/backtrader) 实现的历史 K 线数据 Feed。
设计思路参考 backtrader 官方的 `feeds/yahoo.py`（`YahooFinanceData`）：在
`start()` 中拉取在线数据，在 `_load()` 中逐根喂给策略。

## 目录结构

```
backtrader-example/
├── feed.py                # 核心 Feed：WebullData（含 K 线解析 WebullBar）
├── broker.py              # 交易 Broker：WebullBroker（真实下单）
├── dual_ma_strategy.py     # 示例双均线策略（支持单个或多个标的，各自独立交易）
├── portfolio_strategy.py   # 示例多标的动量轮动组合策略
├── logging_utils.py       # 统一日志配置
├── backtest/
│   ├── main.py         # 回测入口
│   └── .env            # 回测配置（仅行情相关，不含 sandbox/交易变量）
└── live/
    ├── main.py         # 模拟实盘/真实交易入口
    └── .env            # 实盘配置（含 sandbox 切换与交易 broker 变量）
```

回测和实盘是两个独立的运行入口，各自读取自己目录下的 `.env`：

- `backtest/main.py`：一次性历史回测，只需行情凭证，不涉及 sandbox 或交易账户。
- `live/main.py`：轮询模拟实盘数据，可选接入真实交易 broker（`WEBULL_USE_BROKER=1`），
  支持通过 `WEBULL_ENV` 切换 prod / sandbox 凭证。

`feed.py`、`broker.py`、`dual_ma_strategy.py`、`portfolio_strategy.py`、`logging_utils.py` 是两个入口共用的模块。

## 依赖安装

```bash
uv sync
# 或
pip install -e .
```

依赖为 `backtrader`、`webull-openapi-python-sdk` 与 `python-dotenv`（用于加载
`.env`），均已在 `pyproject.toml` 中声明。`main.py` 启动时通过 `load_dotenv()`
自动读取 `.env`。

## 配置凭证

在对应目录的 `.env` 中填入你的 Webull OpenAPI 凭证（申请见
[Individual Application Process](https://developer.webull.com/apis/docs/authentication/IndividualApplicationAPI.md)）：

`backtest/.env`（回测，只需行情凭证）：

```dotenv
WEBULL_APP_KEY=你的_app_key
WEBULL_APP_SECRET=你的_app_secret
WEBULL_API_ENDPOINT=api.webull.com

WEBULL_SYMBOLS=AAPL
WEBULL_CATEGORY=US_STOCK
WEBULL_TIMESPAN=M1
WEBULL_COUNT=200
# Optional exact time range (ISO 8601; timezone recommended)
WEBULL_FROMDATE=2026-09-15T09:30:00-04:00
WEBULL_TODATE=2026-09-15T16:00:00-04:00
```

`live/.env`（实盘，额外支持 sandbox 切换与交易账户配置，见下方章节）。

> 凭证是敏感信息，`backtest/.env` 与 `live/.env` 均已加入 `.gitignore`，请勿硬编码到源码或提交到仓库。
> 访问美股/ETF 行情需要有效的 OpenAPI 行情订阅，否则可能返回 403。

## 运行

回测：

```bash
uv run python backtest/main.py
# 或
python backtest/main.py
```

模拟实盘（默认不下单）：

```bash
uv run python live/main.py
# 或
python live/main.py
```

`WEBULL_FROMDATE` / `WEBULL_TODATE` 是可选的回测时间边界，支持 ISO 8601 到分钟级精度。
推荐带时区配置，例如 `2026-09-15T09:30:00-04:00`。时间范围会同时传给 Webull
API 的 `start_time/end_time`，并由 backtrader 再做本地边界过滤；使用 `M1` 即可进行分钟级回测。
未配置时保持原有行为，按 `WEBULL_COUNT` 拉取最近 N 根 K 线。

## 在自己的代码中使用 Feed

Feed 只负责数据，凭证与客户端构建由调用方负责：先创建一个 `DataClient`，
再把它传给 `WebullData`（`data_client` 为必填参数）。同一个 `DataClient`
可被多个 feed 复用，减少重复鉴权。

```python
import backtrader as bt
from webull.core.client import ApiClient
from webull.data.data_client import DataClient
from feed import WebullData

# 1. 构建 DataClient（凭证建议从环境变量读取，禁止硬编码）
api_client = ApiClient(app_key, app_secret, "us")
api_client.add_endpoint("us", "api.webull.com")  # 测试环境 api.sandbox.webull.com
data_client = DataClient(api_client)

# 2. 传给 feed
cerebro = bt.Cerebro()
cerebro.adddata(
    WebullData(
        dataname="AAPL",
        data_client=data_client,
        category="US_STOCK",  # 见 webull.data.common.category.Category
        timespan="D",         # 见 webull.data.common.timespan.Timespan
        count=200,
    )
)
```

## Feed 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `dataname` | — | 证券代码，如 `AAPL` |
| `data_client` | — | **必填**，已初始化的 `DataClient`（凭证与 endpoint 由调用方构建） |
| `category` | `US_STOCK` | 证券类型，取值见 `Category` 枚举名 |
| `timespan` | `M1` | K 线粒度：`M1/M5/M15/M30/M60/M120/M240/D/W/M/Y` |
| `count` | `200` | 拉取数量，最大 1200（`M1` 最大 1650） |
| `trading_sessions` | `None` | 交易时段，如 `PRE,RTH,ATH,OVN` |
| `fromdate` / `todate` | `None` | backtrader 内部的时间过滤边界 |

回测入口通过 `WEBULL_FROMDATE` / `WEBULL_TODATE` 配置时间范围，格式为 ISO 8601；

`timespan` 会自动映射为 backtrader 的 `timeframe` 与 `compression`，无需手动设置。

## 数据处理说明

- Webull 接口返回“最近 N 根”，通常按时间倒序；Feed 内部统一按时间**升序**排序后再喂入，
  符合 backtrader 的要求，不依赖接口返回顺序。
- 接口的 OHLCV 字段为字符串、时间为 UTC（形如 `2021-12-28T09:00:09.945+0000`），
  Feed 会解析为 `float` 与 UTC naive `datetime`。
- 日线及以上为前复权、分钟线为不复权（由 Webull 接口决定）。

## 组合标的策略（多标的动量轮动）

`portfolio_strategy.py` 提供 `PortfolioMomentumStrategy`，演示如何用 backtrader 交易一个
标的篮子并做跨标的资金分配/再平衡。与 `dual_ma_strategy.py`（每个标的独立跑双均线，互不
影响资金分配）不同，`PortfolioMomentumStrategy` 需要
往同一个 `Cerebro` 里 `adddata()` 多个 `WebullData` feed（每个标的一个）。

策略逻辑（横截面动量轮动）：

1. 每 `rebalance_days` 根 bar 触发一次再平衡（而非每根 bar 都判断）。
2. 计算每个标的过去 `lookback` 根 bar 的收益率，作为动量分数。
3. 按动量分数排序，只保留分数为正的标的，取排名前 `top_n` 个。
4. 用 `order_target_percent` 把资金等权（受 `max_weight` 上限约束）分配到选中标的；
   不再入选的标的自动清仓。backtrader 会据此自动计算加仓/减仓/清仓所需的订单。

在 `backtest/.env` 中启用（`WEBULL_STRATEGY` 直接填策略文件名，不含 `.py`）：

```dotenv
WEBULL_STRATEGY=portfolio_strategy
WEBULL_SYMBOLS=AAPL,MSFT,GOOG,AMZN
# 可选：覆盖策略参数
WEBULL_STRATEGY_PARAMS=lookback=15,rebalance_days=3,top_n=2,max_weight=0.5
```

```bash
python backtest/main.py
```

在自己的代码中使用：

```python
import backtrader as bt
from feed import WebullData
from portfolio_strategy import PortfolioMomentumStrategy

cerebro = bt.Cerebro()
for symbol in ["AAPL", "MSFT", "GOOG", "AMZN"]:
    cerebro.adddata(
        WebullData(dataname=symbol, data_client=data_client, timespan="D", count=200),
        name=symbol,
    )
cerebro.addstrategy(
    PortfolioMomentumStrategy,
    lookback=20,       # 动量计算窗口（bar 数）
    rebalance_days=5,  # 再平衡间隔（bar 数）
    top_n=3,           # 同时最多持有的标的数
    max_weight=0.35,   # 单标的最大权重上限
)
cerebro.broker.setcash(100000.0)
cerebro.run()
```

策略同样通过 `notify_trade()` 记录 `closed_trades`（逐笔盈亏，字段与双均线策略一致），
并额外记录 `rebalance_log`（每次再平衡时的排名与选中结果），`backtest/main.py` 结尾会
打印这两份明细。

## 模拟实盘（轮询驱动）

`WebullLiveData` 提供一个 **live feed**，用于模拟实盘运行。它不依赖 MQTT 流式推送，
而是后台线程按固定间隔**轮询**历史 K 线接口（`get_batch_history_bar`），发现有新收盘的
bar 就喂给策略，从而模拟实时行情逐 bar 到达。

在 `live/.env` 中配置：

```dotenv
WEBULL_SYMBOLS=AAPL
WEBULL_TIMESPAN=M1
```

轮询间隔（`poll_interval`）、每次拉取数量（`fetch_count`）、回填根数（`backfill`）当前未通过
`.env` 暴露，默认值见 `live/main.py` 的 `build_live_feed()`，需要调整可直接改代码。

```bash
python live/main.py   # 进入实盘轮询，Ctrl+C 退出
```

在自己的代码中使用：

```python
import backtrader as bt
from webull.core.client import ApiClient
from webull.data.data_client import DataClient
from feed import WebullLiveData
from dual_ma_strategy import DualMovingAverageStrategy

api_client = ApiClient(app_key, app_secret, "us")  # 凭证建议从环境变量读取
api_client.add_endpoint("us", "api.webull.com")
data_client = DataClient(api_client)

cerebro = bt.Cerebro()
cerebro.adddata(
    WebullLiveData(
        dataname="AAPL",
        data_client=data_client,
        timespan="M1",
        poll_interval=5,      # 轮询间隔（秒）
        fetch_count=20,       # 每次拉取根数
        backfill=True,        # 首次回填历史 bar
    )
)
cerebro.addstrategy(DualMovingAverageStrategy)
cerebro.run()   # 持续运行，直到 Ctrl+C 或策略调用 runstop()
```

live feed 关键行为：

- `islive()` 返回 `True`，backtrader 会关闭 preload/runonce，逐 bar 驱动策略。
- `_load()` 遵循 backtrader 的 live 契约：有新数据返回 `True`，队列暂空返回 `None`
  （表示"稍后再来"而非结束）。
- 只喂**已收盘**的 bar（`real_time_required` 默认不含未收盘 bar），并保证喂入时间
  严格单调递增，避免重复/回退的 bar 污染策略。
- 轮询间隔请结合 K 线粒度与接口限流合理设置，`fetch_count` 需覆盖两次轮询之间
  可能新增的 bar 数量以防漏 bar。

## 交易 Broker（实盘下单）

`broker.py` 提供 `WebullBroker`，把 backtrader 的 broker 契约映射到 Webull 交易 API，
设计参考 backtrader 官方的 `brokers/ibbroker.py`：

| 组件 | 说明 |
| --- | --- |
| `WebullOrder(OrderBase)` | 把 backtrader 订单类型/方向/有效期映射成 Webull 下单请求体 |
| `WebullCommInfo(CommInfoBase)` | 成本/市值估算（真实佣金由券商结算） |
| `WebullBroker(BrokerBase)` | buy/sell/cancel/getcash/getvalue/getposition + 订单状态轮询 |

与 ibbroker 的关键差异：IB 依赖 TWS 推送回调更新订单，Webull 交易 API 是 HTTP 请求式，
因此这里用**后台线程轮询订单详情**驱动状态流转，不需要额外的 store 层。

### 订单类型映射

| backtrader | Webull `order_type` |
| --- | --- |
| `Market` | `MARKET` |
| `Limit` | `LIMIT` |
| `Stop` | `STOP_LOSS` |
| `StopLimit` | `STOP_LOSS_LIMIT` |
| `StopTrail` | `TRAILING_STOP_LOSS` |
| `Close` | `MARKET_ON_CLOSE` |

### 订单状态映射

Webull 状态 → backtrader 状态：`SUBMITTED`→Accepted、`PARTIAL_FILLED`→Partial、
`FILLED`→Completed、`CANCELLED`→Cancelled、`FAILED`→Rejected。
状态或成交量未变化时不会重复通知策略。

### 启用方式

在 `live/.env` 中配置（订单状态轮询间隔当前未暴露到 `.env`，默认值见
`live/main.py` 的 `build_broker()`）：

```dotenv
# ⚠️ 1=真实下单，0=用 backtrader 模拟 broker（只跑策略不下单）
WEBULL_USE_BROKER=0
# prod（默认）或 sandbox，切换凭证/endpoint/账户 ID
WEBULL_ENV=prod
WEBULL_TRADE_ENDPOINT=pre-openapi-us-alb.webullbroker.com
WEBULL_ACCOUNT_ID=              # 留空则自动选第一个现金账户（prod 环境）
WEBULL_SANDBOX_ACCOUNT_ID=      # sandbox 环境的账户 ID
WEBULL_TRADING_SESSION=CORE     # CORE / ALL / NIGHT
```

```bash
python live/main.py
```

在自己的代码中使用：

```python
import backtrader as bt
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient
from broker import WebullBroker

api_client = ApiClient(app_key, app_secret, "us")
api_client.add_endpoint("us", "pre-openapi-us-alb.webullbroker.com")
trade_client = TradeClient(api_client)

cerebro = bt.Cerebro()
cerebro.setbroker(
    WebullBroker(
        trade_client=trade_client,
        account_id="<your_account_id>",  # account_v2.get_account_list() 可查
        trading_session="CORE",
        poll_interval=2,
    )
)
```

> ⚠️ **风险提示**：启用 `WEBULL_USE_BROKER=1` 后，策略信号会真实提交订单。
> 请先在 Sandbox / Pre 测试环境完整验证下单、撤单、状态回报链路，再考虑接生产。
> 交易 API 需要单独申请权限，详见
> [Trading API Application](https://developer.webull.com/apis/docs/authentication/IndividualApplicationAPI.md)。

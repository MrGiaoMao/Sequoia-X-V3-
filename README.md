# Sequoia-X V3: 王者回归 | The King Returns

> A 股量化选股系统 V3 | A-Share Quantitative Stock Selection System V3

---

## 简介 | Introduction

Sequoia-X V3 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股，
通过飞书交互式卡片推送至群，并在本地生成 Markdown 选股报告。

数据层使用 [baostock](http://baostock.com)（免费、无需注册、无限流）拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite，彻底规避东方财富反爬问题。

---

## 运行方式 | Run Modes

```bash
python main.py                 # 日常模式（默认串行）：增量补数据 + 13 个策略 + 飞书推送 + 本地报告
python main.py --parallel      # 日常并行模式：多进程并行执行策略（可选 --max-workers N 指定进程数）
python main.py --backfill      # 回填模式：单线程保守灌入历史 K 线（较慢，取决于限速）
```

macOS 可双击启动（自动检测 `.venv/bin/python`，缺失则回退系统 `python3`）：

- `launch_sequoia_x.command` —— 日常模式
- `launch_sequoia_x_backfill.command` —— 回填模式

另有 `scripts/bench.py`：只跑策略并统计各自耗时，跳过数据同步、推送与报告，用于基线压测。

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 海龟突破：收盘突破 20 日新高 + 成交额过亿 + 阳线防诱多，按流通市值排序 |
| **MaVolume** | 均线金叉+放量：5 日均线上穿 20 日均线，今日量 > 20 日均量 1.5 倍 |
| **HighTightFlag** | 高而窄旗形：40 日涨幅超 60% + 近 10 日振幅收敛 + 高位抗跌缩量 |
| **LimitUpShakeout** | 涨停洗盘：昨涨停后今放量收阴、不破昨收的洗盘回踩 |
| **UptrendLimitDown** | 上升趋势跌停错杀：趋势中放量跌停级长阴捕捉 |
| **RpsBreakout** | RPS 极强动量突破：全市场 120 日涨幅排名前 10% + 贴近 120 日高点 |
| **RpsYilin** | 亦霖版 RPS 严格突破：RPS 前 5% + 真突破 + 放量 + 趋势过滤 |
| **TripleVolumeBreakout** | 三倍量缩量盘整突破：三倍量信号 + 缩量盘整 + 综合评分排序 |
| **BowlRebound** | 碗口反弹：趋势回踩 + 历史放量异动 + KDJ 低位反弹 |
| **FirstNewHigh30DaysBreakout** | 30 日首次新高：首次新高 + 均线多头发散 + 放量 + 布林中轨强势 |
| **PrivatePlacement** | 定增公告监控（akshare 公告数据，近 7 天定向增发） |
| **TrendMeanReversion** | 上升趋势超跌回归：趋势健康 + 短期超跌 + 止跌确认防接飞刀 |
| **StopFall** | 止跌候选：S2 初步企稳 / S3 止跌确认分级 + 市场宽度动态打分 |

新增策略只需三步：在 `sequoia_x/strategy/` 新建继承 `BaseStrategy` 的文件 → 在 `main.py` 顶部 import → 追加进 `strategy_classes` 列表。

---

## 策略详解 | Strategy Details

### TurtleTrade 海龟突破

- **选股逻辑**：收盘价突破前 20 日最高价；今日成交额 > 1 亿；实体阳线且收于昨收之上（防高开低走诱多）；入选后按流通市值从大到小排序。
- **适用条件**：趋势性突破行情；成交额门槛过滤小盘低流动性标的，适合稳健趋势跟踪。

### MaVolume 均线金叉+放量

- **选股逻辑**：5 日均线上穿 20 日均线（昨下今上）；今日成交量 > 20 日均量 1.5 倍。
- **适用条件**：上升趋势启动初期的金叉确认，适合做趋势起点的早期介入。

### HighTightFlag 高而窄旗形

- **选股逻辑**：近 40 日区间涨幅超 60%（最高/最低 > 1.6）；近 10 日振幅 < 1.15（极度收敛）；近 10 日低点不低于 40 日高点的 80%（高位抗跌）；今日量 < 前 20 日均量 0.6 倍。
- **适用条件**：强势股高位缩量整理末期，等待向上突破；适用于波动收敛的强势市场。

### LimitUpShakeout 涨停洗盘

- **选股逻辑**：昨日涨幅 ≥ 9.5%；今日收阴；今日量 > 昨日 2 倍；今日最低价 ≥ 昨日收盘价（不破昨收）。
- **适用条件**：涨停次日放量洗盘回踩确认，适合短线强势股接力环境。

### UptrendLimitDown 上升趋势跌停错杀

- **选股逻辑**：昨日 MA20 > MA60（趋势中）；今日收盘 ≤ 昨收 × 0.905（跌停级）；今日量 > 20 日均量 2 倍。
- **适用条件**：上升趋势中的恐慌错杀，博取急跌后修复；需注意跌停次日延续风险。

### RpsBreakout RPS 极强动量突破

- **选股逻辑**：120 日涨幅全市场横向排名 RPS ≥ 90（前 10%）；收盘价 ≥ 前 120 日滚动最高价的 90%。
- **适用条件**：强动量延续行情，适合趋势跟踪风格。

### RpsYilin 亦霖版 RPS 严格突破

- **选股逻辑**：RPS ≥ 95（前 5%）；收盘价真突破前 120 日最高价（前高不含当日）；今日量 > 20 日均量 1.5 倍；成交额 > 1 亿；实体阳线且收涨；收盘 > MA20 > MA60；按 RPS、成交额、突破幅度排序取前 30。
- **适用条件**：严格版动量突破，胜率优先；适用于强势趋势市场。

### TripleVolumeBreakout 三倍量缩量盘整突破

- **选股逻辑**：近 3–90 个交易日内出现三倍量信号（量 ≥ 前日 3 倍、收阳、收涨）；信号后进入缩量盘整（平台振幅 ≤ 30%，后半段量能低于前半段）；收盘不低于 MA60 的 95%（无破位）；成交额 ≥ 1 亿；按量能质量、信号时间、缩量程度、盘整振幅、趋势结构、突破强度、成交额综合打分，≥ 55 分优先、≥ 45 分兜底，最多取 8 只。
- **适用条件**：主力放量吸筹后缩量整理再突破的形态，适合中长线波段布局。

### BowlRebound 碗口反弹

- **选股逻辑**：短期趋势线（EMA10 二次平滑）位于多空线（MA14/28/57/114 均值）之上；近 15 日内出现 ≥ 4 倍放量阳线且最大量日为阳线；今日 KDJ-J ≤ 30（超卖）；收盘回到“碗中”或贴近多空线 ±3%、短期趋势线 ±2%；成交额 ≥ 1 亿；按位置 + J 值 + 成交额综合排序。
- **适用条件**：趋势健康前提下的回踩低吸，适合强势股回调到位时介入。

### FirstNewHigh30DaysBreakout 30 日首次新高

- **选股逻辑**：今日最高价突破前 30 日最高且收盘 ≥ 前高 98%，过去 29 日无同类突破（首次新高）；MA5 > MA10 > MA20 > MA60 且均线上行、间距扩大；连续 3 日站上 MA20；量 > 昨 5 日均量 1.2 倍且 < 昨 20 日均量 3 倍（放量不爆量）；阳线收盘在当日振幅上半区、涨幅 ≤ 8.5%；成交额 > 5 亿；排除 ST/停牌。可选指数环境过滤（默认关闭）：沪深 300/中证 500/创业板指至少一个指数收盘站上 MA20 且 MA20 上行。
- **适用条件**：均线多头发散初期的首次突破，捕捉趋势加速起点，规避已高潮个股。

### PrivatePlacement 定增公告监控

- **选股逻辑**：akshare 拉取增发公告，仅保留“定向增发”，按发行日期筛选最近 7 天，去重返回股票代码，不走行情数据。
- **适用条件**：事件驱动观察名单，适合与行情策略配合跟踪。

### TrendMeanReversion 上升趋势超跌回归

- **选股逻辑**：趋势健康（MA20 > MA60、收盘 > MA60、60 日涨幅 > 0）；收盘低于 MA20 3%–10%；近 5 日跌 6%–18%；成交额 > 1 亿；排除连续两日接近跌停或今日接近跌停（防接飞刀）；须满足收阳 / 高于昨收 / 长下影（下影 ≥ 振幅 35% 且收盘高于最低价 2%）之一；按偏离 MA20 幅度 + 回调深度 + 距 MA60 高度打分。
- **适用条件**：上升趋势中的回调企稳，切忌用于单边下跌市。

### StopFall 止跌候选

- **选股逻辑**：硬过滤：上市 ≥ 80 个交易日、60 日回撤 ≤ -12%、距前低 ≤ 2 倍 ATR、20 日收益为负、无一字跌停、排除 ST/退市；七因子打分（低点支撑 25%、下跌减速 15%、抛压衰竭 15%、5 日相对强度 15%、3 日收盘位置 10%、波动率正常化 10%、突破强度 10%）且需 ≥ 4 个确认信号；按全市场站上 MA20 的占比（市场宽度）定分数线——改善（≥ 45% 或 5 日提升 ≥ 5%）为 60 分、中性 65 分、弱势（< 30%）70 分；弱势时只输出 S3（收盘突破前 5 日最高收盘的止跌确认），否则也输出高分 S2（初步企稳）。
- **适用条件**：下跌末期与震荡市寻找企稳标的，市场越弱筛选越严，天然自适应。

---

## 特色功能 | Features

- **飞书交互式卡片**：每个策略生成「📈 选股播报」卡片，含日期、选股数量与雪球链接；股票名称自动补全（沪深走 baostock、北交所走腾讯行情兜底）。
- **策略专属机器人**：通过 `STRATEGY_WEBHOOK_<策略标识>=URL` 为每个策略单独路由飞书机器人，未配置的策略自动使用默认 `FEISHU_WEBHOOK_URL`。
- **策略共振推送**：多策略命中同一股票时按 高/中 吸引力组合生成「🔥 策略共振」卡片，只推送共振股票。
- **每日 Markdown 报告**：自动生成 `Sequoia-X_选股报告_YYYY-MM-DD.md`，默认输出到 `~/Documents/量化交易/今日选股结果`（可用 `REPORT_OUTPUT_DIR` 修改），含概览、各策略明细与共振板块。
- **推送容错**：飞书连接类异常自动重试（最多 3 次，指数退避），失败不影响其他策略。

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10

### 1. 安装依赖

```bash
# 推荐使用 uv（快速包管理器）
uv sync

# 或者 pip
pip install .
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，必填 FEISHU_WEBHOOK_URL（兜底飞书机器人）
```

可选配置：每个策略独立的飞书机器人，以及报告输出目录：

```env
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx   # 必填
STRATEGY_WEBHOOK_MA_VOLUME=https://open.feishu.cn/open-apis/bot/v2/hook/xxx   # 可选
REPORT_OUTPUT_DIR=/path/to/reports   # 可选，默认 ~/Documents/量化交易/今日选股结果
```

为降低 baostock 限流风险，默认启用 2 进程（1~4 可调）并在每个进程的请求之间加入随机等待：

```env
BAOSTOCK_MAX_WORKERS=2
BAOSTOCK_REQUEST_DELAY_SECONDS=0.8
BAOSTOCK_REQUEST_JITTER_SECONDS=0.8
BAOSTOCK_ERROR_COOLDOWN_SECONDS=30
```

### 3. 首次回填历史数据

```bash
python main.py --backfill
```

启用请求保护后，~5200 只 A 股历史后复权日 K 数据回填可能需要 1 小时以上，
具体耗时取决于 `.env` 中的 baostock 限速参数。回填支持断点续传，中断后重新运行即可接着灌。

### 4. 日常运行

```bash
python main.py
```

建议配合 crontab 每个交易日收盘后自动执行：

```cron
15 19 * * 1-5 cd "/Users/tonnymaoyilin/Documents/量化交易/Sequoia-X" && .venv/bin/python main.py >> log.txt 2>&1
```

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                        # 入口：argparse 分发日常/并行/回填模式
├── launch_sequoia_x.command       # macOS 双击启动：日常模式
├── launch_sequoia_x_backfill.command  # macOS 双击启动：回填模式
├── pyproject.toml                 # 依赖声明 + ruff/pytest 配置
├── .env.example                   # 环境变量模板
├── data/                          # SQLite 数据库（运行时生成，不入 git）
├── scripts/
│   └── bench.py                   # 策略耗时基准（不推送、不改数据）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py              # Pydantic-settings 配置管理
│   │   └── logger.py              # rich 结构化日志
│   ├── data/
│   │   └── engine.py              # 数据引擎（baostock 回填 + 增量同步 + SQLite）
│   ├── strategy/
│   │   ├── base.py                # 策略抽象基类
│   │   ├── turtle_trade.py        # 海龟交易策略
│   │   ├── ma_volume.py           # 均线放量策略
│   │   ├── high_tight_flag.py     # 高窄旗形策略
│   │   ├── limit_up_shakeout.py   # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py  # 上升跌停策略
│   │   ├── rps_breakout.py        # RPS 突破策略
│   │   ├── RPS_YILIN.py           # 亦霖版 RPS 严格突破策略
│   │   ├── ThreeTimeYilinStrategy_v3.py  # 三倍量缩量盘整突破策略
│   │   ├── bowl_rebound.py        # 碗口反弹策略
│   │   ├── FirstNewHigh30DaysBreakoutStrategy.py  # 30日首次新高突破策略
│   │   ├── stop_fall.py           # 止跌候选策略
│   │   ├── private_placement.py   # 定增公告监控策略
│   │   └── trend_mean_reversion.py  # 上升趋势超跌回归策略
│   └── notify/
│       ├── feishu.py              # 飞书交互式卡片推送
│       └── report.py              # 每日 Markdown 选股报告
└── tests/                         # 属性测试（hypothesis）
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册、无限流）；仅定增公告策略使用 akshare。
- **复权方式**：后复权（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱。
- **存储**：本地 SQLite（`data/sequoia_v2.db`，WAL 模式），可直接拷贝到其他机器使用。
- **日常增量**：多进程并行（默认 2 进程，`BAOSTOCK_MAX_WORKERS` 可调，上限 4）+ 请求随机抖动，降低被限流风险；写入采用先删后插，避免中断造成脏数据。
- **回填**：单线程保守拉取 + 断点续传 + 每 200 只主动重连 + 单票失败重试 3 次（指数退避）。

---

## 测试 | Tests

```bash
uv run pytest
```

测试覆盖配置管理、数据引擎、飞书推送、日志、入口与策略接口，使用 hypothesis 做属性化测试。

---

## 许可证 | License

MIT

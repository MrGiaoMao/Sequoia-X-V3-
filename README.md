# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送至飞书群。

数据层使用 [baostock](http://baostock.com)（免费、无需注册、无限流）拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite，彻底规避东方财富反爬问题。

---

## 两种运行模式

```bash
python main.py               # 日常模式：4进程增量补数据 + 跑策略 + 飞书推送
python main.py --backfill     # 回填模式：单线程保守灌入历史K线（较慢，取决于限速）
```

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 海龟突破：20日新高 + 成交额过亿 + 阳线防诱多，按涨幅排序 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 高而窄的旗形整理突破 |
| **LimitUpShakeout** | 涨停洗盘回踩确认 |
| **UptrendLimitDown** | 上升趋势中的跌停反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |
| **RpsYilin** | 亦霖版 RPS 严格突破：RPS 前 5% + 真突破 + 放量 + 趋势过滤 |
| **TripleVolumeBreakout** | 三倍量缩量盘整突破：基础过滤 + 综合评分排序 |
| **BowlRebound** | 碗口反弹：强势股回踩低吸模型 |
| **FirstNewHigh30DaysBreakout** | 30日首次新高均线发散突破：首次新高 + 均线多头 + 布林中轨强势 + 放量 |
| **PrivatePlacement** | 定增公告监控 |
| **TrendMeanReversion** | 上升趋势超跌回归：中期趋势健康 + 短期超跌 + 止跌确认 |
| **StrategyResonance** | 策略共振推送：只推送命中高/中吸引力组合的股票名称 |

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
# 编辑 .env，填写飞书 Webhook URL
```

为降低 baostock 限流风险，默认启用 4 进程并在每个进程的请求之间加入随机等待：

```env
BAOSTOCK_MAX_WORKERS=4
BAOSTOCK_REQUEST_DELAY_SECONDS=0.5
BAOSTOCK_REQUEST_JITTER_SECONDS=0.5
BAOSTOCK_ERROR_COOLDOWN_SECONDS=30
```

### 3. 首次回填历史数据

```bash
python main.py --backfill
```

启用请求保护后，~5200 只 A 股历史后复权日 K 数据回填可能需要 1 小时以上，
具体耗时取决于 `.env` 中的 baostock 限速参数。

### 4. 日常运行

```bash
python main.py
```

建议配合 crontab 每个交易日收盘后自动执行：

```cron
15 19 * * 1-5 cd /root/Sequoia-X && .venv/bin/python main.py >> log.txt 2>&1
```

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：argparse 分发日常/回填模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库（运行时生成，不入 git）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   └── engine.py            # 数据引擎（baostock 回填 + 增量同步 + SQLite）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   ├── rps_breakout.py      # RPS 突破策略
│   │   ├── RPS_YILIN.py         # 亦霖版 RPS 严格突破策略
│   │   ├── ThreeTimeYilinStrategy_v3.py # 三倍量缩量盘整突破策略
│   │   ├── bowl_rebound.py      # 碗口反弹策略
│   │   ├── FirstNewHigh30DaysBreakoutStrategy.py # 30日首次新高突破策略
│   │   └── private_placement.py # 定增公告监控策略
│   │   └── trend_mean_reversion.py # 上升趋势超跌回归策略
│   └── notify/
│       └── feishu.py            # 飞书 Webhook 推送
└── tests/                       # 属性测试（hypothesis）
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册、无限流）
- **复权方式**：后复权（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **日常增量**：4 进程并行通过 baostock 拉取，降低请求频率，减少触发限流风险

---

## 许可证 | License

MIT

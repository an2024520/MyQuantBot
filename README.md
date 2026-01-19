# 📘 MyQuantBot 项目架构与数据流向说明书 (Ver 1.0)

## 1. 项目物理结构 (Physical Structure)

本项目采用 **Flask 微服务架构**，结合 **多线程守护 (Daemon Threads)** 模式。代码组织遵循“职责分离”原则，将策略、服务、接口和配置严格分开。

MyQuantBot/
├── run.py                      # [入口] 程序的启动点
├── config.py                   # [配置] 系统级基础配置 (Env/Defaults)
├── requirements.txt            # [依赖] Python 依赖包列表
├── setup.sh & update.sh        # [运维] 环境安装与更新脚本
├── setup_keys.sh               # [核心] API Key 配置向导 (生成 secrets.py)
├── autopilot_config.json       # [配置] 自动驾驶策略配置文件
├── bot_state.json              # [持久化] 机器人运行时状态快照
├── app/
│   ├── __init__.py             # [工厂] App 初始化、服务启动注册
│   ├── routes/                 # [接口层] 处理 HTTP 请求
│   │   ├── api.py              # -> 后端数据接口 (AJAX/REST)
│   │   └── views.py            # -> 前端页面路由 (HTML Render)
│   ├── services/               # [服务层] 后台驻留服务
│   │   ├── monitor.py          # -> 行情监控、SMI计算、SharedState总线
│   │   ├── autopilot_service.py# -> 自动驾驶大脑 (决策层)
│   │   └── bot_manager.py      # -> 机器人管家 (启停控制、状态管理)
│   ├── strategies/             # [策略层] 交易核心逻辑
│   │   ├── future_grid_strategy.py # -> 策略主类 (组合模式入口)
│   │   └── future_grid_modules/    # -> 策略功能拆分 (Mixins)
│   │       ├── initialization.py   # -> 账户/交易所初始化 (含 Key 读取)
│   │       ├── calculation.py      # -> 纯数学计算 (位置、PNL)
│   │       ├── order_engine.py     # -> 挂单/撤单/推窗逻辑
│   │       ├── risk_control.py     # -> 止盈止损风控
│   │       └── data_sync.py        # -> 数据同步
│   ├── templates/              # [前端] HTML 模板
│   └── utils/                  # [工具] 通用工具库
│       ├── indicators.py       # -> 技术指标算法 (SMI, RSI)
│       └── notifier.py         # -> 消息通知 (Telegram/Bark)

---

## 2. 核心架构逻辑 (System Architecture)

系统由 **三大平行线程** 组成，通过 **SharedState (内存总线)** 和 **JSON 文件 (持久化存储)** 进行交互。

### 🧩 线程模型
1.  **Web 主线程 (Flask)**:
    * 职责：响应前端 UI 操作，提供 API 接口。
    * 交互：通过 `BotManager` 控制机器人，通过 `SharedState` 读取行情。
2.  **Monitor 守护线程**:
    * 职责：每隔几秒轮询交易所公有行情，计算 SMI/RSI，更新系统日志。
    * 产出：将最新数据写入 `SharedState`。
3.  **AutoPilot 守护线程**:
    * 职责：也是一个死循环，不断读取 `SharedState` 中的信号。
    * 动作：一旦信号触发阈值，调用 `BotManager` 执行开/平仓。

---

## 3. 数据流向深度解析 (Data Flow)

这是排查问题的关键路径图。

### 3.1 🔑 鉴权与配置流 (The "Key" Flow)
**问题：API Key 是怎么加载的？**

1.  **生成 (Write)**:
    * 用户运行 `setup_keys.sh`。
    * 脚本将 Key 写入独立的外部安全文件：`/opt/myquant_config/secrets.py`。
2.  **加载 (Read)**:
    * `FutureGridBot` 启动初始化。
    * 调用 `initialization.py` 中的 `init_exchange()`。
    * 逻辑：**优先**读传入的 `config` -> **如果为空**，自动导入 `secrets.py` 获取 Key。

### 3.2 📡 行情与信号流 (The "Signal" Flow)
**问题：SMI 信号是如何驱动交易的？**

1.  **采集**: `monitor.py` 线程从交易所 (Binance/OKX) 获取 K 线数据。
2.  **计算**: 调用 `utils/indicators.py` 计算 SMI 数值。
3.  **广播**: 将 `smi`, `price` 存入 `SharedState.market_data`。
4.  **决策**: `autopilot_service.py` 读取 `SharedState`，比对 `autopilot_config.json` 中的阈值。
5.  **执行**: 如果满足条件，调用 `BotManager.start_bot()` 或 `stop_bot()`。

### 3.3 ⚙️ 策略参数与状态流 (The "State" Flow)
**问题：机器人重启后为什么还能记得之前的状态？**

1.  **启动/更新**: 用户在前端修改参数 -> `api.py` -> `BotManager.start_bot(config)`。
2.  **持久化**:
    * `BotManager` 在启动、停止、暂停时，都会调用 `save_state()`。
    * 数据被写入 `bot_state.json` (或生产环境 `/opt/myquant_config/bot_state.json`)。
3.  **复活**:
    * 系统重启 (`app/__init__.py`) -> 调用 `BotManager.load_state()`。
    * 读取 `bot_state.json` -> 如果字段 `running: true`，则自动重新实例化 `FutureGridBot` 并运行。

---

## 4. 关键模块职责说明 (Module Responsibilities)

| 模块 | 角色 | 核心职责 |
| :--- | :--- | :--- |
| **run.py** | 启动器 | 系统的入口，禁止 Reload 模式以防线程双开。 |
| **monitor.py** | 雷达 | **SharedState 的维护者**。负责“看盘”，不负责“交易”。它拥有系统视角的各项指标。 |
| **bot_manager.py** | 管家 | **单例模式**。负责持有 `FutureGridBot` 实例，确保同一时间只有一个机器人在跑。负责状态落盘。 |
| **autopilot_service.py** | 指挥官 | **决策大脑**。它不直接操作交易所，而是通过指挥“管家”来间接控制。包含熔断机制。 |
| **future_grid_strategy.py** | 士兵 | **执行实体**。它是 Mixin 的集合体。它不知道“自动驾驶”的存在，只知道执行具体的网格逻辑。 |
| **order_engine.py** | 扳机 | **推窗逻辑核心**。负责 `_place_order` 和 `_check_order_status`。所有的挂单墙移动逻辑都在这里。 |
| **initialization.py** | 后勤 | **环境准备**。负责连接交易所 API，加载 `secrets.py`，并计算初始网格数组。 |

---

## 5. 架构师总结

目前的架构已经非常成熟，具备了 **企业级量化系统** 的特征：

1.  **解耦 (Decoupling)**: 信号计算 (`Monitor`) 与 交易执行 (`Strategy`) 完全分离。
2.  **鲁棒 (Robustness)**: 通过 `setup_keys.sh` 分离敏感信息，通过 `bot_state.json` 实现断电续传。
3.  **模块化 (Modularity)**: 策略层使用了 **Mixin 混入模式**，将 2000 行的大代码拆解为 5 个小文件，极大地降低了维护难度。
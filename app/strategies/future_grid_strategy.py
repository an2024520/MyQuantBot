# app/strategies/future_grid_strategy.py
import threading
import time
import random

# 引入所有拆分出去的模块 (Mixin)
from app.strategies.future_grid_modules.initialization import FutureGridInitMixin
from app.strategies.future_grid_modules.calculation import FutureGridCalcMixin
from app.strategies.future_grid_modules.risk_control import FutureGridRiskMixin
from app.strategies.future_grid_modules.data_sync import FutureGridSyncMixin
from app.strategies.future_grid_modules.order_engine import FutureGridOrderMixin

class FutureGridBot(FutureGridInitMixin, FutureGridCalcMixin, FutureGridRiskMixin, 
                    FutureGridSyncMixin, FutureGridOrderMixin):
    
    def __init__(self, config, logger_func):
        self.config = config
        self.log = logger_func
        self.exchange = None
        self.grids = []
        self.running = False
        self.paused = False 
        self.market_symbol = None 
        
        # --- Phase 3: 智能轮询状态机 ---
        self.last_sync_time = 0
        self.last_grid_idx = -1
        self.force_sync = True
        self.sync_interval = 15
        # -----------------------------

        # [新增] Phase 4: 推窗策略核心状态 (增量追加)
        self.grid_step = 0.0      # 网格步长缓存
        self.grid_count = 0       # 网格数量缓存
        self.active_orders = {'buy': {}, 'sell': {}}  # 本地挂单记录 {price: order_id}
        self.gap_price = 0.0      # 当前空档价格
        self.state_lock = threading.Lock() # 线程锁确保原子性
        self.order_qty = float(config.get('amount', 0)) # 缓存下单数量
        # -----------------------------
        
        # 前端交互的核心数据结构（键名严格匹配前端）
        self.status_data = {
            "current_grid_idx": -1,
            "profit": 0,           
            "orders": [],          
            "liquidation_price": 0, 
            "liquidation": 0,       # 兼容前端 liq-price 显示
            "unrealized_pnl": 0,    
            "funding_rate": 0,      # 存储百分比数值，如 0.0100 表示 0.0100%
            "current_pos": 0,       
            "entry_price": 0,       
            "last_price": 0,
            "current_price": 0,     # 兼容前端 cur-price 显示
            "wallet_balance": 0,
            "running": False,
            "paused": False
        }

        # 后台运行线程
        self.worker_thread = None

    def run_step(self, current_price):
        if not self.running: return
        
        self.status_data['last_price'] = current_price
        self.status_data['current_price'] = current_price
        self.status_data['running'] = True
        self.status_data['paused'] = self.paused
        
        if self.paused: return 
        
        if not self.exchange.apiKey:
            self.sim_calculate_pnl()
            idx = self.calculate_grid_index(current_price)
            target_pos = self.calculate_target_position(idx)
            self.adjust_position(target_pos)
            self.update_orders_display(idx)
            return

        if self.check_risk_management(): return
        
        # [修改] Phase 4 逻辑接管
        # 1. 优先执行订单状态检查 (推窗逻辑)
        self._check_order_status()

        # 2. Watchdog 纠偏 (保留原逻辑作为低频兜底)
        now = time.time()
        
        # 只有在初始化或定时同步时才执行 Watchdog
        should_sync = False
        new_grid_idx = self.calculate_grid_index(current_price) # 用于 Watchdog 计算理论仓位

        if self.force_sync:
            should_sync = True
            self.force_sync = False
        elif (now - self.last_sync_time) > self.sync_interval:
            should_sync = True

        if should_sync:
            self.sync_account_data()
            target_pos = self.calculate_target_position(new_grid_idx)
            self.adjust_position(target_pos)
            # self.manage_maker_orders(new_grid_idx) # [修改] 已废弃
            
            self.last_grid_idx = new_grid_idx
            self.last_sync_time = now

    def _main_loop(self):
        while self.running:
            if self.paused:
                time.sleep(1)
                continue

            try:
                current_price = self.status_data['last_price']

                if self.exchange and self.exchange.apiKey:
                    try:
                        ticker = self.exchange.fetch_ticker(self.market_symbol)
                        current_price = float(ticker['last'])
                    except Exception as e:
                        self.log(f"[价格获取失败] {e}，使用上次价格继续")

                else:
                    fluctuation = random.uniform(-0.005, 0.005)
                    current_price *= (1 + fluctuation)
                    if current_price > 100:
                        current_price = round(current_price, 2)
                    elif current_price > 1:
                        current_price = round(current_price, 4)
                    else:
                        current_price = round(current_price, 6)

                self.status_data['last_price'] = current_price
                self.run_step(current_price)

            except Exception as e:
                self.log(f"[主循环异常] {e}")

            time.sleep(1)

    def _initialize_and_run(self):
        self.log("[系统] 正在后台初始化交易所、账户和网格...")

        try:
            if not self.init_exchange():
                raise Exception("交易所初始化失败")
            if not self.setup_account():
                raise Exception("账户设置失败")
            if not self.generate_grids():
                raise Exception("网格生成失败")

            start_price = 0
            try:
                if self.exchange and self.exchange.apiKey:
                    ticker = self.exchange.fetch_ticker(self.market_symbol)
                    start_price = float(ticker['last'])
                else:
                    start_price = sum(self.grids) / len(self.grids)
                self.status_data['last_price'] = start_price
                self.status_data['current_price'] = start_price
                
                # [修改] 使用智能初始化逻辑生成挂单墙 (Strategy Aware)
                self.initialize_grid_orders(start_price)
                
            except Exception as e:
                self.log(f"[警告] 初始价格获取失败: {e}")
                self.update_orders_display(-1)

            mode = self.config.get('strategy_type', 'neutral')
            self.log(f"[合约] 策略初始化完成 (Phase 4 Event Driven) | 模式: {mode}")

            # [修改] 移除旧的 run_step 初始化调用，防止逻辑重叠
            # 建仓工作交由后续的 Watchdog 自动接管

            self._main_loop()

        except Exception as e:
            self.log(f"[初始化严重错误] {e}，策略无法启动")
            self.running = False

    def start(self):
        if self.running:
            self.log("[警告] 策略已在运行中")
            return

        self.start_time = time.time()
        self.running = True
        self.paused = False
        self.force_sync = True
        self.last_grid_idx = -1

        self.worker_thread = threading.Thread(target=self._initialize_and_run, daemon=True)
        self.worker_thread.start()

        self.log("[系统] 启动命令已接收，后台线程正在初始化（不会阻塞界面）")

    def pause(self):
        self.paused = True
        self.log("[指令] 策略已暂停！")
        # [修改] 使用新版撤单逻辑
        self._cancel_all_orders()
        self.log("[系统] 挂单已全部撤销")

    def resume(self):
        self.paused = False
        self.force_sync = True 
        self.log("[指令] 策略恢复运行！")
        # [新增] 恢复时重新初始化挂单
        try:
            current = self.status_data['last_price']
            self.initialize_grid_orders(current)
        except: pass

    def stop(self):
        self.log("[指令] 正在停止... 撤单并平仓")
        self.running = False 
        self.start_time = None 
        self.paused = False

        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=15)

        if self.exchange and self.exchange.apiKey:
            try:
                # [修改] 使用新版撤单逻辑
                self._cancel_all_orders()
                
                positions = self.exchange.fetch_positions([self.market_symbol])
                for pos in positions:
                    if pos['symbol'] == self.market_symbol:
                        amt = self._get_position_amount(pos['info'])
                        if amt != 0:
                            side = 'sell' if amt > 0 else 'buy'
                            self.exchange.create_order(self.market_symbol, 'market', side, abs(amt))
                            self.log(f"[系统] 已平仓 {amt}")
            except Exception as e:
                self.log(f"[停止过程出错] {e}")
        else:
            self.status_data['current_pos'] = 0
            self.log("[模拟] 已重置虚拟持仓")
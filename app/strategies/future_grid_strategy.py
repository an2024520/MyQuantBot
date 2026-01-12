import time
import threading
import traceback
import ccxt
from datetime import datetime
import pandas as pd
import numpy as np

# 【新增】引入拆分出去的组件
from .components.grid_math import GridMath
from .components.order_sync import OrderSync

class FutureGridBot:
    def __init__(self, config, logger_func):
        self.config = config
        self.log = logger_func
        self.running = False
        self.paused = False
        
        # 交易所实例
        self.exchange = None
        self.market_symbol = config['symbol']
        self.api_key = config['api_key']
        self.secret = config['api_secret']
        self.password = config.get('password', '')
        
        # 状态数据 (兼容前端)
        self.status_data = {
            'status': 'stopped',
            'wallet_balance': 0.0,
            'unrealized_pnl': 0.0,
            'current_pos': 0.0,
            'entry_price': 0.0,
            'last_price': 0.0,
            'next_grid': 0.0,
            'grid_idx': -1,
            'orders': []
        }
        
        # 模拟模式相关
        self.is_sim = config.get('is_sim', False)
        self.sim_price = float(config.get('initial_price', 0))
        self.sim_balance = float(config.get('initial_balance', 1000))
        self.sim_pos = 0.0
        self.sim_entry_price = 0.0

        # 【组件初始化】
        # 1. 数学计算组件
        self.grid_math = GridMath(config, logger_func)
        # 2. 挂单同步组件 (exchange暂时为None, init_exchange时注入)
        self.order_sync = OrderSync(None, config, logger_func)

        # 为了兼容性，主类依然持有 grids 列表，但数据来源由 grid_math 提供
        self.grids = []

    def init_exchange(self):
        """初始化交易所连接"""
        if self.is_sim:
            self.log("[模拟模式] 启动虚拟交易所环境")
            # 模拟模式下，也要把 None 传给组件，或者传一个模拟对象（此处保持 None 即可，组件内有处理）
            self.order_sync.exchange = None
            return self.generate_grids()

        try:
            exchange_class = getattr(ccxt, self.config['exchange_id'])
            params = {
                'apiKey': self.api_key,
                'secret': self.secret,
                'enableRateLimit': True,
                'options': {'defaultType': 'future'} 
            }
            if self.password:
                params['password'] = self.password
            
            self.exchange = exchange_class(params)
            
            # 尝试加载市场，验证连接
            self.exchange.load_markets()
            
            # 开启统一账户模式检查 (OKX特有)
            if self.config['exchange_id'] == 'okx':
                try:
                    self.exchange.set_leverage(int(self.config['leverage']), self.market_symbol)
                except Exception as e:
                    self.log(f"[OKX杠杆设置警告] {e}")

            self.log(f"[交易所] 连接成功: {self.config['exchange_id']}")
            
            # 【关键】将连接好的 exchange 注入到 OrderSync 组件中
            self.order_sync.exchange = self.exchange
            
            return self.generate_grids()
            
        except Exception as e:
            self.log(f"[交易所连接失败] {e}")
            return False

    def generate_grids(self):
        """生成网格 (委托给组件)"""
        # 1. 调用组件计算
        result = self.grid_math.generate_grids()
        # 2. 同步数据回主类
        self.grids = self.grid_math.grids
        return result

    def calculate_grid_index(self, price):
        """计算网格索引 (委托给组件)"""
        return self.grid_math.calculate_grid_index(price)

    def calculate_target_position(self, grid_idx):
        """计算目标仓位 (委托给组件)"""
        return self.grid_math.calculate_target_position(grid_idx)

    def _to_precision(self, price=None, amount=None):
        """
        精度处理辅助函数
        注：主类保留此函数，用于 adjust_position (市价单) 的处理
        """
        if self.is_sim: return str(price) if price else str(amount)
        try:
            if price is not None:
                return self.exchange.price_to_precision(self.market_symbol, price)
            if amount is not None:
                return self.exchange.amount_to_precision(self.market_symbol, amount)
        except:
            pass
        return str(price) if price else str(amount)

    def get_market_price(self):
        """获取最新价格"""
        if self.is_sim:
            # 模拟随机游走
            change = (np.random.random() - 0.5) * 0.005
            self.sim_price = self.sim_price * (1 + change)
            return self.sim_price
        
        try:
            ticker = self.exchange.fetch_ticker(self.market_symbol)
            return float(ticker['last'])
        except Exception as e:
            self.log(f"[获取行情失败] {e}")
            return 0.0

    def sync_account_data(self, price):
        """同步账户权益、持仓信息"""
        if self.is_sim:
            # 模拟计算未实现盈亏
            if self.sim_pos != 0:
                pnl = (price - self.sim_entry_price) * self.sim_pos
                # 如果是做空，反向
                if self.sim_pos < 0:
                    pnl = (self.sim_entry_price - price) * abs(self.sim_pos)
            else:
                pnl = 0
            
            self.status_data.update({
                'wallet_balance': round(self.sim_balance, 2),
                'unrealized_pnl': round(pnl, 4),
                'current_pos': self.sim_pos,
                'entry_price': round(self.sim_entry_price, 4),
                'last_price': round(price, 4)
            })
            return

        try:
            # 1. 查询余额
            balance = self.exchange.fetch_balance()
            total_wallet = balance['total'].get('USDT', 0)
            
            # 2. 查询持仓
            positions = self.exchange.fetch_positions([self.market_symbol])
            current_pos = 0.0
            entry_price = 0.0
            unrealized = 0.0
            
            for p in positions:
                # 兼容不同交易所返回结构
                if p['symbol'] == self.market_symbol:
                    size = float(p.get('contracts', 0) or p.get('info', {}).get('sz', 0))
                    if size == 0: size = float(p.get('amount', 0)) # fallback
                    
                    side = p['side'] # long/short
                    if side == 'short': size = -size
                    
                    current_pos = size
                    entry_price = float(p.get('entryPrice', 0) or 0)
                    unrealized = float(p.get('unrealizedPnl', 0) or 0)
                    break
            
            self.status_data.update({
                'wallet_balance': round(total_wallet, 2),
                'unrealized_pnl': round(unrealized, 4),
                'current_pos': current_pos,
                'entry_price': round(entry_price, 4),
                'last_price': round(price, 4)
            })
            
        except Exception as e:
            self.log(f"[账户同步失败] {e}")

    def manage_maker_orders(self, current_grid_idx):
        """挂单管理 (委托给组件)"""
        # 调用 OrderSync 组件
        force_sync = self.order_sync.manage_maker_orders(
            current_grid_idx, 
            self.grids, 
            self.market_symbol
        )
        
        # 【关键】将组件中更新的 orders 列表同步回 status_data，供前端显示
        self.status_data['orders'] = self.order_sync.orders
        
        return force_sync

    def update_orders_display(self, current_idx):
        """更新前端显示 (委托给组件)"""
        self.order_sync.update_orders_display(current_idx, self.grids)
        self.status_data['orders'] = self.order_sync.orders

    def adjust_position(self, target_pos):
        """
        仓位纠偏 (保留在主类中，因为涉及核心资金操作 - 市价单)
        """
        current_pos = float(self.status_data['current_pos'])
        diff = target_pos - current_pos
        
        # 最小下单数量阈值 (需要根据币种调整，这里简单硬编码或读取配置)
        min_amount = float(self.config.get('min_amount', 0.001)) 
        
        if abs(diff) < min_amount:
            return

        self.log(f"[仓位纠偏] 当前: {current_pos}, 目标: {target_pos}, 需变动: {diff}")
        
        if self.is_sim:
            # 模拟成交
            cost = diff * self.sim_price
            fee = abs(cost) * 0.0005 # 模拟手续费
            self.sim_balance -= fee
            
            # 更新持仓均价
            if (self.sim_pos > 0 and diff > 0) or (self.sim_pos < 0 and diff < 0):
                # 加仓：算均价
                old_val = self.sim_pos * self.sim_entry_price
                new_val = diff * self.sim_price
                self.sim_entry_price = (old_val + new_val) / (self.sim_pos + diff)
            elif self.sim_pos == 0:
                self.sim_entry_price = self.sim_price
            
            self.sim_pos += diff
            return

        # 实盘下单
        try:
            side = 'buy' if diff > 0 else 'sell'
            amount_abs = abs(diff)
            
            # 转换精度
            amt_str = self._to_precision(amount=amount_abs)
            
            self.exchange.create_order(
                self.market_symbol, 
                'market', 
                side, 
                amt_str
            )
            time.sleep(1) # 等待成交
            self.log(f"[纠偏成功] {side} {amt_str}")
            
        except Exception as e:
            self.log(f"[纠偏下单失败] {e}")

    def run(self):
        """主循环"""
        self.log("策略线程启动...")
        self.running = True
        
        # 初始连接
        if not self.init_exchange():
            self.status_data['status'] = 'error'
            self.running = False
            return

        self.status_data['status'] = 'running'
        
        error_count = 0
        while self.running:
            try:
                if self.paused:
                    self.status_data['status'] = 'paused'
                    time.sleep(1)
                    continue
                else:
                    self.status_data['status'] = 'running'

                # 1. 获取行情
                price = self.get_market_price()
                if price <= 0:
                    time.sleep(2)
                    continue

                # 2. 同步账户
                self.sync_account_data(price)

                # 3. 计算网格位置
                grid_idx = self.calculate_grid_index(price)
                self.status_data['grid_idx'] = grid_idx
                
                # 4. 计算目标仓位并纠偏
                target_pos = self.calculate_target_position(grid_idx)
                self.adjust_position(target_pos)
                
                # 5. 维护挂单 (Maker)
                # 如果 manage_maker_orders 返回 True，说明挂单变动剧烈，需要加快循环或额外处理
                self.manage_maker_orders(grid_idx)

                # 正常休眠
                error_count = 0
                time.sleep(float(self.config.get('interval', 5)))

            except Exception as e:
                error_count += 1
                self.log(f"[主循环异常] {e}")
                traceback.print_exc()
                time.sleep(5)
                if error_count > 10:
                    self.log("错误次数过多，策略自动停止")
                    self.running = False
                    self.status_data['status'] = 'error'

    def stop(self):
        self.running = False
        self.log("正在停止策略...")
        # 可以在这里添加 撤销所有挂单 的逻辑
        if self.exchange and not self.is_sim:
            try:
                self.exchange.cancel_all_orders(self.market_symbol)
            except:
                pass
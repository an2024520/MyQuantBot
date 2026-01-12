import time
import threading
import traceback
import ccxt
import os
import importlib.util
import math
from datetime import datetime
import pandas as pd
import numpy as np

# 引入拆分出去的组件
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
        self.grid_math = GridMath(config, logger_func)
        self.order_sync = OrderSync(None, config, logger_func)

        # 兼容性保留
        self.grids = []

    def init_exchange(self):
        """初始化交易所连接 (完全恢复原有逻辑)"""
        if self.is_sim:
            self.log("[模拟模式] 启动虚拟交易所环境")
            self.order_sync.exchange = None
            return self.generate_grids()

        try:
            exchange_id = self.config.get('exchange_id', 'binance')
            exchange_class = getattr(ccxt, exchange_id)
            
            # --- 恢复原有的密钥加载逻辑 ---
            api_key = self.config.get('api_key', '')
            secret = self.config.get('secret', '')
            password = self.config.get('password', '')

            EXTERNAL_SECRETS_PATH = "/opt/myquant_config/secrets.py"
            
            if not api_key:
                if os.path.exists(EXTERNAL_SECRETS_PATH):
                    try:
                        spec = importlib.util.spec_from_file_location("external_secrets", EXTERNAL_SECRETS_PATH)
                        ext_mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(ext_mod)
                        
                        keys = getattr(ext_mod, 'HARDCODED_KEYS', {})
                        
                        if keys.get('exchange_id') == exchange_id:
                            api_key = keys.get('apiKey', '')
                            secret = keys.get('secret', '')
                            password = keys.get('password', '')
                            self.log(f"[系统] ✅ 已加载外部密钥舱 (/opt/myquant_config/)")
                    except Exception as e:
                        self.log(f"[系统] 外部密钥加载失败: {e}")

            params = {
                'apiKey': api_key,
                'secret': secret,
                'enableRateLimit': True,
                'options': {'defaultType': 'future'} 
            }
            if password:
                params['password'] = password
            # ---------------------------

            self.exchange = exchange_class(params)
            
            # 尝试加载市场
            self.exchange.load_markets()
            
            # 开启统一账户模式检查 (OKX特有)
            if self.config['exchange_id'] == 'okx':
                try:
                    self.exchange.set_leverage(int(self.config['leverage']), self.market_symbol)
                except Exception as e:
                    self.log(f"[OKX杠杆设置警告] {e}")

            self.log(f"[交易所] 连接成功: {self.config['exchange_id']}")
            
            # 【关键】连接成功后，注入到 OrderSync 组件
            self.order_sync.exchange = self.exchange
            
            return self.generate_grids()
            
        except Exception as e:
            self.log(f"[交易所连接失败] {e}")
            return False

    def generate_grids(self):
        result = self.grid_math.generate_grids()
        self.grids = self.grid_math.grids
        return result

    def calculate_grid_index(self, price):
        return self.grid_math.calculate_grid_index(price)

    def calculate_target_position(self, grid_idx):
        return self.grid_math.calculate_target_position(grid_idx)

    def _to_precision(self, price=None, amount=None):
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
        if self.is_sim:
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
        if self.is_sim:
            if self.sim_pos != 0:
                pnl = (price - self.sim_entry_price) * self.sim_pos
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
            balance = self.exchange.fetch_balance()
            total_wallet = balance['total'].get('USDT', 0)
            
            positions = self.exchange.fetch_positions([self.market_symbol])
            current_pos = 0.0
            entry_price = 0.0
            unrealized = 0.0
            
            for p in positions:
                if p['symbol'] == self.market_symbol:
                    size = float(p.get('contracts', 0) or p.get('info', {}).get('sz', 0))
                    if size == 0: size = float(p.get('amount', 0))
                    
                    side = p['side']
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
        force_sync = self.order_sync.manage_maker_orders(
            current_grid_idx, 
            self.grids, 
            self.market_symbol
        )
        self.status_data['orders'] = self.order_sync.orders
        return force_sync

    def update_orders_display(self, current_idx):
        self.order_sync.update_orders_display(current_idx, self.grids)
        self.status_data['orders'] = self.order_sync.orders

    def adjust_position(self, target_pos):
        current_pos = float(self.status_data['current_pos'])
        diff = target_pos - current_pos
        min_amount = float(self.config.get('min_amount', 0.001)) 
        
        if abs(diff) < min_amount:
            return

        self.log(f"[仓位纠偏] 当前: {current_pos}, 目标: {target_pos}, 需变动: {diff}")
        
        if self.is_sim:
            cost = diff * self.sim_price
            fee = abs(cost) * 0.0005
            self.sim_balance -= fee
            
            if (self.sim_pos > 0 and diff > 0) or (self.sim_pos < 0 and diff < 0):
                old_val = self.sim_pos * self.sim_entry_price
                new_val = diff * self.sim_price
                self.sim_entry_price = (old_val + new_val) / (self.sim_pos + diff)
            elif self.sim_pos == 0:
                self.sim_entry_price = self.sim_price
            
            self.sim_pos += diff
            return

        try:
            side = 'buy' if diff > 0 else 'sell'
            amount_abs = abs(diff)
            amt_str = self._to_precision(amount=amount_abs)
            
            self.exchange.create_order(
                self.market_symbol, 
                'market', 
                side, 
                amt_str
            )
            time.sleep(1)
            self.log(f"[纠偏成功] {side} {amt_str}")
            
        except Exception as e:
            self.log(f"[纠偏下单失败] {e}")

    def run(self):
        self.log("策略线程启动...")
        self.running = True
        
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

                price = self.get_market_price()
                if price <= 0:
                    time.sleep(2)
                    continue

                self.sync_account_data(price)

                grid_idx = self.calculate_grid_index(price)
                self.status_data['grid_idx'] = grid_idx
                
                target_pos = self.calculate_target_position(grid_idx)
                self.adjust_position(target_pos)
                
                self.manage_maker_orders(grid_idx)

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
        if self.exchange and not self.is_sim:
            try:
                self.exchange.cancel_all_orders(self.market_symbol)
            except:
                pass
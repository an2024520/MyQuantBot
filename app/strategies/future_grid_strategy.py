# app/strategies/future_grid_strategy.py
import ccxt
import time
import math

class FutureGridBot:
    def __init__(self, config, logger_func):
        self.config = config
        self.log = logger_func
        self.exchange = None
        self.grids = []
        self.running = False
        self.paused = False  # <--- 【新增】暂停状态
        self.market_symbol = None 
        
        self.last_update_time = 0 
        
        self.status_data = {
            "current_grid_idx": -1,
            "total_profit": 0, 
            "grid_orders": [], 
            "liquidation_price": 0, 
            "unrealized_pnl": 0,    
            "funding_rate": 0,      
            "current_pos": 0,       
            "entry_price": 0,       
            "last_price": 0,        
            "wallet_balance": 0     
        }

    def init_exchange(self):
        try:
            exchange_id = self.config.get('exchange_id', 'binance')
            exchange_class = getattr(ccxt, exchange_id)
            params = {
                'apiKey': self.config.get('api_key', ''),
                'secret': self.config.get('secret', ''),
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'}, 
                'timeout': 30000
            }
            if self.config.get('password'):
                params['password'] = self.config.get('password')

            self.exchange = exchange_class(params)
            self.exchange.load_markets()
            
            user_symbol = self.config['symbol']
            target_base = user_symbol.split('/')[0]
            target_quote = user_symbol.split('/')[1]
            
            self.market_symbol = user_symbol
            found = False
            for market in self.exchange.markets.values():
                if (market['base'] == target_base and 
                    market['quote'] == target_quote and 
                    market['swap']):
                    self.market_symbol = market['symbol']
                    found = True
                    break
            
            if not found:
                self.log(f"[警告] 未找到精准匹配的 {user_symbol} 合约")
            else:
                self.log(f"[合约] 初始化成功: {self.market_symbol}")
                
            return True
        except Exception as e:
            self.log(f"[初始化失败] {e}")
            return False

    def setup_account(self):
        try:
            if not self.exchange.apiKey:
                sim_bal = float(self.config.get('sim_balance', 1000))
                self.status_data['wallet_balance'] = sim_bal
                self.log(f"[模拟模式] 初始资金: {sim_bal}")
                return True

            leverage = int(self.config.get('leverage', 1))
            try: self.exchange.set_leverage(leverage, self.market_symbol)
            except: pass 
            try: self.exchange.set_position_mode(hedged=False, symbol=self.market_symbol)
            except: pass
            return True
        except Exception as e:
            self.log(f"[账户设置错误] {e}")
            return False

    def generate_grids(self):
        try:
            lower = float(self.config['lower_price'])
            upper = float(self.config['upper_price'])
            num = int(self.config['grid_num'])
            if num < 2: num = 2
            
            step = (upper - lower) / num
            self.grids = [lower + i * step for i in range(num + 1)]
            
            digits = 2 if lower > 100 else (4 if lower > 1 else 6)
            self.grids = [round(g, digits) for g in self.grids]
            
            self.log(f"[网格生成] 区间 {lower}-{upper}, 共 {num} 格")
            return True
        except Exception as e:
            self.log(f"[参数错误] {e}")
            return False

    def _get_position_amount(self, pos_info):
        try:
            if 'positionAmt' in pos_info: return float(pos_info['positionAmt'])
            if 'pos' in pos_info: return float(pos_info['pos'])
            return 0.0
        except: return 0.0

    def fetch_market_data(self):
        if not self.running: return

        try:
            ticker = self.exchange.fetch_ticker(self.market_symbol)
            self.status_data['last_price'] = float(ticker['last'])
        except: return 

        if self.exchange.apiKey:
            try:
                positions = self.exchange.fetch_positions([self.market_symbol])
                found_pos = False
                for pos in positions:
                    if pos['symbol'] == self.market_symbol:
                        self.status_data['current_pos'] = self._get_position_amount(pos['info'])
                        self.status_data['entry_price'] = float(pos.get('entryPrice') or 0)
                        self.status_data['liquidation_price'] = float(pos.get('liquidationPrice') or 0)
                        self.status_data['unrealized_pnl'] = float(pos.get('unrealizedPnl') or 0)
                        found_pos = True
                        break
                if not found_pos: self.status_data['current_pos'] = 0

                balance = self.exchange.fetch_balance()
                quote_currency = self.config['symbol'].split('/')[1] 
                self.status_data['wallet_balance'] = float(balance.get(quote_currency, {}).get('total', 0))
            except Exception as e:
                pass
        else:
            self.sim_calculate_pnl()

    def sim_calculate_pnl(self):
        try:
            entry = self.status_data.get('entry_price', 0)
            pos = self.status_data.get('current_pos', 0)
            last = self.status_data.get('last_price', entry)
            leverage = int(self.config.get('leverage', 1))
            
            if entry > 0 and pos != 0:
                if pos > 0: 
                    self.status_data['unrealized_pnl'] = (last - entry) * abs(pos)
                    self.status_data['liquidation_price'] = entry * (1 - 1/leverage + 0.005)
                else: 
                    self.status_data['unrealized_pnl'] = (entry - last) * abs(pos)
                    self.status_data['liquidation_price'] = entry * (1 + 1/leverage - 0.005)
            else:
                self.status_data['unrealized_pnl'] = 0
                self.status_data['liquidation_price'] = 0
        except: pass

    # 风控检查函数
    def check_risk_management(self):
        current_price = self.status_data['last_price']
        if current_price <= 0: return False

        stop_loss = self.config.get('stop_loss')
        take_profit = self.config.get('take_profit')
        mode = self.config.get('strategy_type', 'neutral')

        # 1. 检查止损
        if stop_loss and str(stop_loss).strip():
            sl_price = float(stop_loss)
            triggered = False
            if mode == 'short':
                if current_price >= sl_price: triggered = True
            else:
                if current_price <= sl_price: triggered = True
            
            if triggered:
                self.log(f"[风控触发] 现价 {current_price} 触及止损线 {sl_price}，正在停止策略...")
                self.stop()
                return True

        # 2. 检查止盈
        if take_profit and str(take_profit).strip():
            tp_price = float(take_profit)
            triggered = False
            if mode == 'short':
                if current_price <= tp_price: triggered = True
            else:
                if current_price >= tp_price: triggered = True
            
            if triggered:
                self.log(f"[风控触发] 现价 {current_price} 触及止盈线 {tp_price}，正在止盈退出...")
                self.stop()
                return True
        return False

    def calculate_target_position(self):
        current_price = self.status_data['last_price']
        if current_price == 0: return 0, -1

        grid_idx = -1
        for i, p in enumerate(self.grids):
            if current_price >= p: grid_idx = i
            else: break
        
        self.status_data['current_grid_idx'] = grid_idx
        
        if grid_idx < 0: grid_idx = 0 
        if grid_idx >= len(self.grids): grid_idx = len(self.grids) - 1 

        mode = self.config.get('strategy_type', 'neutral')
        amount_per_grid = float(self.config['amount'])
        total_grids = len(self.grids) - 1
        
        target_pos = 0

        if mode == 'long':
            hold_grids = total_grids - grid_idx
            if hold_grids < 0: hold_grids = 0
            target_pos = hold_grids * amount_per_grid
            
        elif mode == 'short':
            hold_grids = grid_idx
            target_pos = -(hold_grids * amount_per_grid)
            
        elif mode == 'neutral':
            mid_idx = total_grids / 2
            diff_grids = mid_idx - grid_idx
            target_pos = diff_grids * amount_per_grid

        return target_pos, grid_idx

    def adjust_position(self, target_pos):
        current_pos = self.status_data['current_pos']
        amount_per_grid = float(self.config['amount'])
        
        diff = target_pos - current_pos
        
        if abs(diff) < (amount_per_grid * 0.5):
            return

        side = 'buy' if diff > 0 else 'sell'
        qty = abs(diff)
        
        if not self.exchange.apiKey:
            self.log(f"[模拟纠偏] 目标{target_pos:.4f} 实持{current_pos:.4f} -> 市价{side} {qty:.4f}")
            self.status_data['current_pos'] += diff
            if self.status_data['current_pos'] != 0:
                self.status_data['entry_price'] = self.status_data['last_price']
            return

        try:
            self.log(f"[系统纠偏] 偏离检测! 正在{side} {qty:.4f}")
            
            order = self.exchange.create_order(self.market_symbol, 'market', side, qty)
            
            filled = order.get('filled')
            if filled is None:
                filled = float(order.get('amount', qty))
            else:
                filled = float(filled)
            
            self.log(f"[成交确认] 订单已提交，成交量: {filled}")

            max_retries = 8
            synced = False
            for i in range(max_retries):
                time.sleep(1.5)
                self.fetch_market_data()
                
                new_pos = self.status_data['current_pos']
                if abs(new_pos - target_pos) < (amount_per_grid * 0.5):
                    self.log(f"[同步成功] 持仓已更新为 {new_pos}")
                    synced = True
                    break
                else:
                    self.log(f"[数据延迟] 交易所显示 {new_pos} (目标 {target_pos})，重试 {i+1}...")
            
            if not synced:
                self.log(f"[警告] 交易所数据同步超时，暂缓操作")

        except Exception as e:
            err_msg = str(e).lower()
            if "insufficient" in err_msg or "margin" in err_msg:
                self.log(f"[严重错误] 交易所提示保证金不足！策略急停。")
                self.stop() 
            else:
                self.log(f"[纠偏失败] {e}")

    def manage_maker_orders(self, current_grid_idx):
        if not self.exchange.apiKey: 
            self.update_orders_display(current_grid_idx)
            return

        try:
            active_limit = int(self.config.get('active_order_limit', 5))
            amount = float(self.config['amount'])
            
            buy_indices = []
            sell_indices = []

            for i in range(current_grid_idx - 1, current_grid_idx - 1 - active_limit, -1):
                if i >= 0: buy_indices.append(i)
            
            for i in range(current_grid_idx + 1, current_grid_idx + 1 + active_limit):
                if i < len(self.grids): sell_indices.append(i)

            open_orders = self.exchange.fetch_open_orders(self.market_symbol)
            
            wanted_prices = set([self.grids[i] for i in buy_indices + sell_indices])
            kept_prices = set()
            
            for order in open_orders:
                price = float(order['price'])
                is_wanted = False
                matched_wanted = None
                for wp in wanted_prices:
                    if abs(price - wp) < (wp * 0.0001):
                        matched_wanted = wp
                        is_wanted = True
                        break
                
                should_cancel = False
                if not is_wanted: should_cancel = True
                elif matched_wanted in kept_prices: should_cancel = True
                
                if should_cancel:
                    try:
                        self.exchange.cancel_order(order['id'], self.market_symbol)
                    except: pass
                else:
                    if matched_wanted: kept_prices.add(matched_wanted)
            
            for idx in buy_indices:
                price = self.grids[idx]
                already_exists = False
                for kp in kept_prices:
                    if abs(kp - price) < (price * 0.0001):
                        already_exists = True
                        break
                if not already_exists:
                    try: self.exchange.create_order(self.market_symbol, 'limit', 'buy', amount, price)
                    except Exception as e: pass 
            
            for idx in sell_indices:
                price = self.grids[idx]
                already_exists = False
                for kp in kept_prices:
                    if abs(kp - price) < (price * 0.0001):
                        already_exists = True
                        break
                if not already_exists:
                    try: self.exchange.create_order(self.market_symbol, 'limit', 'sell', amount, price)
                    except Exception as e: pass

            self.update_orders_display(current_grid_idx)
            
        except Exception as e:
            self.log(f"[挂单维护出错] {e}")

    def update_orders_display(self, current_idx):
        orders = []
        amount = self.config['amount']
        active_limit = int(self.config.get('active_order_limit', 5))
        
        for i in range(len(self.grids)-1, -1, -1):
            price = self.grids[i]
            order_type = "---"
            style = "text-muted"
            
            if i == current_idx:
                style = "text-warning bg-dark border border-warning"
                order_type = "⚡ 现价 ⚡"
            elif i > current_idx and i <= current_idx + active_limit:
                order_type = "SELL (挂单)"
                style = "text-danger"
            elif i < current_idx and i >= current_idx - active_limit:
                order_type = "BUY (挂单)"
                style = "text-success"
                
            orders.append({"idx": i, "price": price, "type": order_type, "amt": amount, "style": style})
        self.status_data['grid_orders'] = orders

    def run_step(self, current_price):
        if not self.running: return
        self.status_data['last_price'] = current_price
        
        # --- 【新增】暂停拦截逻辑 ---
        if self.paused:
            return 
        # ---------------------------
        
        if not self.exchange.apiKey:
            self.sim_calculate_pnl()

        now = time.time()
        if now - self.last_update_time < 5:
            return
        
        self.last_update_time = now 
        
        self.fetch_market_data()
        
        # 风控检查
        if self.check_risk_management():
            return
            
        target_pos, grid_idx = self.calculate_target_position()
        self.adjust_position(target_pos)
        self.manage_maker_orders(grid_idx)

    def start(self):
        if self.init_exchange() and self.setup_account() and self.generate_grids():
            self.running = True
            self.paused = False # 确保启动时不是暂停状态
            mode = self.config.get('strategy_type')
            self.log(f"[合约] 策略启动 (Hard Core v7.7) | 模式: {mode}")
            
            self.last_update_time = 0 
            self.run_step(self.status_data.get('last_price', 0))
        else:
            self.running = False

    # 【新增】暂停方法
    def pause(self):
        """暂停：保留持仓，仅撤销挂单"""
        self.paused = True
        self.log("[指令] 策略已暂停！(持仓保留，挂单撤销)")
        
        if self.exchange.apiKey:
            try:
                open_orders = self.exchange.fetch_open_orders(self.market_symbol)
                for order in open_orders:
                    try:
                        self.exchange.cancel_order(order['id'], self.market_symbol)
                    except: pass
                self.log("[系统] 挂单已全部撤销，等待恢复...")
            except Exception as e:
                self.log(f"[暂停撤单失败] {e}")

    # 【新增】恢复方法
    def resume(self):
        """恢复：继续运行"""
        self.paused = False
        self.log("[指令] 策略恢复运行！正在重新计算网格...")
        # 下一次 run_step 会自动处理补单和挂单

    def stop(self):
        self.log("[指令] 正在停止... 撤单并平仓")
        self.running = False 
        self.paused = False
        
        if self.exchange.apiKey:
            try:
                self.log("[停止] 正在逐个撤销挂单...")
                open_orders = self.exchange.fetch_open_orders(self.market_symbol)
                for order in open_orders:
                    try:
                        self.exchange.cancel_order(order['id'], self.market_symbol)
                    except: pass
                self.log("[停止] 挂单已清理")
                
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
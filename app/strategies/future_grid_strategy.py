# app/strategies/future_grid_strategy.py
import ccxt
import time
import math
import os                   
import importlib.util       

class FutureGridBot:
    def __init__(self, config, logger_func):
        self.config = config
        self.log = logger_func
        self.exchange = None
        self.grids = []
        self.running = False
        self.paused = False 
        self.market_symbol = None 
        
        # --- Phase 3: 智能轮询状态机 ---
        self.last_sync_time = 0      # 上次同步账户的时间
        self.last_grid_idx = -1      # 上次计算所在的网格索引
        self.force_sync = True       # 强制同步标志位
        self.sync_interval = 15      # 账户同步心跳 (秒)
        # -----------------------------
        
        # 前端交互的核心数据结构
        self.status_data = {
            "current_grid_idx": -1,
            "profit": 0,           
            "orders": [],          
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
            
            # 1. 尝试从前端配置读取
            api_key = self.config.get('api_key', '')
            secret = self.config.get('secret', '')
            password = self.config.get('password', '')

            # 2. 从外部绝对路径加载密钥舱 (GitHub 脱敏)
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
                else:
                    pass

            # 3. 构造交易所参数
            params = {
                'apiKey': api_key,
                'secret': secret,
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'}, 
                'timeout': 30000
            }
            if password:
                params['password'] = password

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

    def sync_account_data(self):
        """同步账户数据：包含持仓详情与费率 (增加空值校验)"""
        if not self.running or not self.exchange.apiKey: return

        try:
            # 1. 获取持仓与账户信息
            positions = self.exchange.fetch_positions([self.market_symbol])
            found_pos = False
            
            for pos in positions:
                if pos['symbol'] == self.market_symbol:
                    # 使用 or 0 确保 None 会被转换为 0
                    self.status_data['current_pos'] = self._get_position_amount(pos['info'])
                    self.status_data['entry_price'] = float(pos.get('entryPrice') or 0)
                    self.status_data['liquidation_price'] = float(pos.get('liquidationPrice') or 0)
                    self.status_data['unrealized_pnl'] = float(pos.get('unrealizedPnl') or 0)
                    found_pos = True
                    break
            
            if not found_pos: 
                self.status_data['current_pos'] = 0
                self.status_data['entry_price'] = 0
                self.status_data['liquidation_price'] = 0
                self.status_data['unrealized_pnl'] = 0

            # 2. 获取余额
            balance = self.exchange.fetch_balance()
            quote_currency = self.config['symbol'].split('/')[1] 
            if quote_currency in balance:
                self.status_data['wallet_balance'] = float(balance[quote_currency].get('total', 0) or 0)
            elif 'total' in balance and quote_currency in balance['total']:
                self.status_data['wallet_balance'] = float(balance['total'][quote_currency] or 0)

            # 3. 获取资金费率
            try:
                funding_info = self.exchange.fetch_funding_rate(self.market_symbol)
                self.status_data['funding_rate'] = float(funding_info.get('fundingRate', 0) or 0)
            except:
                pass 
            
            self.last_sync_time = time.time() 
            
        except Exception as e:
            self.log(f"[数据同步失败] {e}")

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

    def check_risk_management(self):
        current_price = self.status_data['last_price']
        if current_price <= 0: return False

        stop_loss = self.config.get('stop_loss')
        take_profit = self.config.get('take_profit')
        mode = self.config.get('strategy_type', 'neutral')

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

    def calculate_grid_index(self, price):
        if price == 0: return -1
        grid_idx = -1
        for i, p in enumerate(self.grids):
            if price >= p: grid_idx = i
            else: break
        
        if grid_idx < 0: grid_idx = 0 
        if grid_idx >= len(self.grids): grid_idx = len(self.grids) - 1 
        return grid_idx

    def calculate_target_position(self, grid_idx):
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

        return target_pos

    def _to_precision(self, price=None, amount=None):
        if not self.exchange: return str(price) if price else str(amount)
        try:
            if price is not None:
                return self.exchange.price_to_precision(self.market_symbol, price)
            if amount is not None:
                return self.exchange.amount_to_precision(self.market_symbol, amount)
        except:
            pass
        return str(price) if price else str(amount)

    def adjust_position(self, target_pos):
        """【Final Fix】纠偏逻辑：空值防御 + 死锁解除"""
        current_pos = self.status_data['current_pos']
        amount_per_grid = float(self.config['amount'])
        
        diff = target_pos - current_pos
        
        if abs(diff) < (amount_per_grid * 0.5):
            return

        side = 'buy' if diff > 0 else 'sell'
        qty = abs(diff)
        
        # --- 模拟环境 ---
        if not self.exchange.apiKey:
            self.log(f"[模拟纠偏] 目标{target_pos:.4f} 实持{current_pos:.4f} -> 市价{side} {qty:.4f}")
            self.status_data['current_pos'] += diff
            if self.status_data['current_pos'] != 0:
                self.status_data['entry_price'] = self.status_data['last_price']
            return

        # --- 实盘环境 (IOC 限价单) ---
        try:
            self.log(f"[系统纠偏] 偏离检测! 正在{side} {qty:.4f}")
            
            ticker = self.exchange.fetch_ticker(self.market_symbol)
            
            # 【修复】数据兜底，防止 NoneType 崩溃
            # 优先用 ask/bid，没有则用 last，最后用本地缓存价格
            if side == 'buy':
                base_price = float(ticker.get('ask') or ticker.get('last') or self.status_data['last_price'])
                limit_price = base_price * 1.002 # 0.2% 滑点保护
            else:
                base_price = float(ticker.get('bid') or ticker.get('last') or self.status_data['last_price'])
                limit_price = base_price * 0.998

            price_str = self._to_precision(price=limit_price)
            qty_str = self._to_precision(amount=qty)
            params = {'timeInForce': 'IOC'} 
            
            order = self.exchange.create_order(
                symbol=self.market_symbol, 
                type='limit', side=side, amount=qty_str, price=price_str, params=params
            )
            
            filled = float(order.get('filled', 0))
            if filled > 0:
                self.log(f"[成交确认] IOC订单成交，数量: {filled}")
                time.sleep(0.5) 
                self.sync_account_data() # 立即刷新前端
            else:
                self.log(f"[未成交] IOC订单被取消")

            # 【核心修复】无论成交与否，都必须关闭强制同步，防止死循环
            self.force_sync = False 

        except Exception as e:
            err_msg = str(e).lower()
            if "insufficient" in err_msg or "margin" in err_msg:
                self.log(f"[严重错误] 保证金不足！策略急停。")
                self.stop() 
            else:
                self.log(f"[纠偏失败] {e}")
                # 【核心修复】报错也要释放锁，否则 Monitor 线程会卡死
                self.force_sync = False 

    def manage_maker_orders(self, current_grid_idx):
        if not self.exchange.apiKey: 
            self.update_orders_display(current_grid_idx)
            return

        try:
            active_limit = int(self.config.get('active_order_limit', 5))
            amount = float(self.config['amount'])
            
            buy_indices = [i for i in range(current_grid_idx - 1, current_grid_idx - 1 - active_limit, -1) if i >= 0]
            sell_indices = [i for i in range(current_grid_idx + 1, current_grid_idx + 1 + active_limit) if i < len(self.grids)]
            
            target_buy_prices = {self.grids[i] for i in buy_indices}
            target_sell_prices = {self.grids[i] for i in sell_indices}
            
            open_orders = self.exchange.fetch_open_orders(self.market_symbol)
            
            to_cancel_ids = []
            active_buy_prices = set()
            active_sell_prices = set()

            for order in open_orders:
                price = float(order['price'])
                oid = order['id']
                side = order['side']
                is_valid = False
                
                if side == 'buy':
                    for tp in target_buy_prices:
                        if abs(price - tp) < (tp * 0.0001):
                            active_buy_prices.add(tp)
                            is_valid = True
                            break
                elif side == 'sell':
                    for tp in target_sell_prices:
                        if abs(price - tp) < (tp * 0.0001):
                            active_sell_prices.add(tp)
                            is_valid = True
                            break
                
                if not is_valid: to_cancel_ids.append(oid)
            
            to_create_specs = [] 
            for idx in buy_indices:
                p = self.grids[idx]
                if p not in active_buy_prices: to_create_specs.append(('buy', p))
            for idx in sell_indices:
                p = self.grids[idx]
                if p not in active_sell_prices: to_create_specs.append(('sell', p))

            def exec_cancel(order_ids):
                for oid in order_ids:
                    try:
                        self.exchange.cancel_order(oid, self.market_symbol)
                        time.sleep(0.05)
                    except: pass

            def exec_create(specs):
                created = False
                for side, price in specs:
                    try:
                        price_str = self._to_precision(price=price)
                        amt_str = self._to_precision(amount=amount)
                        self.exchange.create_order(self.market_symbol, 'limit', side, amt_str, price_str)
                        time.sleep(0.05)
                        created = True
                    except Exception as e:
                        raise e
                return created

            try:
                if to_create_specs: 
                    if exec_create(to_create_specs):
                        self.force_sync = True 

                if to_cancel_ids: 
                    exec_cancel(to_cancel_ids)
                    
            except Exception as e:
                if "insufficient" in str(e).lower() or "margin" in str(e).lower():
                    self.log(f"[资金优化] 保证金紧张，执行先撤后补...")
                    if to_cancel_ids: exec_cancel(to_cancel_ids)
                else:
                    self.log(f"[挂单异常] {e}")

            self.update_orders_display(current_grid_idx)
            
        except Exception as e:
            self.log(f"[挂单维护崩溃] {e}")

    def update_orders_display(self, current_idx):
        orders = []
        try:
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
                    
                orders.append({
                    "idx": i, "price": price, "type": order_type, "amt": amount, "style": style
                })
            
            self.status_data['orders'] = orders 
        except Exception as e:
            self.log(f"[显示更新错误] {e}")

    def run_step(self, current_price):
        if not self.running: return
        self.status_data['last_price'] = current_price
        
        if self.paused: return 
        
        if not self.exchange.apiKey:
            self.sim_calculate_pnl()
            idx = self.calculate_grid_index(current_price)
            target_pos = self.calculate_target_position(idx)
            self.adjust_position(target_pos)
            self.update_orders_display(idx)
            return

        if self.check_risk_management(): return

        now = time.time()
        new_grid_idx = self.calculate_grid_index(current_price)
        self.status_data['current_grid_idx'] = new_grid_idx
        
        should_sync = False
        
        if self.force_sync:
            should_sync = True
        elif new_grid_idx != self.last_grid_idx:
            should_sync = True
        elif (now - self.last_sync_time) > self.sync_interval:
            should_sync = True

        if should_sync:
            self.sync_account_data()
            target_pos = self.calculate_target_position(new_grid_idx)
            self.adjust_position(target_pos)
            self.manage_maker_orders(new_grid_idx)
            
            self.last_grid_idx = new_grid_idx
            self.last_sync_time = now
            # force_sync 的关闭已下放到 adjust_position 内部处理

    def start(self):
        if self.init_exchange() and self.setup_account() and self.generate_grids():
            self.running = True
            self.paused = False
            self.force_sync = True 
            self.last_grid_idx = -1
            
            start_price = 0
            try:
                ticker = self.exchange.fetch_ticker(self.market_symbol)
                start_price = float(ticker['last'])
                self.status_data['last_price'] = start_price
                idx = self.calculate_grid_index(start_price)
                self.update_orders_display(idx)
                self.log(f"[系统] 初始挂单墙已生成，当前价: {start_price}")
            except Exception as e:
                self.log(f"[警告] 初始价格获取延迟: {e}")
                self.update_orders_display(-1)
            
            mode = self.config.get('strategy_type')
            self.log(f"[合约] 策略启动 (Phase 3 Engine) | 模式: {mode}")

            if start_price > 0:
                self.log("[系统] 正在执行首单建仓...")
                self.run_step(start_price)

        else:
            self.running = False

    def pause(self):
        self.paused = True
        self.log("[指令] 策略已暂停！")
        if self.exchange.apiKey:
            try:
                open_orders = self.exchange.fetch_open_orders(self.market_symbol)
                for order in open_orders:
                    try: self.exchange.cancel_order(order['id'], self.market_symbol)
                    except: pass
                self.log("[系统] 挂单已全部撤销")
            except Exception as e: self.log(f"[暂停撤单失败] {e}")

    def resume(self):
        self.paused = False
        self.force_sync = True 
        self.log("[指令] 策略恢复运行！")

    def stop(self):
        self.log("[指令] 正在停止... 撤单并平仓")
        self.running = False 
        self.paused = False
        if self.exchange.apiKey:
            try:
                open_orders = self.exchange.fetch_open_orders(self.market_symbol)
                for order in open_orders:
                    try: self.exchange.cancel_order(order['id'], self.market_symbol)
                    except: pass
                
                positions = self.exchange.fetch_positions([self.market_symbol])
                for pos in positions:
                    if pos['symbol'] == self.market_symbol:
                        amt = self._get_position_amount(pos['info'])
                        if amt != 0:
                            side = 'sell' if amt > 0 else 'buy'
                            self.exchange.create_order(self.market_symbol, 'market', side, abs(amt))
                            self.log(f"[系统] 已平仓 {amt}")
            except Exception as e: self.log(f"[停止过程出错] {e}")
        else:
            self.status_data['current_pos'] = 0
            self.log("[模拟] 已重置虚拟持仓")
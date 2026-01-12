# app/strategies/future_grid_strategy.py
import ccxt
import time
import math
import os
import importlib.util
import threading
import random  # 用于模拟模式下的价格波动

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
        self.last_sync_time = 0
        self.last_grid_idx = -1
        self.force_sync = True
        self.sync_interval = 15
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

    def init_exchange(self):
        try:
            exchange_id = self.config.get('exchange_id', 'binance')
            exchange_class = getattr(ccxt, exchange_id)
            
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
        if not self.running or not self.exchange.apiKey: return

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
            
            if not found_pos: 
                self.status_data['current_pos'] = 0
                self.status_data['entry_price'] = 0
                self.status_data['liquidation_price'] = 0
                self.status_data['unrealized_pnl'] = 0

            balance = self.exchange.fetch_balance()
            quote_currency = self.config['symbol'].split('/')[1] 
            if quote_currency in balance['total']:
                self.status_data['wallet_balance'] = float(balance['total'].get(quote_currency, 0))

            try:
                funding_info = self.exchange.fetch_funding_rate(self.market_symbol)
                raw_rate = float(funding_info.get('fundingRate', 0) or 0)
                self.status_data['funding_rate'] = round(raw_rate * 100, 4)
            except:
                self.status_data['funding_rate'] = 0
            
            self.status_data['liquidation'] = self.status_data['liquidation_price']

            if self.status_data['current_pos'] != 0 and self.status_data['entry_price'] > 0:
                if self.status_data['liquidation_price'] <= 0:
                    leverage = int(self.config.get('leverage', 1))
                    entry = self.status_data['entry_price']
                    if self.status_data['current_pos'] > 0:
                        liq = entry * (1 - 1/leverage + 0.005)
                    else:
                        liq = entry * (1 + 1/leverage - 0.005)
                    liq = round(liq, 4 if entry > 1 else 2)
                    self.status_data['liquidation_price'] = liq
                    self.status_data['liquidation'] = liq
                    self.log(f"[风控] API强平价无效，手动计算 ≈ {liq}")

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

            self.status_data['liquidation'] = self.status_data['liquidation_price']
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
            # === 修改点 1：挂单优先逻辑 (Maker Centric) ===
            # 原逻辑: hold_grids = total_grids - grid_idx (库存优先)
            # 新逻辑: total_grids - (grid_idx + 1)
            # 含义：放弃当前格子的库存，只持有更下方格子的货。当前格留给 Limit Buy 挂单。
            hold_grids = total_grids - (grid_idx + 1)
            if hold_grids < 0: hold_grids = 0
            target_pos = hold_grids * amount_per_grid
            
        elif mode == 'short':
            # === 修改点 2：挂单优先逻辑 (Maker Centric) ===
            # 原逻辑: hold_grids = grid_idx (库存优先)
            # 新逻辑: grid_idx - 1
            # 含义：放弃当前格子的空单，只持有更上方格子的空单。当前格留给 Limit Sell 挂单。
            hold_grids = grid_idx - 1
            if hold_grids < 0: hold_grids = 0
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
        current_pos = self.status_data['current_pos']
        amount_per_grid = float(self.config['amount'])
        
        # === 核心修正：基于网格单位的整倍数计算 (Integer Quantization) ===
        # 1. 计算原始浮点偏差
        raw_diff = target_pos - current_pos
        
        # 2. 计算缺失的“完整格子数” (四舍五入)
        # 这里的 amount_per_grid 就是你设置的通用最小单位 (如 0.01 或 0.002)
        # 0.0099 -> 1格;  0.004 -> 0格;  0.021 -> 2格
        missing_grids = round(raw_diff / amount_per_grid)
        
        # 3. 完美防抖：如果偏差不足半个格子，round后为0，直接跳过
        if missing_grids == 0:
            return

        # 4. 重构下单数量：必须是 config['amount'] 的整数倍
        side = 'buy' if missing_grids > 0 else 'sell'
        qty = abs(missing_grids) * amount_per_grid
        
        # ==========================================================

        if not self.exchange.apiKey:
            self.log(f"[模拟纠偏] 目标{target_pos:.4f} 实持{current_pos:.4f} -> 修正{abs(missing_grids)}格 -> 市价{side} {qty:.4f}")
            self.status_data['current_pos'] += (missing_grids * amount_per_grid)
            if self.status_data['current_pos'] != 0:
                self.status_data['entry_price'] = self.status_data['last_price']
            return

        try:
            self.log(f"[系统纠偏] 偏差{raw_diff:.4f} -> 修正{abs(missing_grids)}格 -> 正在市价{side} {qty:.4f}")
            
            qty_str = self._to_precision(amount=qty)
            
            # 回归最简模式：使用市价单 (Market)
            # 只要 qty 是 0.01 的标准倍数，OKX 市价单是可以成交的
            order = self.exchange.create_order(
                symbol=self.market_symbol,
                type='market',
                side=side,
                amount=qty_str
            )

            # 保持双重确认逻辑，确保数据准确
            order_id = order['id']
            time.sleep(0.5) 
            full_order = self.exchange.fetch_order(order_id, self.market_symbol)
            filled = float(full_order.get('filled', 0))
            
            if filled > 0:
                self.log(f"[成交确认] 纠偏成功，数量: {filled:.4f}")
                time.sleep(0.5)
                self.sync_account_data()
            else:
                self.log(f"[未成交] 市价单未立即返回成交量，等待同步")

            self.force_sync = True 

        except Exception as e:
            err_msg = str(e).lower()
            if "insufficient" in err_msg or "margin" in err_msg:
                self.log(f"[严重错误] 保证金不足！策略急停。")
                self.stop()
            else:
                self.log(f"[纠偏失败] {e}")
                self.force_sync = True

    def manage_maker_orders(self, current_grid_idx):
        if not self.exchange.apiKey: 
            self.update_orders_display(current_grid_idx)
            return

        try:
            active_limit = int(self.config.get('active_order_limit', 5))
            amount = float(self.config['amount'])
            
            # === 修改点：完全重构挂单偏移逻辑 (Maker Centric v2) ===
            mode = self.config.get('strategy_type', 'neutral')
            
            # 默认偏移 (适用于 Neutral 或传统逻辑)
            # 买单从下一格开始(-1), 卖单从上一格开始(+1)
            buy_start_offset = -1
            sell_start_offset = 1
            
            if mode == 'long':
                # Long 模式
                # 1. 放弃当前格持仓 -> 必须在当前格挂买单兜底 (Offset 0)
                # 2. 持仓从上一格(idx+1, 90800)开始 -> 卖单从上上格(idx+2, 91200)开始
                #    结果：90400挂买，90800空档，91200挂卖
                buy_start_offset = 0
                sell_start_offset = 2
            
            elif mode == 'short':
                # Short 模式
                # 1. 放弃当前格空单 -> 必须在当前格挂卖单开空 (Offset 1, 因idx是floor, 90800是idx+1)
                #    注意：做空时，90400(idx)的上沿是90800(idx+1)。所以卖单从+1开始是对的。
                # 2. 持仓从下一格(idx-1, 90000)开始 -> 买单(平空)从下下格(idx-2, 89600)开始
                #    结果：89600挂买，90000空档，90800挂卖
                buy_start_offset = -2
                sell_start_offset = 1
            
            # 计算起始索引
            start_buy = current_grid_idx + buy_start_offset
            start_sell = current_grid_idx + sell_start_offset
            
            # 生成挂单索引列表
            buy_indices = [i for i in range(start_buy, current_grid_idx - 1 - active_limit, -1) if i >= 0]
            sell_indices = [i for i in range(start_sell, current_grid_idx + 1 + active_limit) if i < len(self.grids)]
            
            # ====================================================
            
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

                idx = self.calculate_grid_index(start_price)
                self.update_orders_display(idx)
                self.log(f"[系统] 初始挂单墙已生成，当前价: {start_price}")
            except Exception as e:
                self.log(f"[警告] 初始价格获取失败: {e}")
                self.update_orders_display(-1)

            mode = self.config.get('strategy_type', 'neutral')
            self.log(f"[合约] 策略初始化完成 (Phase 3 Engine) | 模式: {mode}")

            if start_price > 0:
                self.log("[系统] 执行首单建仓...")
                self.run_step(start_price)

            self._main_loop()

        except Exception as e:
            self.log(f"[初始化严重错误] {e}，策略无法启动")
            self.running = False

    def start(self):
        if self.running:
            self.log("[警告] 策略已在运行中")
            return

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
        if self.exchange and self.exchange.apiKey:
            try:
                open_orders = self.exchange.fetch_open_orders(self.market_symbol)
                for order in open_orders:
                    try: self.exchange.cancel_order(order['id'], self.market_symbol)
                    except: pass
                self.log("[系统] 挂单已全部撤销")
            except Exception as e: 
                self.log(f"[暂停撤单失败] {e}")

    def resume(self):
        self.paused = False
        self.force_sync = True 
        self.log("[指令] 策略恢复运行！")

    def stop(self):
        self.log("[指令] 正在停止... 撤单并平仓")
        self.running = False 
        self.paused = False

        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=15)

        if self.exchange and self.exchange.apiKey:
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
            except Exception as e:
                self.log(f"[停止过程出错] {e}")
        else:
            self.status_data['current_pos'] = 0
            self.log("[模拟] 已重置虚拟持仓")
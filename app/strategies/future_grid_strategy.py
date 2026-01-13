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
            # [新增] 缓存关键参数供新逻辑使用
            self.grid_step = step
            self.grid_count = num

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

    # ==================================================================
    # [新增] Phase 4: 推窗/队列平移核心逻辑组件
    # ==================================================================
    def _cancel_all_orders(self):
        """[新增] 清空当前交易对的所有挂单"""
        if not self.exchange or not self.exchange.apiKey: return
        try:
            self.exchange.cancel_all_orders(self.market_symbol)
            self.active_orders = {'buy': {}, 'sell': {}}
        except Exception:
            # 兼容不支持 cancel_all 的情况
            orders = self.exchange.fetch_open_orders(self.market_symbol)
            for o in orders:
                try: self.exchange.cancel_order(o['id'], self.market_symbol)
                except: pass
            self.active_orders = {'buy': {}, 'sell': {}}

    def _place_order_safe(self, side, price):
        """[新增] 安全下单包装函数"""
        if not self.exchange or not self.exchange.apiKey: return
        
        # 价格对齐 (假设最小Step)
        price = round(price / self.grid_step) * self.grid_step
        
        # 本地防重
        if price in self.active_orders[side]:
            return
        
        try:
            price_str = self._to_precision(price=price)
            amt_str = self._to_precision(amount=self.order_qty)
            
            order = self.exchange.create_order(
                self.market_symbol, 'limit', side, amt_str, price_str
            )
            self.active_orders[side][price] = order['id']
            # self.log(f"✅ 挂单: {side} @ {price}") 
        except Exception as e:
            self.log(f"🛑 下单失败 [{side} {price}]: {e}")

    def _cancel_order_by_price(self, side, price):
        """[新增] 根据价格查找并撤销订单"""
        target_id = None
        target_price_key = None
        
        # 模糊匹配价格
        for p, oid in self.active_orders[side].items():
            if math.isclose(float(p), float(price), abs_tol=self.grid_step * 0.1):
                target_id = oid
                target_price_key = p
                break
        
        if target_id:
            try:
                self.exchange.cancel_order(target_id, self.market_symbol)
                del self.active_orders[side][target_price_key]
                # self.log(f"♻️ 撤单: {side} @ {price}")
            except Exception as e:
                # 订单可能已消失，清理本地记录
                if "NotFound" in str(e) or "Unknown" in str(e):
                    if target_price_key in self.active_orders[side]:
                        del self.active_orders[side][target_price_key]
                else:
                    self.log(f"⚠️ 撤单失败: {e}")

    def initialize_grid_orders(self, current_price):
        """
        [新增] 启动/纠偏时的静态挂单墙生成
        注意：此处直接复用了旧逻辑(manage_maker_orders)中的 Offset 策略来确定空档(Gap)，
        确保在 Long 模式下空档定在上方，Short 模式下空档定在下方。
        """
        self.log(f"⚡ 正在计算初始网格模型 (Strategy Aware)...")
        self._cancel_all_orders()
        
        # 1. 计算基础网格索引 (复用旧逻辑)
        grid_idx = self.calculate_grid_index(current_price)
        
        # 2. 根据策略模式确定 Gap 位置 (复用 manage_maker_orders 的思想)
        mode = self.config.get('strategy_type', 'neutral')
        
        # 默认 Gap (Neutral)
        gap_idx = grid_idx 
        
        if mode == 'long':
            # Long 模式:
            # 旧逻辑中 buy_start = idx, sell_start = idx + 2
            # 意味着中间的 idx + 1 是空档 (Gap)
            gap_idx = grid_idx + 1
            if gap_idx >= len(self.grids): gap_idx = len(self.grids) - 1

        elif mode == 'short':
            # Short 模式:
            # 旧逻辑中 buy_start = idx - 1, sell_start = idx + 1
            # 意味着中间的 idx 是空档 (Gap)
            gap_idx = grid_idx
        
        else:
            # Neutral: 使用四舍五入寻找最近的网格线
            min_dist = float('inf')
            best_i = 0
            for i, p in enumerate(self.grids):
                if abs(p - current_price) < min_dist:
                    min_dist = abs(p - current_price)
                    best_i = i
            gap_idx = best_i

        # 3. 确定空档价格
        self.gap_price = self.grids[gap_idx]
        self.log(f"📍 初始空档锁定: {self.gap_price} (模式: {mode}, 现价: {current_price})")
        
        # 4. 生成挂单
        active_limit = int(self.config.get('active_order_limit', 5))
        
        # 下方挂买 (Gap - N*Step)
        for i in range(1, active_limit + 1):
            p = self.gap_price - (i * self.grid_step)
            self._place_order_safe('buy', p)
            
        # 上方挂卖 (Gap + N*Step)
        for i in range(1, active_limit + 1):
            p = self.gap_price + (i * self.grid_step)
            self._place_order_safe('sell', p)
            
        self.update_orders_display_from_memory()

    def _process_grid_shift(self, filled_order):
        """[新增] 推窗逻辑：仅在成交时触发"""
        with self.state_lock:
            side = filled_order['side']
            fill_price = float(filled_order['price'])
            amount = float(filled_order['amount'])
            
            # 更新状态：成交价即为新空档
            old_gap = self.gap_price
            new_gap = fill_price
            self.gap_price = new_gap
            
            self.log(f"🔔 成交 {side} {amount} @ {fill_price} | 空档移动: {old_gap} -> {new_gap}")
            
            active_limit = int(self.config.get('active_order_limit', 5))
            
            if side == 'sell':
                # 卖成交 -> 上移
                target_buy = new_gap - self.grid_step
                self._place_order_safe('buy', target_buy)
                
                target_top_sell = new_gap + (active_limit * self.grid_step)
                self._place_order_safe('sell', target_top_sell)
                
                remove_buy = new_gap - ((active_limit + 1) * self.grid_step)
                self._cancel_order_by_price('buy', remove_buy)
                
            elif side == 'buy':
                # 买成交 -> 下移
                target_sell = new_gap + self.grid_step
                self._place_order_safe('sell', target_sell)
                
                target_bottom_buy = new_gap - (active_limit * self.grid_step)
                self._place_order_safe('buy', target_bottom_buy)
                
                remove_sell = new_gap + ((active_limit + 1) * self.grid_step)
                self._cancel_order_by_price('sell', remove_sell)
            
            self.update_orders_display_from_memory()

    def _check_order_status(self):
        """[新增] 订单状态轮询"""
        if not self.exchange or not self.exchange.apiKey: return

        try:
            # 获取当前交易所挂单
            open_orders = self.exchange.fetch_open_orders(self.market_symbol)
            open_ids = [o['id'] for o in open_orders]
            
            # 找出本地记录中存在，但交易所已不存在的订单
            filled_candidates = []
            
            for side in ['buy', 'sell']:
                # 使用 list() 复制 keys 避免遍历时修改字典
                for price, oid in list(self.active_orders[side].items()):
                    if oid not in open_ids:
                        filled_candidates.append({'id': oid, 'side': side, 'price': price})
            
            for candidate in filled_candidates:
                try:
                    order_detail = self.exchange.fetch_order(candidate['id'], self.market_symbol)
                    status = order_detail['status']
                    
                    if status == 'closed': 
                        # 成交 -> 触发推窗
                        price_key = candidate['price']
                        if price_key in self.active_orders[candidate['side']]:
                            del self.active_orders[candidate['side']][price_key]
                        
                        self._process_grid_shift(order_detail)
                        self.sync_account_data()
                        
                    elif status == 'canceled': 
                        # 撤销 -> 仅清理本地
                        self.log(f"⚠️ 发现外部撤单: {candidate['side']}")
                        if candidate['price'] in self.active_orders[candidate['side']]:
                            del self.active_orders[candidate['side']][candidate['price']]
                            
                except Exception as e:
                    self.log(f"查单失败: {e}")
                    
        except Exception as e:
            self.log(f"状态轮询异常: {e}")

    def update_orders_display_from_memory(self):
        """[新增] 从内存 active_orders 生成前端显示数据"""
        try:
            orders = []
            amount = self.config['amount']
            
            # 估算当前 index
            current_idx = -1
            for i, p in enumerate(self.grids):
                if math.isclose(p, self.gap_price, abs_tol=self.grid_step*0.1):
                    current_idx = i
                    break
            
            self.status_data['current_grid_idx'] = current_idx

            for i in range(len(self.grids)-1, -1, -1):
                p = self.grids[i]
                order_type = "---"
                style = "text-muted"
                
                is_buy = False
                is_sell = False
                
                for bp in self.active_orders['buy'].keys():
                    if math.isclose(float(bp), p, abs_tol=0.1): is_buy = True
                
                for sp in self.active_orders['sell'].keys():
                    if math.isclose(float(sp), p, abs_tol=0.1): is_sell = True
                
                if math.isclose(p, self.gap_price, abs_tol=self.grid_step*0.1):
                    style = "text-warning bg-dark border border-warning"
                    order_type = "⚡ 空档(GAP) ⚡"
                elif is_sell:
                    order_type = "SELL (挂单)"
                    style = "text-danger"
                elif is_buy:
                    order_type = "BUY (挂单)"
                    style = "text-success"
                    
                orders.append({
                    "idx": i, "price": p, "type": order_type, "amt": amount, "style": style
                })
            
            self.status_data['orders'] = orders
        except Exception as e:
            pass

    # ==================================================================

    def adjust_position(self, target_pos):
        current_pos = self.status_data['current_pos']
        amount_per_grid = float(self.config['amount'])
        
        # 1. 计算原始浮点偏差 (例如: 目标10, 实持9.999 -> diff=0.001)
        raw_diff = target_pos - current_pos
        
        # 2. 核心取整逻辑 (依然保留)
        # 将浮点数偏差转化为"缺几个格子"
        # 0.001 -> 0;  0.9 -> 1;  -2.1 -> -2
        missing_grids = round(raw_diff / amount_per_grid)
        
        # 3. 核心防抖逻辑 (Tolerance Level 3)
        # 这是一个巨大的过滤器。
        # 只要缺失的格子数绝对值 < 3，说明：
        # - 要么是浮点误差 (0)
        # - 要么是Gap策略的缓冲区 (1)
        # - 要么是刚突破时的成交延迟 (2)
        # 这些情况统统不需要纠偏。
        if abs(missing_grids) < 3:
            return

        # 4. 执行纠偏 (仅在严重失衡 >=3 时触发)
        # 既然已经严重失衡，必须使用市价单(Market)雷霆手段瞬间拉回，
        # 绝不能再磨磨唧唧挂限价单。
        side = 'buy' if missing_grids > 0 else 'sell'
        qty = abs(missing_grids) * amount_per_grid
        
        if not self.exchange.apiKey:
            self.log(f"[模拟纠偏] 目标{target_pos:.4f} 实持{current_pos:.4f} -> 修正{abs(missing_grids)}格 -> 市价{side} {qty:.4f}")
            self.status_data['current_pos'] += (missing_grids * amount_per_grid)
            if self.status_data['current_pos'] != 0:
                self.status_data['entry_price'] = self.status_data['last_price']
            return

        try:
            self.log(f"[系统纠偏] 严重失衡(diff={abs(missing_grids)}格) -> 正在市价{side} {qty:.4f}")
            
            qty_str = self._to_precision(amount=qty)
            
            # 使用市价单确保立即成交
            order = self.exchange.create_order(
                symbol=self.market_symbol,
                type='market',
                side=side,
                amount=qty_str
            )

            order_id = order['id']
            time.sleep(0.5) 
            full_order = self.exchange.fetch_order(order_id, self.market_symbol)
            filled = float(full_order.get('filled', 0))
            
            if filled > 0:
                self.log(f"[纠偏成功] 已强制{side} {filled:.4f}")
                time.sleep(0.5)
                self.sync_account_data()
                # [新增] 纠偏后网格状态已乱，调用智能初始化重新铺设网格
                # 注意：这里调用的是修改后的 initialize_grid_orders，它会自动处理 Long/Short 的 Gap 对齐
                self.initialize_grid_orders(self.status_data['last_price'])
            else:
                self.log(f"[纠偏警告] 市价单已发但未立即返回成交量")

            self.force_sync = True 

        except Exception as e:
            err_msg = str(e).lower()
            if "insufficient" in err_msg or "margin" in err_msg:
                self.log(f"[严重错误] 保证金不足，无法纠偏！策略停止。")
                self.stop()
            else:
                self.log(f"[纠偏失败] {e}")
                self.force_sync = True

    def manage_maker_orders(self, current_grid_idx):
        # [修改] 强制屏蔽旧逻辑，防止死循环震荡。保留函数壳以防Crash。
        return

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
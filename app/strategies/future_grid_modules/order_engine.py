# app/strategies/future_grid_modules/order_engine.py
import time
import math

class FutureGridOrderMixin:
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
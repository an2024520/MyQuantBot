# app/strategies/components/order_sync.py
import time

class OrderSync:
    def __init__(self, exchange, config, logger):
        self.exchange = exchange
        self.config = config
        self.log = logger
        self.orders = [] # 用于前端显示

    def _to_precision(self, market_symbol, price=None, amount=None):
        if not self.exchange: return str(price) if price else str(amount)
        try:
            if price is not None:
                return self.exchange.price_to_precision(market_symbol, price)
            if amount is not None:
                return self.exchange.amount_to_precision(market_symbol, amount)
        except:
            pass
        return str(price) if price else str(amount)

    def update_orders_display(self, current_idx, grids):
        orders = []
        try:
            amount = self.config['amount']
            active_limit = int(self.config.get('active_order_limit', 5))
            
            for i in range(len(grids)-1, -1, -1):
                price = grids[i]
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
            
            self.orders = orders 
        except Exception as e:
            self.log(f"[显示更新错误] {e}")

    def manage_maker_orders(self, current_grid_idx, grids, market_symbol):
        if not self.exchange.apiKey: 
            self.update_orders_display(current_grid_idx, grids)
            return

        try:
            active_limit = int(self.config.get('active_order_limit', 5))
            amount = float(self.config['amount'])
            
            buy_indices = [i for i in range(current_grid_idx - 1, current_grid_idx - 1 - active_limit, -1) if i >= 0]
            sell_indices = [i for i in range(current_grid_idx + 1, current_grid_idx + 1 + active_limit) if i < len(grids)]
            
            target_buy_prices = {grids[i] for i in buy_indices}
            target_sell_prices = {grids[i] for i in sell_indices}
            
            open_orders = self.exchange.fetch_open_orders(market_symbol)
            
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
                p = grids[idx]
                if p not in active_buy_prices: to_create_specs.append(('buy', p))
            for idx in sell_indices:
                p = grids[idx]
                if p not in active_sell_prices: to_create_specs.append(('sell', p))

            def exec_cancel(order_ids):
                for oid in order_ids:
                    try:
                        self.exchange.cancel_order(oid, market_symbol)
                        time.sleep(0.05)
                    except: pass

            def exec_create(specs):
                created = False
                for side, price in specs:
                    try:
                        price_str = self._to_precision(market_symbol, price=price)
                        amt_str = self._to_precision(market_symbol, amount=amount)
                        self.exchange.create_order(market_symbol, 'limit', side, amt_str, price_str)
                        time.sleep(0.05)
                        created = True
                    except Exception as e:
                        raise e
                return created

            force_sync_needed = False
            try:
                if to_create_specs: 
                    if exec_create(to_create_specs):
                        force_sync_needed = True 

                if to_cancel_ids: 
                    exec_cancel(to_cancel_ids)
                    
            except Exception as e:
                if "insufficient" in str(e).lower() or "margin" in str(e).lower():
                    self.log(f"[资金优化] 保证金紧张，执行先撤后补...")
                    if to_cancel_ids: exec_cancel(to_cancel_ids)
                else:
                    self.log(f"[挂单异常] {e}")

            self.update_orders_display(current_grid_idx, grids)
            return force_sync_needed
            
        except Exception as e:
            self.log(f"[挂单维护崩溃] {e}")
            return False
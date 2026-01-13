# app/strategies/future_grid_modules/calculation.py
import math

class FutureGridCalcMixin:
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
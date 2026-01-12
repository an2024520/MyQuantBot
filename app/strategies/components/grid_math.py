# app/strategies/components/grid_math.py

class GridMath:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger
        self.grids = []

    def generate_grids(self):
        try:
            lower = float(self.config['lower_price'])
            upper = float(self.config['upper_price'])
            num = int(self.config['grid_num'])
            if num < 2: num = 2
            
            step = (upper - lower) / num
            self.grids = [lower + i * step for i in range(num + 1)]
            
            # 动态精度
            digits = 2 if lower > 100 else (4 if lower > 1 else 6)
            self.grids = [round(g, digits) for g in self.grids]
            
            self.log(f"[网格生成] 区间 {lower}-{upper}, 共 {num} 格")
            return True
        except Exception as e:
            self.log(f"[参数错误] {e}")
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
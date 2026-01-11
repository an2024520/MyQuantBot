# grid_strategy.py
import ccxt
from datetime import datetime

class GridBot:
    def __init__(self, config, logger_func):
        self.config = config
        self.log = logger_func
        self.exchange = None
        self.grids = []
        self.running = False
        
        # 内部状态
        self.status_data = {
            "current_grid_idx": -1,
            "next_buy": 0,
            "next_sell": 0,
            "total_profit": 0,
            "grid_orders": []
        }

    def init_exchange(self):
        try:
            exchange_id = self.config.get('exchange_id', 'binance')
            exchange_class = getattr(ccxt, exchange_id)
            params = {
                'apiKey': self.config.get('api_key', ''),
                'secret': self.config.get('secret', ''),
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
                'timeout': 30000
            }
            self.exchange = exchange_class(params)
            self.log(f"[网格] 交易所 {exchange_id} 初始化完成")
            return True
        except Exception as e:
            self.log(f"[网格错误] 初始化失败: {str(e)}")
            return False

    def generate_grids(self):
        try:
            lower = float(self.config['lower_price'])
            upper = float(self.config['upper_price'])
            num = int(self.config['grid_num'])
            amount = float(self.config['amount'])
            
            if num < 2: num = 2
            step = (upper - lower) / num
            self.grids = [lower + i * step for i in range(num + 1)]
            
            # 动态精度
            digits = 2 if lower > 100 else 4
            self.grids = [round(g, digits) for g in self.grids]
            
            self.log(f"[网格] 网格线生成完毕 ({lower}-{upper})")
            return True
        except Exception as e:
            self.log(f"[网格错误] 参数异常: {str(e)}")
            return False

    def update_orders_display(self, current_idx):
        orders = []
        amount = self.config['amount']
        for i in range(len(self.grids)-1, -1, -1):
            price = self.grids[i]
            if i > current_idx:
                orders.append({"idx": i, "price": price, "type": "SELL", "amt": amount, "style": "text-danger"})
            elif i < current_idx:
                orders.append({"idx": i, "price": price, "type": "BUY", "amt": amount, "style": "text-success"})
            else:
                orders.append({"idx": i, "price": price, "type": "---", "amt": "---", "style": "text-warning bg-dark"})
        self.status_data['grid_orders'] = orders

    def run_step(self, current_price):
        """主程序轮询时调用此方法"""
        if not self.running: return

        # 1. 定位网格
        current_idx = -1
        for i, price in enumerate(self.grids):
            if current_price >= price:
                current_idx = i
            else:
                break
        
        last_idx = self.status_data['current_grid_idx']
        self.status_data['current_grid_idx'] = current_idx
        self.update_orders_display(current_idx)
        
        # 2. 交易判定
        if last_idx != -1 and current_idx != -1 and last_idx != current_idx:
            amount = float(self.config['amount'])
            if current_idx < last_idx:
                self.log(f"[买入] ⬇️ 下穿网格 #{current_idx}，模拟买入 {amount}")
            elif current_idx > last_idx:
                profit = (self.grids[1] - self.grids[0]) * amount
                self.status_data['total_profit'] += profit
                self.log(f"[卖出] ⬆️ 上穿网格 #{current_idx}，模拟获利 {profit:.4f} U")

    def start(self):
        if self.init_exchange() and self.generate_grids():
            self.running = True
            self.log("[网格] 策略已启动，正在监听主行情...")
        else:
            self.running = False

    def stop(self):
        self.running = False
        self.log("[网格] 策略已停止")
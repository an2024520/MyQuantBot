# app/strategies/future_grid_modules/risk_control.py

class FutureGridRiskMixin:
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
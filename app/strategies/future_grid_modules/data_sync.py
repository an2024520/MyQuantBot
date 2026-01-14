# app/strategies/future_grid_modules/data_sync.py
import time

class FutureGridSyncMixin:
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
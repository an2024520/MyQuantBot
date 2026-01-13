# app/strategies/future_grid_modules/initialization.py
import ccxt
import os
import importlib.util

class FutureGridInitMixin:
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
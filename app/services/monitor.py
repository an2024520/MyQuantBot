# app/services/monitor.py
import threading
import time
import ccxt
from collections import deque
from app.utils.indicators import calculate_rsi, calculate_smi

# === 全局共享数据 (替代原本的 SHARED_DATA) ===
class SharedState:
    market_data = {}  # 存储行情 { 'BTC/USDT': {...} }
    system_logs = deque(maxlen=200) # 存储日志
    
    # 监控配置
    watch_settings = {
        "BTC/USDT": "1h", "ETH/USDT": "1h", "SOL/USDT": "1h",
        "BTC/USDC": "1h", "ETH/USDC": "1h", "SOL/USDC": "1h"
    }

def add_log(msg):
    """全局日志写入"""
    ts = time.strftime("%H:%M:%S")
    log_entry = f"[{ts}] {msg}"
    SharedState.system_logs.insert(0, log_entry)
    print(log_entry) # 同时打印到控制台

def market_monitor_thread():
    """后台监控线程"""
    from app.services.bot_manager import BotManager  # 延迟导入防止循环引用
    
    exchange = ccxt.binance({'enableRateLimit': True, 'timeout': 30000})
    symbols = list(SharedState.watch_settings.keys())
    
    print(">>> [System] 智能监控服务已启动...")
    
    while True:
        try:
            for symbol in symbols:
                # 1. 获取价格
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = float(ticker['last'])
                except:
                    continue
                
                # 2. 计算指标
                tf = SharedState.watch_settings.get(symbol, '1h')
                ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=500)
                closes = [x[4] for x in ohlcv]
                
                rsi = calculate_rsi(closes)
                smi, sig = calculate_smi(closes)
                
                # 3. 更新共享状态
                SharedState.market_data[symbol] = {
                    "price": current_price,
                    "tf": tf,
                    "rsi": round(rsi, 2) if rsi else 0,
                    "smi": round(smi, 5) if smi else 0,
                    "sig": round(sig, 5) if sig else 0
                }
                
                # 4. 驱动机器人 (只驱动合约机器人)
                bot = BotManager.get_bot()
                if bot and bot.running:
                    # 只有当机器人的交易对匹配当前轮询的 symbol 时才驱动
                    if bot.config.get('symbol') == symbol:
                        try:
                            bot.run_step(current_price)
                        except Exception as e:
                            add_log(f"Bot Error: {e}")

            time.sleep(2)
            
        except Exception as e:
            print(f"[Monitor Error] {e}")
            time.sleep(5)

def start_market_monitor():
    t = threading.Thread(target=market_monitor_thread, daemon=True)
    t.start()
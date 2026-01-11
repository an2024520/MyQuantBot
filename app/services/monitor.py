# app/services/monitor.py
import threading
import time
import ccxt
from collections import deque
from app.utils.indicators import calculate_rsi, calculate_smi
from config import Config  # 【新增】引入配置

# === 全局共享数据 ===
class SharedState:
    market_data = {}  # { 'BTC/USDT': {...} }
    system_logs = deque(maxlen=200) 
    
    # 监控列表 (前端显示用)
    watch_settings = {
        "BTC/USDT": "1h", "ETH/USDT": "1h", "SOL/USDT": "1h",
        "BTC/USDC": "1h", "ETH/USDC": "1h", "SOL/USDC": "1h"
    }

def add_log(msg):
    ts = time.strftime("%H:%M:%S")
    log_entry = f"[{ts}] {msg}"
    SharedState.system_logs.insert(0, log_entry)
    print(log_entry)

def get_public_exchange():
    # ... (前文代码)
    source = getattr(Config, 'MARKET_SOURCE', 'binance')
    
    # 【必须加上这行伪装】
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    
    common_params = {
        'enableRateLimit': True, 
        'timeout': 30000,
        'userAgent': user_agent  # <--- 注入伪装头
    }
    

    if source == 'coinbase':
        print(f">>> [System] 公共行情源: Coinbase (现货/机构)")
        return ccxt.coinbase(common_params)
    
    elif source == 'okx':
        print(f">>> [System] 公共行情源: OKX (合约)")
        # OKX 特殊处理：默认看 Swap
        params = common_params.copy()
        params['options'] = {'defaultType': 'swap'}
        return ccxt.okx(params)
    
    else: # 默认 binance
        print(f">>> [System] 公共行情源: Binance")
        return ccxt.binance(common_params)

def market_monitor_thread():
    from app.services.bot_manager import BotManager
    
    # 1. 初始化 (加长超时 + 关闭日志)
    exchange = get_public_exchange()
    exchange.timeout = 120000       # 120秒超时
    exchange.verbose = False        # 禁止啰嗦
    
    symbols = list(SharedState.watch_settings.keys())
    print(">>> [System] Monitor thread started (Safe Mode).")
    
    while True:
        try:
            for display_symbol in symbols:
                # Coinbase 适配
                query_symbol = display_symbol
                if getattr(Config, 'MARKET_SOURCE', 'binance') == 'coinbase' and 'USDT' in display_symbol:
                    query_symbol = display_symbol.replace('USDT', 'USD')
                
                try:
                    # 抓取数据
                    ticker = exchange.fetch_ticker(query_symbol)
                    current_price = float(ticker['last'])
                    
                    # 抓取 K 线
                    tf = SharedState.watch_settings.get(display_symbol, '1h')
                    ohlcv = exchange.fetch_ohlcv(query_symbol, tf, limit=100) # 减少数据量到 100
                    closes = [x[4] for x in ohlcv]
                    
                    # 计算指标
                    rsi = calculate_rsi(closes)
                    smi, sig = calculate_smi(closes)
                    
                    # 更新状态
                    SharedState.market_data[display_symbol] = {
                        "price": current_price,
                        "tf": tf,
                        "rsi": round(rsi, 2) if rsi else 0,
                        "smi": round(smi, 5) if smi else 0,
                        "source": getattr(Config, 'MARKET_SOURCE', 'binance')
                    }
                    
                    # 成功获取一个就打印一个简短标记 (避免中文/特殊符号)
                    # print(f"Updated: {display_symbol}") 
                
                except Exception as inner_e:
                    # 【关键】这里不要打印 inner_e 的具体内容，防止包含乱码导致线程崩溃
                    # print("Update failed.") 
                    continue
            
            # 休息一下
            time.sleep(5)
            
        except Exception as e:
            # 【关键】全局错误也不要打印详细内容
            print("[Monitor] Global loop error (suppressed to prevent crash).")
            time.sleep(10)

def start_market_monitor():
    t = threading.Thread(target=market_monitor_thread, daemon=True)
    t.start()

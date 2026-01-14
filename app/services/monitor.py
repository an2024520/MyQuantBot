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
    target_source = getattr(Config, 'MARKET_SOURCE', 'binance') # 【新增】目标数据源 (用于热切换) 
    
    # 监控列表 (前端显示用)
    watch_settings = {
        "BTC/USDT": "1h", "ETH/USDT": "1h", "SOL/USDT": "1h",
        "BTC/USDC": "1h", "ETH/USDC": "1h", "SOL/USDC": "1h"
    }

def add_log(msg):
    ts = time.strftime("%H:%M:%S")
    log_entry = f"[{ts}] {msg}"
    # === 核心修复: insert 改为 appendleft 以支持自动滚动 ===
    # SharedState.system_logs.insert(0, log_entry)  <-- 原错误代码
    SharedState.system_logs.appendleft(log_entry)
    print(log_entry)

def get_public_exchange():
    """【新增】根据配置获取交易所实例 (工厂模式)"""
    source = getattr(Config, 'MARKET_SOURCE', 'binance')
    
    common_params = {
        'enableRateLimit': True, 
        'timeout': 30000
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
    
    # 1. 初始化交易所
    exchange = get_public_exchange()
    symbols = list(SharedState.watch_settings.keys())
    
    print(">>> [System] 智能监控服务已启动...")
    
    # 【新增】热切换检测
    current_source_name = Config.MARKET_SOURCE

    while True:
        try:
            # 0. 检查源切换
            if SharedState.target_source != current_source_name:
                add_log(f"[Monitor] 切换行情源: {current_source_name} -> {SharedState.target_source}")
                # 动态修改 Config (虽然 Config 是单例，但这里修改内存值以欺骗 get_public_exchange)
                Config.MARKET_SOURCE = SharedState.target_source
                exchange = get_public_exchange()
                current_source_name = SharedState.target_source

            for display_symbol in symbols:
                # 【新增】智能符号适配 (Smart Adapter)
                query_symbol = display_symbol
                
                # 如果是 Coinbase，它主力是 USD，这里做隐式映射
                # 前端看 BTC/USDT -> 后台查 BTC/USD
                if current_source_name == 'coinbase' and 'USDT' in display_symbol:
                    query_symbol = display_symbol.replace('USDT', 'USD')
                
                # 1. 获取价格 & 计算延迟
                latency = 0
                try:
                    t1 = time.time()
                    ticker = exchange.fetch_ticker(query_symbol)
                    t2 = time.time()
                    current_price = float(ticker['last'])
                    latency = int((t2 - t1) * 1000) # ms
                except Exception as e:
                    # 偶尔报错不打印，防止刷屏
                    continue
                
                # 2. 计算指标
                tf = SharedState.watch_settings.get(display_symbol, '1h')
                try:
                    ohlcv = exchange.fetch_ohlcv(query_symbol, tf, limit=500)
                    closes = [x[4] for x in ohlcv]
                    
                    rsi = calculate_rsi(closes)
                    smi, sig = calculate_smi(closes)
                    
                    # 3. 更新共享状态 (注意：Key 依然用 display_symbol，保持前端一致)
                    SharedState.market_data[display_symbol] = {
                        "price": current_price,
                        "tf": tf,
                        "rsi": round(rsi, 2) if rsi else 0,
                        "smi": round(smi, 5) if smi else 0,
                        "sig": round(sig, 5) if sig else 0,
                        "source": current_source_name, # 标记来源
                        "latency": latency # 【新增】延迟
                    }
                except:
                    continue
                
                # 4. 驱动机器人 (只驱动合约机器人)
                bot = BotManager.get_bot()
                if bot and bot.running:
                    # 注意：机器人自己有 fetch_market_data，这里仅作为 fallback 或触发器
                    # 实际交易中，机器人使用自己的行情源，这里不需要频繁驱动
                    pass 

            time.sleep(2)
            
        except Exception as e:
            print(f"[Monitor Error] {e}")
            time.sleep(5)

def start_market_monitor():
    t = threading.Thread(target=market_monitor_thread, daemon=True)
    t.start()
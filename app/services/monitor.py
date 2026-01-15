# app/services/monitor.py
import threading
import time
import psutil
import json
import os
import ccxt
from collections import deque
from app.utils.notifier import send_message
from app.utils.indicators import calculate_rsi, calculate_smi
from config import Config  # 【新增】引入配置

# === 全局共享数据 ===
class SharedState:
    market_data = {}  # { 'BTC/USDT': {...} }
    system_logs = deque(maxlen=200) 
    target_source = getattr(Config, 'MARKET_SOURCE', 'binance') # 【新增】目标数据源 (用于热切换) 
    
    # 监控列表 (前端显示用)
    # 监控列表 (前端显示用)
    watch_settings = {"BTC/USDT": "1h"}
    last_alert_time = 0

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

    # 初始化流量计算变量
    last_sent_bytes = 0
    last_recv_bytes = 0
    last_net_time = time.time()

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

            # === A. 获取系统状态 (System Stats) ===
            try:
                # 1. 基础硬件
                cpu = psutil.cpu_percent()
                mem = psutil.virtual_memory().percent
                disk = psutil.disk_usage('/').percent
                
                # 2. 网络流量 (实时与总量)
                net = psutil.net_io_counters()
                curr_sent = net.bytes_sent
                curr_recv = net.bytes_recv
                
                # 实时网速 (需依赖 last_sent_bytes 变量，确保循环外已初始化)
                curr_time = time.time()
                time_delta = curr_time - last_net_time
                
                if time_delta > 0.1:
                    up_speed_kb = (curr_sent - last_sent_bytes) / time_delta / 1024
                    down_speed_kb = (curr_recv - last_recv_bytes) / time_delta / 1024
                    sys_up = f"{int(up_speed_kb)}"
                    sys_down = f"{int(down_speed_kb)}"
                else:
                    sys_up = "0"
                    sys_down = "0"
                
                last_sent_bytes = curr_sent
                last_recv_bytes = curr_recv
                last_net_time = curr_time
                
                # 总量 (GB)
                sent_gb = round(curr_sent / (1024**3), 2)
                recv_gb = round(curr_recv / (1024**3), 2)
                
                # 3. 时间与平均值 (Split Calculation)
                uptime_sec = time.time() - psutil.boot_time()
                uptime_days = uptime_sec / 86400
                
                # 格式化运行时间 (e.g. "5d 12h")
                days = int(uptime_days)
                hours = int((uptime_sec % 86400) / 3600)
                uptime_str = f"{days}d {hours}h"
                
                # 计算日均 (GB/Day)
                if uptime_days > 0.01:
                    daily_sent = round(sent_gb / uptime_days, 2)
                    daily_recv = round(recv_gb / uptime_days, 2)
                else:
                    daily_sent = 0
                    daily_recv = 0
                
                # [挂载数据]
                if "BTC/USDT" in SharedState.market_data:
                    # 注意: sys_up/sys_down (实时速度) 的计算逻辑需保留在上方
                    SharedState.market_data["BTC/USDT"].update({
                        "sys_cpu": cpu,
                        "sys_mem": mem,
                        "sys_disk": disk,
                        "sys_up": sys_up,
                        "sys_down": sys_down,
                        "sys_uptime": uptime_str,
                        
                        # 新的分离统计数据
                        "sys_total_up": f"{sent_gb} G",
                        "sys_daily_up": f"{daily_sent} G/d",
                        "sys_total_down": f"{recv_gb} G",
                        "sys_daily_down": f"{daily_recv} G/d"
                    })
            except Exception as e:
                print(f"[SysMonitor Error] {e}")

            # === B. 哨兵报警逻辑 (Sentinel Alert) ===
            try:
                # 1. 读取配置 (静默读取，失败不报错)
                config_path = "/opt/myquantbot/autopilot_config.json"
                if not os.path.exists(config_path):
                    config_path = "autopilot_config.json" # Local fallback
                
                with open(config_path, 'r', encoding='utf-8') as f:
                    ap_config = json.load(f)
                
                # 2. 检查 SMI 触发
                btc_data = SharedState.market_data.get("BTC/USDT", {})
                current_smi = btc_data.get("smi")
                
                if current_smi is not None:
                    triggers = ap_config.get('sentinel', {}).get('triggers', {})
                    long_open = triggers.get('long_open', -0.46)
                    short_open = triggers.get('short_open', 0.46)
                    
                    is_triggered = False
                    msg_type = ""
                    
                    if current_smi < long_open:
                        is_triggered = True
                        msg_type = f"🟢 机会: SMI ({current_smi}) 低于 {long_open}"
                    elif current_smi > short_open:
                        is_triggered = True
                        msg_type = f"🔴 风险: SMI ({current_smi}) 高于 {short_open}"
                    
                    # 3. 冷却时间检查
                    notify_cfg = ap_config.get('notification', {})
                    interval = int(notify_cfg.get('interval_minutes', 15)) * 60
                    
                    if is_triggered and (time.time() - SharedState.last_alert_time > interval):
                        # 发送消息
                        full_msg = f"{msg_type}\n当前价格: {btc_data.get('price')}\nCPU: {cpu}% MEM: {mem}%"
                        send_message(ap_config, full_msg)
                        SharedState.last_alert_time = time.time()
            except Exception as e:
                # 避免报警逻辑导致主循环崩溃
                # print(f"[Sentinel Error] {e}") 
                pass

            time.sleep(2)
            
        except Exception as e:
            print(f"[Monitor Error] {e}")
            time.sleep(5)

def start_market_monitor():
    t = threading.Thread(target=market_monitor_thread, daemon=True)
    t.start()
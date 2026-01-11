# indicators.py
# ---------------------------------------
# 专门负责数学计算的模块
# ---------------------------------------

def calculate_ema_series(series, period):
    """计算 EMA 序列 (递归算法)"""
    if len(series) < period: return []
    
    # 使用第一个值作为初始值，模拟 Pandas/TradingView 的递归逻辑
    alpha = 2 / (period + 1)
    ema_values = [series[0]] 
    
    for price in series[1:]:
        val = (price * alpha) + (ema_values[-1] * (1 - alpha))
        ema_values.append(val)
        
    return ema_values

def calculate_rsi(prices, period=14):
    """计算 RSI (相对强弱指标)"""
    if len(prices) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calculate_smi(prices, long_len=20, short_len=5, sig_len=5):
    """
    SMI Ergodic Indicator (TradingView 对齐版)
    逻辑: TSI(close, short, long) -> EMA(TSI, sig)
    核心修正: 移除 100 倍率因子，使数值范围落在 -1.0 到 1.0 之间
    """
    # 确保数据长度足够进行多次 EMA 平滑
    if len(prices) < long_len + short_len + sig_len + 50:
        return None, None

    # 1. 价格变动
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    abs_changes = [abs(c) for c in changes]
    
    if not changes: return None, None

    # 2. 双重平滑 (Double Smoothing)
    # 分子: EMA(EMA(change, 20), 5)
    ema_pc_long = calculate_ema_series(changes, long_len)
    if not ema_pc_long: return None, None
    ema_pc_short = calculate_ema_series(ema_pc_long, short_len)
    
    # 分母: EMA(EMA(abs_change, 20), 5)
    ema_apc_long = calculate_ema_series(abs_changes, long_len)
    ema_apc_short = calculate_ema_series(ema_apc_long, short_len)
    
    # 3. 计算 TSI (蓝线值)
    tsi_series = []
    
    # 截取对齐数据
    min_len = min(len(ema_pc_short), len(ema_apc_short))
    pc_slice = ema_pc_short[-min_len:]
    apc_slice = ema_apc_short[-min_len:]
    
    for i in range(min_len):
        denom = apc_slice[i]
        if denom == 0:
            tsi_series.append(0)
        else:
            # 【核心修正】这里不乘 100
            tsi_series.append(pc_slice[i] / denom)
            
    if not tsi_series: return None, None
    
    # 4. 计算 Signal Line (橙线值)
    signal_series = calculate_ema_series(tsi_series, sig_len)
    
    if not signal_series: return None, None
    
    # 返回最新的两个值
    return tsi_series[-1], signal_series[-1]
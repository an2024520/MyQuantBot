# app/routes/api.py
from flask import Blueprint, request, jsonify
import ccxt
from config import Config  # 【新增】
from app.services.monitor import SharedState, add_log
from app.services.bot_manager import BotManager
import json, os

bp = Blueprint('api', __name__)

# ... (market_status, set_timeframe, check_balance 保持不变) ...

@bp.route('/market_status')
def market_status():
    return jsonify(SharedState.market_data)

@bp.route('/set_timeframe', methods=['POST'])
def set_timeframe():
    data = request.json
    symbol = data.get('symbol')
    tf = data.get('tf')
    if symbol in SharedState.watch_settings:
        SharedState.watch_settings[symbol] = tf
        return jsonify({"status": "ok"})
    return jsonify({"status": "error"})

@bp.route('/system/update_source', methods=['POST'])
def update_source():
    try:
        data = request.json
        new_source = data.get('source')
        if new_source not in ['binance', 'okx', 'coinbase']:
            return jsonify({"status": "error", "msg": "Invalid source"})
            
        # 1. 更新内存状态，通知 Monitor 线程切换
        SharedState.target_source = new_source
        
        # 2. 持久化到 bot_state.json (复用 BotManager 的路径定义)
        # 注意: 这里只更新 market_source 字段，不覆盖其他
        state_path = BotManager.EXTERNAL_STATE_PATH
        state = {}
        
        if os.path.exists(state_path):
            with open(state_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
                
        state['market_source'] = new_source
        
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
            
        add_log(f"[系统] 行情源已切换为 {new_source} (已持久化)")
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@bp.route('/check_balance', methods=['POST'])
def check_balance():
    # ... (原有代码保持不变) ...
    try:
        data = request.json
        exchange_id = data.get('exchange_id', 'binance')
        exchange_class = getattr(ccxt, exchange_id)
        params = {
            'apiKey': data.get('api_key'),
            'secret': data.get('secret'),
            'timeout': 10000,
            'options': {'defaultType': 'swap'}
        }
        if data.get('password'):
            params['password'] = data.get('password')
            
        ex = exchange_class(params)
        bal = ex.fetch_balance()
        quote = data.get('quote', 'USDT')
        
        total = 0
        if quote in bal:
            total = float(bal[quote].get('total', 0))
        elif quote in bal.get('total', {}):
             total = float(bal['total'][quote])
             
        return jsonify({"status": "ok", "balance": total})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@bp.route('/kline')
def get_kline():
    try:
        symbol = request.args.get('symbol', 'BTC/USDT')
        tf = request.args.get('tf', '1h')
        
        # 【新增】动态获取数据源，与 Monitor 保持一致
        source = getattr(Config, 'MARKET_SOURCE', 'binance')
        
        # 实例化临时交易所
        if source == 'coinbase':
            exchange = ccxt.coinbase({'enableRateLimit': True})
            # 智能适配: USDT -> USD
            if 'USDT' in symbol:
                symbol = symbol.replace('USDT', 'USD')
        elif source == 'okx':
            exchange = ccxt.okx({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
        else:
            exchange = ccxt.binance({'enableRateLimit': True})
        
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=500)
        return jsonify({"status": "ok", "data": ohlcv})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

# ... (future_start, stop, pause, resume, update 保持不变) ...

@bp.route('/future/start', methods=['POST'])
def future_start():
    try:
        BotManager.start_bot(request.json)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@bp.route('/future/stop', methods=['POST'])
def future_stop():
    BotManager.stop_bot()
    return jsonify({"status": "ok"})

@bp.route('/future/pause', methods=['POST'])
def future_pause():
    try:
        BotManager.pause_bot()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@bp.route('/future/resume', methods=['POST'])
def future_resume():
    try:
        BotManager.resume_bot()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@bp.route('/future/update', methods=['POST'])
def future_update():
    try:
        keys = BotManager.update_config(request.json)
        if keys:
            add_log(f"[指令] 参数热更新: {', '.join(keys)}")
            return jsonify({'status': 'ok', 'msg': f'已更新: {", ".join(keys)}'})
        return jsonify({'status': 'ok', 'msg': '无变更'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@bp.route('/future/status')
def future_status():
    bot = BotManager.get_bot()
    
    res = {
        "running": False,
        "paused": False,
        "logs": list(SharedState.system_logs),
        "profit": 0, "orders": [], "funding_rate": 0, 
        "liquidation": 0, "current_pos": 0, "entry_price": 0, 
        "wallet_balance": 0, "current_price": 0, "smi": 0, "rsi": 0
    }
    
    # 1. 先填充公共行情 (作为兜底)
    # 假设机器人配置了 BTC/USDT，尝试从 SharedState 拿数据
    target_symbol = "BTC/USDT" 
    if bot: target_symbol = bot.config.get('symbol', "BTC/USDT")
    
    if target_symbol in SharedState.market_data:
        m_data = SharedState.market_data[target_symbol]
        res['current_price'] = m_data['price'] # 默认用公共价格
        res['smi'] = m_data['smi']
        res['rsi'] = m_data['rsi']

    # 2. 如果机器人正在运行，强制覆盖为真实合约价格 (微观精准化)
    if bot and bot.running:
        res['running'] = bot.running
        res['paused'] = bot.paused
        res.update(bot.status_data) # 这里包含了 entry_price, current_pos 等
        
        # 【核心修正】: 用机器人内部的 last_price 覆盖公共 price
        # 这样你在面板看到的一定是 OKX 合约的真实成交价
        if bot.status_data.get('last_price', 0) > 0:
            res['current_price'] = bot.status_data['last_price']
            
    return jsonify(res)
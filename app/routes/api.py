# app/routes/api.py
from flask import Blueprint, request, jsonify
import ccxt
from app.services.monitor import SharedState, add_log
from app.services.bot_manager import BotManager

bp = Blueprint('api', __name__)

# --- 公共行情接口 ---

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

@bp.route('/check_balance', methods=['POST'])
def check_balance():
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
        exchange = ccxt.binance({'enableRateLimit': True})
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=500)
        return jsonify({"status": "ok", "data": ohlcv})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

# --- 合约机器人控制接口 ---

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

# 【新增】暂停
@bp.route('/future/pause', methods=['POST'])
def future_pause():
    try:
        BotManager.pause_bot()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

# 【新增】恢复
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
        "paused": False, # <--- 【新增】
        "logs": list(SharedState.system_logs),
        "profit": 0, "orders": [], "funding_rate": 0, 
        "liquidation": 0, "current_pos": 0, "entry_price": 0, 
        "wallet_balance": 0, "current_price": 0, "smi": 0, "rsi": 0
    }
    
    if bot:
        res['running'] = bot.running
        res['paused'] = bot.paused # <--- 【新增】
        res.update(bot.status_data)
        
        sym = bot.config.get('symbol')
        if sym in SharedState.market_data:
            m_data = SharedState.market_data[sym]
            res['current_price'] = m_data['price']
            res['smi'] = m_data['smi']
            res['rsi'] = m_data['rsi']
            
    return jsonify(res)
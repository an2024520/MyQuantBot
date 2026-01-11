# main.py
import time
import threading
import ccxt
from flask import Flask, request, jsonify, render_template
# 假设 indicators.py 就在同级目录，且包含这两个函数
from indicators import calculate_rsi, calculate_smi
from grid_strategy import GridBot
from future_grid_strategy import FutureGridBot

app = Flask(__name__)

SHARED_DATA = {
    "market": {}, 
    "grid_logs": [] 
}

WATCH_SETTINGS = {
    "BTC/USDT": "1h", "ETH/USDT": "1h", "SOL/USDT": "1h",
    "BTC/USDC": "1h", "ETH/USDC": "1h", "SOL/USDC": "1h"
}

spot_bot = None
future_bot = None

def add_log(msg):
    ts = time.strftime("%H:%M:%S")
    SHARED_DATA['grid_logs'].insert(0, f"[{ts}] {msg}")
    if len(SHARED_DATA['grid_logs']) > 200: SHARED_DATA['grid_logs'].pop()

def market_monitor_thread():
    exchange_config = {'enableRateLimit': True, 'timeout': 30000}
    exchange = ccxt.binance(exchange_config)
    
    symbols = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT",
        "BTC/USDC", "ETH/USDC", "SOL/USDC"
    ]
    
    print(">>> 智能监控启动：覆盖 USDT & USDC 市场...")
    
    while True:
        try:
            for symbol in symbols:
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = float(ticker['last'])
                except:
                    continue
                
                current_tf = WATCH_SETTINGS.get(symbol, '1h')
                
                # 获取K线用于计算指标
                ohlcv = exchange.fetch_ohlcv(symbol, current_tf, limit=1000)
                closes = [x[4] for x in ohlcv]
                
                rsi = calculate_rsi(closes)
                smi, sig = calculate_smi(closes)
                
                SHARED_DATA['market'][symbol] = {
                    "price": current_price,
                    "tf": current_tf,
                    "rsi": round(rsi, 2) if rsi else 0,
                    "smi": round(smi, 5) if smi else 0,
                    "sig": round(sig, 5) if sig else 0
                }
                
                # 驱动现货机器人
                if spot_bot and spot_bot.running and spot_bot.config['symbol'] == symbol:
                    spot_bot.run_step(current_price)

                # 驱动合约机器人
                if future_bot and future_bot.running:
                    if future_bot.config['symbol'] == symbol:
                        try:
                            future_bot.run_step(current_price)
                        except Exception as e:
                            add_log(f"Bot Error: {str(e)}")
            
            time.sleep(2) 
            
        except Exception as e:
            print(f"[Monitor Error] {str(e)}")
            time.sleep(5)

# --- 路由部分 ---

@app.route('/')
def dashboard(): 
    # 这里是你上传代码里的逻辑，首页指向 dashboard
    return render_template('dashboard.html')

@app.route('/grid_panel')
def grid_panel(): 
    return render_template('grid_bot.html')

@app.route('/future_grid_panel')
def future_grid_panel(): 
    return render_template('future_grid_bot.html')

@app.route('/api/market_status')
def api_market(): 
    return jsonify(SHARED_DATA['market'])

@app.route('/api/set_timeframe', methods=['POST'])
def set_timeframe():
    data = request.json
    symbol = data.get('symbol')
    tf = data.get('tf')
    if symbol in WATCH_SETTINGS:
        WATCH_SETTINGS[symbol] = tf
        return jsonify({"status": "ok"})
    return jsonify({"status": "error"})

@app.route('/api/check_balance', methods=['POST'])
def check_balance():
    try:
        data = request.json
        exchange_id = data.get('exchange_id', 'binance')
        api_key = data.get('api_key', '')
        secret = data.get('secret', '')
        password = data.get('password', '') 
        quote = data.get('quote', 'USDT')
        
        if not api_key or not secret:
            return jsonify({"status": "error", "msg": "请填写 API Key"})

        exchange_class = getattr(ccxt, exchange_id)
        params = {
            'apiKey': api_key,
            'secret': secret,
            'timeout': 10000,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        }
        if password:
            params['password'] = password
        
        exchange = exchange_class(params)
        balance = exchange.fetch_balance()
        
        target_bal = 0
        # 兼容不同交易所的余额结构
        if quote in balance:
            target_bal = float(balance[quote].get('total', 0))
        elif quote in balance.get('total', {}):
             target_bal = float(balance['total'][quote])
            
        return jsonify({"status": "ok", "balance": target_bal})

    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

# --- 现货机器人接口 ---

@app.route('/api/grid/start', methods=['POST'])
def grid_start():
    global spot_bot
    if spot_bot and spot_bot.running: return jsonify({"status": "error", "msg": "已运行"})
    spot_bot = GridBot(request.json, add_log)
    spot_bot.start()
    return jsonify({"status": "ok"})

@app.route('/api/grid/stop', methods=['POST'])
def grid_stop():
    if spot_bot: spot_bot.stop()
    return jsonify({"status": "ok"})

@app.route('/api/grid/status')
def grid_status():
    res = {"running": False, "logs": SHARED_DATA['grid_logs'], "profit": 0, "orders": [], "current_price": 0}
    if spot_bot:
        res['running'] = spot_bot.running
        res['profit'] = spot_bot.status_data['total_profit']
        res['orders'] = spot_bot.status_data['grid_orders']
        sym = spot_bot.config.get('symbol')
        if sym in SHARED_DATA['market']: res['current_price'] = SHARED_DATA['market'][sym]['price']
    return jsonify(res)

# --- 合约机器人接口 ---

@app.route('/api/future/start', methods=['POST'])
def future_start():
    global future_bot
    if future_bot and future_bot.running: return jsonify({"status": "error", "msg": "已运行"})
    try:
        future_bot = FutureGridBot(request.json, add_log)
        future_bot.start()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/future/stop', methods=['POST'])
def future_stop():
    if future_bot: future_bot.stop()
    return jsonify({"status": "ok"})

# 【新增】合约参数热更新接口
@app.route('/api/future/update', methods=['POST'])
def future_update():
    global future_bot
    if not future_bot or not future_bot.running:
        return jsonify({'status': 'error', 'msg': '策略未运行'})

    data = request.json
    try:
        updated_keys = []
        # 只允许更新安全参数
        if 'stop_loss' in data:
            future_bot.config['stop_loss'] = float(data['stop_loss']) if data['stop_loss'] else ''
            updated_keys.append('止损')
        if 'take_profit' in data:
            future_bot.config['take_profit'] = float(data['take_profit']) if data['take_profit'] else ''
            updated_keys.append('止盈')
        if 'active_order_limit' in data and data['active_order_limit']:
            future_bot.config['active_order_limit'] = int(data['active_order_limit'])
            updated_keys.append('挂单数')

        if updated_keys:
            add_log(f"[指令] 参数热更新成功: {', '.join(updated_keys)}")
            return jsonify({'status': 'ok', 'msg': f'已更新: {", ".join(updated_keys)}'})
        else:
            return jsonify({'status': 'ok', 'msg': '无有效参数变更'})
            
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/api/future/status')
def future_status_api():
    # 默认值
    res = {
        "running": False, 
        "logs": SHARED_DATA['grid_logs'], 
        "profit": 0, 
        "orders": [], 
        "funding_rate": 0, 
        "liquidation": 0, 
        "current_pos": 0, 
        "entry_price": 0, 
        "wallet_balance": 0, 
        "current_price": 0, 
        "smi": 0, 
        "rsi": 0
    }
    
    if future_bot:
        res['running'] = future_bot.running
        # 合并 bot 内部状态
        res.update(future_bot.status_data)
        
        # 补全行情数据 (SMI/RSI)
        sym = future_bot.config.get('symbol')
        if sym in SHARED_DATA['market']:
            m_data = SHARED_DATA['market'][sym]
            res['current_price'] = m_data['price'] # 优先用监控线程的最新价
            res['smi'] = m_data['smi']
            res['rsi'] = m_data['rsi']
            
    return jsonify(res)

if __name__ == '__main__':
    # 启动监控线程
    t = threading.Thread(target=market_monitor_thread, daemon=True)
    t.start()
    print("Web 服务启动: http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
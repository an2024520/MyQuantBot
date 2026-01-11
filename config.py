# config.py
import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'hard-core-v7'
    
    # --- 行情源配置 ---
    # 可选值: 'binance', 'okx', 'coinbase'
    # 建议: 美国/合规需求选 'coinbase'；合约参考选 'okx'
    MARKET_SOURCE = 'coinbase'
# config.py
import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'hard-core-v7'
    # 这里可以放数据库配置等
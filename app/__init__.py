# app/__init__.py
from flask import Flask
from config import Config

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # 注册路由蓝图
    from app.routes.views import bp as views_bp
    from app.routes.api import bp as api_bp
    
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp, url_prefix='/api')

    # 启动后台监控服务
    from app.services.monitor import start_market_monitor
    start_market_monitor()
    
    # 【新增】系统启动时，尝试从存档恢复机器人状态
    from app.services.bot_manager import BotManager
    BotManager.load_state()

    # 【新增】启动 AutoPilot 后台监控服务
    from app.services.autopilot_service import AutoPilotService
    AutoPilotService.start_service()

    return app
# app/services/autopilot_service.py
import json
import os
import threading
import time
import copy
import logging
import traceback

from app.services.monitor import SharedState

# ============ 路径常量 ============
CONFIG_PATH = "autopilot_config.json"  # 本地开发路径
STATE_PATH = "autopilot_state.json"    # 本地开发路径
EXTERNAL_CONFIG_PATH = "/opt/myquantbot/autopilot_config.json"  # VPS 生产路径
EXTERNAL_STATE_PATH = "/opt/myquantbot/autopilot_state.json"    # VPS 生产路径

# 日志配置
logger = logging.getLogger(__name__)


class AutoPilotService:
    """
    SignalGuard / AutoPilot 服务 (单例模式)
    重构版: 使用 SharedState 数据源，支持用户自定义执行目标
    """
    _instance = None
    _lock = threading.Lock()
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 防止重复初始化
        if AutoPilotService._initialized:
            return
        AutoPilotService._initialized = True

        self.lock = threading.Lock()
        
        # 加载配置和状态
        self.config = self.load_config()
        self.state = self.load_state()
        
        # 后台线程
        self.worker = None
        self._running = False
        
        # 运行时数据 (内存存储，不持久化)
        self.runtime_data = {'smi': None, 'price': None, 'updated_at': 0}

    # ============ 服务启动 ============
    @classmethod
    def start_service(cls):
        """启动 AutoPilot 后台服务"""
        instance = cls()
        if not instance._running:
            instance._running = True
            instance.worker = threading.Thread(target=instance._run_loop, daemon=True)
            instance.worker.start()
            logger.info("[AutoPilot] 后台监控服务已启动 (SharedState 模式)")
            print("[AutoPilot] 后台监控服务已启动 (SharedState 模式)")

    @classmethod
    def get_runtime_data(cls):
        """获取运行时数据 (SMI/Price)"""
        instance = cls()
        return instance.runtime_data

    # ============ 核心监控循环 (重构版) ============
    def _run_loop(self):
        """主监控循环 - 从 SharedState 读取数据"""
        while self._running:
            try:
                # 1. 重新加载状态和配置
                self.state = self.load_state()
                self.config = self.load_config()
                
                # 1. 动态获取主页正在监控的交易对 (Dynamic Discovery)
                active_symbols = list(SharedState.market_data.keys())
                
                # 如果主页还没准备好
                if not active_symbols:
                    time.sleep(3)
                    continue
                
                # 默认跟随主页的第一个交易对
                watch_symbol = active_symbols[0]
                market_entry = SharedState.market_data.get(watch_symbol, {})
                
                # 获取主页当前的周期 (用于 UI 显示)
                current_tf = SharedState.watch_settings.get(watch_symbol, 'Unknown')

                smi_value = market_entry.get('smi')
                current_price = market_entry.get('price')
                triggers = self.config.get('sentinel', {}).get('triggers', {})
                
                # 3. 验证数据
                if smi_value is None or current_price is None:
                    logger.debug(f"[AutoPilot] 等待主页数据初始化... (watched: {watch_symbol})")
                    time.sleep(3)
                    continue
                
                # 4. 更新运行时数据 (供 API 读取)
                self.runtime_data.update({
                    'smi': smi_value,
                    'price': current_price,
                    'monitor_symbol': watch_symbol,  # Send actual source to UI
                    'monitor_tf': current_tf,        # Send actual TF to UI
                    'updated_at': time.time()
                })
                
                # 5. 检查是否启用 (仅拦截交易逻辑，数据已更新)
                if not self.state.get('enabled', False):
                    time.sleep(3)  # 空闲时快速轮询保持 UI 更新
                    continue
                
                # 6. 核心逻辑分支 (The Brain)
                self._process_signal(smi_value, current_price, triggers)
                
                # 7. 快速轮询 (仅读内存，安全)
                time.sleep(3)
                
            except Exception as e:
                logger.error(f"[AutoPilot] 监控循环异常: {e}")
                logger.error(traceback.format_exc())
                time.sleep(5)

    def _process_signal(self, smi_value, current_price, triggers):
        """信号处理核心逻辑 (The Brain)"""
        # 延迟导入避免循环依赖
        from app.services.bot_manager import BotManager
        
        bot = BotManager.get_bot()
        bot_running = bot is not None and bot.running
        current_mode = self.state.get('current_mode', 'none')
        
        # ============ Scenario A: Bot 已停止 ============
        if not bot_running:
            # Circuit Breaker: 检测外部停止
            if current_mode != 'none':
                logger.warning("[AutoPilot] Circuit Breaker: Bot 被外部停止，AutoPilot 已禁用")
                print("[AutoPilot] Circuit Breaker: Bot 被外部停止，AutoPilot 已禁用")
                self.state['enabled'] = False
                self.state['current_mode'] = 'none'
                self.save_state(self.state)
                return
            
            # 信号检查: 开仓条件
            long_open = triggers.get('long_open', -0.46)
            short_open = triggers.get('short_open', 0.46)
            
            if smi_value < long_open:
                logger.info(f"[AutoPilot] 触发开多信号: SMI={smi_value:.4f} < {long_open}")
                print(f"[AutoPilot] 触发开多信号: SMI={smi_value:.4f} < {long_open}")
                self._open_position('long', current_price, BotManager)
                
            elif smi_value > short_open:
                logger.info(f"[AutoPilot] 触发开空信号: SMI={smi_value:.4f} > {short_open}")
                print(f"[AutoPilot] 触发开空信号: SMI={smi_value:.4f} > {short_open}")
                self._open_position('short', current_price, BotManager)
        
        # ============ Scenario B: Bot 运行中 ============
        else:
            long_close = triggers.get('long_close', 0.40)
            short_close = triggers.get('short_close', -0.40)
            
            if current_mode == 'long' and smi_value > long_close:
                logger.info(f"[AutoPilot] 触发平多信号: SMI={smi_value:.4f} > {long_close}")
                print(f"[AutoPilot] 触发平多信号: SMI={smi_value:.4f} > {long_close}")
                self._close_position(BotManager)
                
            elif current_mode == 'short' and smi_value < short_close:
                logger.info(f"[AutoPilot] 触发平空信号: SMI={smi_value:.4f} < {short_close}")
                print(f"[AutoPilot] 触发平空信号: SMI={smi_value:.4f} < {short_close}")
                self._close_position(BotManager)

    def _open_position(self, mode, current_price, BotManager):
        """开仓操作"""
        # 延迟导入避免循环依赖
        from app.services.bot_manager import BotManager
        try:
            bot_config = self._calculate_dynamic_config(mode, current_price)
            BotManager.start_bot(bot_config)
            
            self.state['current_mode'] = mode
            self.state['last_trigger_time'] = time.time()
            self.save_state(self.state)
            
            logger.info(f"[AutoPilot] 已开启 {mode.upper()} 仓位, 价格区间: {bot_config.get('lower_price')} - {bot_config.get('upper_price')}")
            print(f"[AutoPilot] 已开启 {mode.upper()} 仓位, 目标: {bot_config.get('symbol')}")
            
        except Exception as e:
            logger.error(f"[AutoPilot] 开仓失败: {e}")
            logger.error(traceback.format_exc())

    def _close_position(self, BotManager):
        """平仓操作"""
        try:
            BotManager.stop_bot()
            
            self.state['current_mode'] = 'none'
            self.state['last_trigger_time'] = time.time()
            self.save_state(self.state)
            
            logger.info("[AutoPilot] 已关闭仓位")
            print("[AutoPilot] 已关闭仓位")
            
        except Exception as e:
            logger.error(f"[AutoPilot] 平仓失败: {e}")
            logger.error(traceback.format_exc())

    def _calculate_dynamic_config(self, mode, current_price):
        """
        计算动态配置 - 注入用户自定义执行目标
        """
        template_key = f"template_{mode}"
        template = self.config.get(template_key, {})
        execution = self.config.get('execution', {})
        
        # 获取缓冲百分比
        upper_buffer_pct = template.get('upper_buffer_pct', 0.05)
        lower_buffer_pct = template.get('lower_buffer_pct', 0.05)
        
        # 计算动态价格区间
        upper_price = int(current_price * (1 + upper_buffer_pct))
        lower_price = int(current_price * (1 - lower_buffer_pct))
        
        # 构建完整配置 (注入用户自定义执行目标)
        bot_config = {
            'exchange_id': execution.get('exchange', 'binance'),  # 动态交易所
            'symbol': execution.get('symbol', 'BTC/USDT'),        # 动态交易对
            'upper_price': upper_price,
            'lower_price': lower_price,
            'leverage': template.get('leverage', 5),
            'amount': template.get('amount', 0.001),
            'grid_num': template.get('grid_num', 40),
            'mode': mode,
        }
        
        return bot_config

    # ============ 默认配置 ============
    @classmethod
    def get_default_config(cls):
        """返回默认配置字典 (含 execution 块)"""
        return {
            "execution": {
                "exchange": "binance",
                "symbol": "BTC/USDT"
            },
            "sentinel": {
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "smi_period": 14,
                "triggers": {
                    "long_open": -0.46,
                    "long_close": 0.40,
                    "short_open": 0.46,
                    "short_close": -0.40
                }
            },
            "template_long": {
                "upper_buffer_pct": 0.07,
                "lower_buffer_pct": 0.03,
                "leverage": 5,
                "amount": 0.001,
                "grid_num": 40
            },
            "template_short": {
                "upper_buffer_pct": 0.03,
                "lower_buffer_pct": 0.07,
                "leverage": 5,
                "amount": 0.001,
                "grid_num": 40
            }
        }

    # ============ 默认状态 ============
    @classmethod
    def get_default_state(cls):
        """返回默认运行状态字典"""
        return {
            "enabled": False,
            "current_mode": "none",
            "last_trigger_time": 0
        }

    # ============ 配置读写 ============
    @classmethod
    def load_config(cls):
        """
        加载配置文件 (带默认值合并)
        """
        loaded_config = None
        
        # 优先尝试外部路径
        if os.path.exists(EXTERNAL_CONFIG_PATH):
            try:
                with open(EXTERNAL_CONFIG_PATH, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
            except Exception as e:
                print(f"[AutoPilot] 外部配置读取失败: {e}")
        
        # 回退到本地路径
        if loaded_config is None and os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
            except Exception as e:
                print(f"[AutoPilot] 本地配置读取失败: {e}")
        
        # 获取默认配置
        default_config = cls.get_default_config()
        
        # 如果没有加载到配置，使用默认值并保存
        if loaded_config is None:
            try:
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, indent=4, ensure_ascii=False)
                print(f"[AutoPilot] 已生成默认配置文件: {CONFIG_PATH}")
            except Exception as e:
                print(f"[AutoPilot] 默认配置保存失败: {e}")
            return default_config
        
        # SAFE MERGE: Only inject missing top-level keys. NEVER overwrite existing ones.
        if 'execution' not in loaded_config:
            loaded_config['execution'] = default_config['execution']
            
        # Ensure other required keys exist (in case of partial config)
        for key in ['sentinel', 'template_long', 'template_short']:
             if key not in loaded_config:
                 loaded_config[key] = default_config[key]
        
        return loaded_config

    @classmethod
    def save_config(cls, config_dict):
        """保存配置文件"""
        # 验证必需的键
        required_keys = ["execution", "sentinel", "template_long", "template_short"]
        for key in required_keys:
            if key not in config_dict:
                raise ValueError(f"配置缺少必需的键: {key}")
        
        try:
            config_dir = os.path.dirname(EXTERNAL_CONFIG_PATH)
            if config_dir and not os.path.exists(config_dir):
                os.makedirs(config_dir, exist_ok=True)
            
            with open(EXTERNAL_CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(config_dict, f, indent=4, ensure_ascii=False)
            print(f"[AutoPilot] 配置已保存: {EXTERNAL_CONFIG_PATH}")
        except Exception as e:
            print(f"[AutoPilot] 配置保存失败: {e}")
            raise

    # ============ 状态读写 ============
    @classmethod
    def load_state(cls):
        """加载运行状态"""
        if os.path.exists(EXTERNAL_STATE_PATH):
            try:
                with open(EXTERNAL_STATE_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"[AutoPilot] 外部状态读取失败: {e}")
        
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"[AutoPilot] 本地状态读取失败: {e}")
        
        return cls.get_default_state()

    @classmethod
    def save_state(cls, state_dict):
        """保存运行状态"""
        try:
            state_dir = os.path.dirname(EXTERNAL_STATE_PATH)
            if state_dir and not os.path.exists(state_dir):
                os.makedirs(state_dir, exist_ok=True)
            
            with open(EXTERNAL_STATE_PATH, 'w', encoding='utf-8') as f:
                json.dump(state_dict, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[AutoPilot] 状态保存失败: {e}")
            raise

    @classmethod
    def set_enabled(cls, enabled: bool):
        """快捷方法: 更新 enabled 字段"""
        state = cls.load_state()
        state["enabled"] = enabled
        cls.save_state(state)
        return state

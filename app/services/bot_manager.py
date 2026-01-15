# app/services/bot_manager.py
import json
import os
from app.strategies.future_grid_strategy import FutureGridBot
from app.services.monitor import add_log

class BotManager:
    _future_bot = None  # 单例实例
    STATE_FILE = "bot_state.json"  # 本地开发路径
    EXTERNAL_STATE_PATH = "/opt/myquant_config/bot_state.json" # VPS 生产路径

    @classmethod
    def get_bot(cls):
        return cls._future_bot

    @classmethod
    def start_bot(cls, config):
        if cls._future_bot and cls._future_bot.running:
            raise Exception("策略已在运行中")
        
        # 初始化并启动
        cls._future_bot = FutureGridBot(config, add_log)
        cls._future_bot.start()
        add_log("[Manager] 机器人实例已创建并启动")
        
        # 启动成功后，保存状态
        cls.save_state()

    @classmethod
    def stop_bot(cls):
        if cls._future_bot:
            cls._future_bot.stop()
            add_log("[Manager] 停止指令已下达")
            
            # 停止后，保存状态 (running=False)
            cls.save_state()

    @classmethod
    def pause_bot(cls):
        if cls._future_bot and cls._future_bot.running:
            cls._future_bot.pause()
            add_log("[Manager] 暂停指令已下达")
            
            # 暂停后，保存状态 (paused=True)
            cls.save_state()
        else:
            raise Exception("策略未运行，无法暂停")

    @classmethod
    def resume_bot(cls):
        if cls._future_bot and cls._future_bot.running:
            cls._future_bot.resume()
            add_log("[Manager] 恢复指令已下达")
            
            # 恢复后，保存状态 (paused=False)
            cls.save_state()
        else:
            raise Exception("策略未运行，无法恢复")
    
    @classmethod
    def update_config(cls, updates):
        """运行时热更新"""
        if not cls._future_bot or not cls._future_bot.running:
            raise Exception("策略未运行")
        
        updated_keys = []
        # 安全更新白名单
        if 'stop_loss' in updates:
            val = updates['stop_loss']
            cls._future_bot.config['stop_loss'] = float(val) if val else ''
            updated_keys.append('止损')
            
        if 'take_profit' in updates:
            val = updates['take_profit']
            cls._future_bot.config['take_profit'] = float(val) if val else ''
            updated_keys.append('止盈')
            
        if 'active_order_limit' in updates and updates['active_order_limit']:
            cls._future_bot.config['active_order_limit'] = int(updates['active_order_limit'])
            updated_keys.append('挂单数')
            
        # 扩展：支持格数、区间、金额等核心参数更新
        if 'grid_count' in updates and updates['grid_count']:
            cls._future_bot.config['grid_count'] = int(updates['grid_count'])
            updated_keys.append('格数')

        if 'upper_price' in updates and updates['upper_price']:
            cls._future_bot.config['upper_price'] = float(updates['upper_price'])
            updated_keys.append('上限')

        if 'lower_price' in updates and updates['lower_price']:
            cls._future_bot.config['lower_price'] = float(updates['lower_price'])
            updated_keys.append('下限')
            
        if 'amount' in updates and updates['amount']:
            val = float(updates['amount'])
            cls._future_bot.config['amount'] = val
            cls._future_bot.order_qty = val # 同步更新缓存
            updated_keys.append('金额')
        
        # 软重启逻辑：重算网格 + 重置挂单
        if updated_keys:
            try:
                # 1. 重新计算网格数组
                cls._future_bot.generate_grids()
                
                # 2. 获取当前价格并重置挂单墙
                current_price = cls._future_bot.status_data.get('last_price', 0)
                if current_price > 0:
                    cls._future_bot.initialize_grid_orders(current_price)
                    add_log(f"[Soft Restart] 参数已热更新，网格重置完成")
                else:
                    add_log(f"[Soft Restart] 警告: 未获取到有效价格，仅更新参数")
                    
            except Exception as e:
                add_log(f"[Soft Restart] 热更新失败: {e}")

        # 【核心安全机制】参数更新后，强制保存最新配置到磁盘 (Write-Through)
        if updated_keys:
            cls.save_state()
            
        return updated_keys

    @classmethod
    def save_state(cls):
        """将当前状态写入硬盘"""
        state = {
            "running": False,
            "paused": False,
            "config": {}
        }
        
        if cls._future_bot:
            state["running"] = cls._future_bot.running
            state["paused"] = getattr(cls._future_bot, "paused", False)
            state["config"] = cls._future_bot.config
        
        try:
            # 确保存储目录存在
            config_dir = os.path.dirname(cls.EXTERNAL_STATE_PATH)
            if config_dir and not os.path.exists(config_dir):
                os.makedirs(config_dir, exist_ok=True)

            with open(cls.EXTERNAL_STATE_PATH, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=4, ensure_ascii=False)
        except Exception as e:
            add_log(f"[系统] 状态保存失败: {e}")

    @classmethod
    def load_state(cls):
        """启动时从硬盘恢复状态"""
        # 优先读取外部配置
        state_file = cls.EXTERNAL_STATE_PATH
        if not os.path.exists(state_file):
            # 回退到本地默认文件 (首次运行或迁移)
            if os.path.exists(cls.STATE_FILE):
                state_file = cls.STATE_FILE
                add_log("[系统] 外部状态未找到，使用本地默认存档")
            else:
                return
            
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
            
            # 如果存档显示之前是运行状态，则自动重启
            if state.get("running", False) and state.get("config"):
                print(">>> [System] 检测到异常退出/重启，正在恢复策略...")
                add_log("[系统] 检测到存档，正在自动恢复策略...")
                
                # 1. 恢复启动 (使用之前的配置)
                try:
                    cls.start_bot(state["config"])
                except Exception as e:
                    add_log(f"[恢复失败] 启动出错: {e}")
                    return

                # 2. 恢复暂停状态 (如果是暂停中)
                if state.get("paused", False):
                    cls.pause_bot()
                    add_log("[系统] 已恢复至【暂停】状态")
                else:
                    add_log("[系统] 已恢复至【运行】状态")
                    
        except Exception as e:
            add_log(f"[系统] 状态恢复失败: {e}")
# app/services/bot_manager.py
import json
import os
from app.strategies.future_grid_strategy import FutureGridBot
from app.services.monitor import add_log

class BotManager:
    _future_bot = None  # 单例实例
    STATE_FILE = "bot_state.json"  # 【新增】状态存档文件

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
        
        # 【新增】启动成功后，保存状态
        cls.save_state()

    @classmethod
    def stop_bot(cls):
        if cls._future_bot:
            cls._future_bot.stop()
            add_log("[Manager] 停止指令已下达")
            
            # 【新增】停止后，保存状态 (此时 running=False)
            cls.save_state()

    @classmethod
    def pause_bot(cls):
        if cls._future_bot and cls._future_bot.running:
            cls._future_bot.pause()
            add_log("[Manager] 暂停指令已下达")
            
            # 【新增】暂停后，保存状态 (paused=True)
            cls.save_state()
        else:
            raise Exception("策略未运行，无法暂停")

    @classmethod
    def resume_bot(cls):
        if cls._future_bot and cls._future_bot.running:
            cls._future_bot.resume()
            add_log("[Manager] 恢复指令已下达")
            
            # 【新增】恢复后，保存状态 (paused=False)
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
        
        # 【新增】参数更新后，保存最新配置到磁盘
        if updated_keys:
            cls.save_state()
            
        return updated_keys

    # --- 【新增】持久化核心逻辑 ---

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
            with open(cls.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=4, ensure_ascii=False)
            # print(f">>> [System] 状态已保存: {state['running']}") 
        except Exception as e:
            add_log(f"[系统] 状态保存失败: {e}")

    @classmethod
    def load_state(cls):
        """启动时从硬盘恢复状态"""
        if not os.path.exists(cls.STATE_FILE):
            return
            
        try:
            with open(cls.STATE_FILE, 'r', encoding='utf-8') as f:
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
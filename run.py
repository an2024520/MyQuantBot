# run.py
import socket

# --- 【新增】强制 IPv4 补丁 (核弹级修复) ---
# 既然 VPS 的 IPv6 不通，我们就让 Python 以为这个世界只有 IPv4
_old_getaddrinfo = socket.getaddrinfo

def new_getaddrinfo(*args, **kwargs):
    responses = _old_getaddrinfo(*args, **kwargs)
    # 过滤掉所有 IPv6 结果 (AF_INET6)，只保留 IPv4 (AF_INET)
    return [response for response in responses if response[0] == socket.AF_INET]

socket.getaddrinfo = new_getaddrinfo
# ----------------------------------------

from app import create_app

app = create_app()

if __name__ == '__main__':
    # 之前说的：使用 127.0.0.1 配合 SSH 隧道最安全
    app.run(host='127.0.0.1', port=5000, debug=True, use_reloader=False)

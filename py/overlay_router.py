import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# 定义子路由
router = APIRouter()

class DanmakuOverlayManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # 使用副本迭代，防止在广播时连接断开导致 list 变化报错
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                self.disconnect(connection)

# 实例化管理器
overlay_manager = DanmakuOverlayManager()

@router.websocket("/ws/overlay")
async def websocket_overlay_endpoint(websocket: WebSocket):
    await overlay_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text() # 保持连接
    except WebSocketDisconnect:
        overlay_manager.disconnect(websocket)

@router.post("/api/overlay/danmaku")
async def show_danmaku_overlay(data: dict):
    await overlay_manager.broadcast({"action": "show", "data": data})
    return {"status": "ok"}

@router.post("/api/overlay/danmaku/clear")
async def clear_danmaku_overlay():
    await overlay_manager.broadcast({"action": "clear"})
    return {"status": "ok"}

@router.get("/danmaku_overlay", response_class=HTMLResponse)
async def get_danmaku_overlay():
    # 将 HTML 字符串放在函数内部或单独的文件中，避免占用主内存空间
    html_content = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>当前回复弹幕</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;700&display=swap');
            body { margin: 0; padding: 0; background-color: transparent; overflow: hidden; font-family: 'Noto Sans SC', sans-serif; }
            #danmaku-container {
                position: absolute; bottom: 50px; left: -600px; width: fit-content; min-width: 300px; max-width: 550px;
                background: rgba(15, 23, 42, 0.8); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
                color: #f1f5f9; padding: 16px 24px; border-radius: 12px 24px 24px 12px;
                border-left: 6px solid #38bdf8; box-shadow: 0 15px 35px rgba(0,0,0,0.4);
                display: flex; flex-direction: column; gap: 8px;
                transition: all 0.8s cubic-bezier(0.16, 1, 0.3, 1); opacity: 0;
            }
            #danmaku-container.show { left: 30px; opacity: 1; }
            .header { display: flex; align-items: center; gap: 8px; }
            .live-dot { width: 8px; height: 8px; background-color: #38bdf8; border-radius: 50%; box-shadow: 0 0 10px #38bdf8; animation: pulse 1.5s infinite; }
            .title { font-size: 14px; font-weight: 700; color: #7dd3fc; text-transform: uppercase; letter-spacing: 1.5px; }
            .content { font-size: 22px; font-weight: 400; line-height: 1.5; word-wrap: break-word; text-shadow: 0 0 8px rgba(255,255,255,0.1); }
            @keyframes pulse { 0% { transform: scale(0.95); opacity: 0.8; } 50% { transform: scale(1.2); opacity: 1; } 100% { transform: scale(0.95); opacity: 0.8; } }
        </style>
    </head>
    <body>
        <div id="danmaku-container">
            <div class="header"><div class="live-dot"></div><div class="title">New Message</div></div>
            <div class="content" id="danmaku-content"></div>
        </div>
        <script>
            const container = document.getElementById('danmaku-container');
            const contentEl = document.getElementById('danmaku-content');
            function connect() {
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = protocol + '//' + window.location.host + '/ws/overlay';
                const ws = new WebSocket(wsUrl);
                ws.onmessage = function(event) {
                    try {
                        const msg = JSON.parse(event.data);
                        if (msg.action === 'show') {
                            contentEl.textContent = msg.data.content || '';
                            container.classList.add('show');
                        } else if (msg.action === 'clear') {
                            container.classList.remove('show');
                        }
                    } catch (e) {}
                };
                ws.onclose = function() { setTimeout(connect, 2000); };
            }
            connect();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
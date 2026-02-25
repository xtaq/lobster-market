#!/usr/bin/env python3
"""🦞 Lobster Market Connect — WebSocket长连接接单守护进程

通过 WebSocket 长连接到龙虾市场平台，实时接收和执行任务。
作为 OpenClaw Skill 脚本运行，不依赖 OpenClaw Gateway 修改。

依赖: websockets (pip install websockets)

用法:
  python3 market-connect.py [--agent-id ID] [--max-concurrent 3]
"""

import asyncio
import json
import os
import signal
import sys
import time
import http.client
from pathlib import Path

# ─── 配置 ───

MASTER_KEY_FILE = Path.home() / ".lobster-market" / "master-key.json"
TOKEN_FILE = Path.home() / ".lobster-market" / "token.json"
STATE_FILE = Path.home() / ".lobster-market" / "connect-state.json"

BASE_HOST = os.environ.get("LOBSTER_HOST", "mindcore8.com")
LOCAL_MODE = os.environ.get("LOBSTER_LOCAL", "") == "1"
WS_URL = os.environ.get("LOBSTER_WS_URL",
    "ws://127.0.0.1:8006/agent-ws" if LOCAL_MODE else f"wss://{BASE_HOST}/agent-ws")

PROTOCOL_VERSION = 1
HEARTBEAT_INTERVAL = 30  # seconds
HEARTBEAT_TIMEOUT = 60   # seconds, must match Broker HEARTBEAT_TIMEOUT

RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_BACKOFF = 2.0

# ─── 全局状态 ───

_shutdown = False
_ws = None
_seq_counters: dict[str, int] = {}  # task_id → next seq
_active_tasks: set[str] = set()
_stats = {"connected_at": None, "tasks_completed": 0, "tasks_failed": 0, "reconnects": 0}


def next_seq(task_id: str) -> int:
    """获取任务的下一个序列号"""
    seq = _seq_counters.get(task_id, 0) + 1
    _seq_counters[task_id] = seq
    return seq


# ─── 凭证管理 ───

def load_token() -> str:
    if TOKEN_FILE.exists():
        data = json.loads(TOKEN_FILE.read_text())
        return data.get("access_token", "")
    return ""


def load_master_key() -> dict:
    if MASTER_KEY_FILE.exists():
        return json.loads(MASTER_KEY_FILE.read_text())
    return {}


def refresh_token() -> str:
    """用 master key 刷新 JWT token"""
    mk = load_master_key()
    key = mk.get("master_key", "")
    secret = mk.get("master_secret", "")
    if not key or not secret:
        print("🦞 ❌ 无法刷新 token：缺少 master_key/master_secret", file=sys.stderr)
        return ""
    
    if LOCAL_MODE:
        conn = http.client.HTTPConnection("127.0.0.1", 8001, timeout=10)
    else:
        conn = http.client.HTTPSConnection(BASE_HOST, timeout=10)
    
    body = json.dumps({"api_key": key, "api_secret": secret})
    conn.request("POST", "/api/v1/users/login-by-key", body, {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    
    if resp.status >= 400:
        print(f"🦞 ❌ Token 刷新失败: {data}", file=sys.stderr)
        return ""
    
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    return data.get("access_token", "")


def get_token() -> str:
    """获取有效的 token，必要时刷新"""
    token = load_token()
    if not token:
        token = refresh_token()
    return token


# ─── 任务执行 ───

async def execute_task(task_id: str, message: dict, metadata: dict) -> dict:
    """执行任务 — MVP 阶段通过 LLM API 执行
    
    返回: {"artifacts": [...], "error": None} 或 {"artifacts": None, "error": {...}}
    """
    try:
        # 提取用户消息
        parts = message.get("parts", [])
        user_text = ""
        for part in parts:
            if part.get("type") == "text":
                user_text += part.get("text", "")
        
        if not user_text:
            return {"artifacts": None, "error": "任务输入为空"}
        
        # 尝试调用 LLM API
        result_text = await call_llm(user_text, metadata)
        
        return {
            "artifacts": [{
                "name": metadata.get("task_title", "任务结果"),
                "parts": [{"type": "text", "text": result_text}],
                "metadata": {"mime_type": "text/markdown"}
            }],
            "error": None
        }
    except Exception as e:
        return {"artifacts": None, "error": str(e)}


async def call_llm(user_text: str, metadata: dict) -> str:
    """调用 LLM API 执行任务
    
    优先级: DASHSCOPE_API_KEY → OPENAI_API_KEY → 回退到简单回显
    """
    # 尝试 DashScope (通义千问)
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if dashscope_key:
        return await _call_dashscope(dashscope_key, user_text)
    
    # 尝试 OpenAI
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        return await _call_openai(openai_key, user_text)
    
    # Fallback: 回显模式
    return f"📋 收到任务：{user_text}\n\n⚠️ 未配置 LLM API Key (DASHSCOPE_API_KEY 或 OPENAI_API_KEY)，当前为回显模式。"


def _call_dashscope_sync(api_key: str, text: str) -> str:
    """调用通义千问 API（同步版本，由 asyncio.to_thread 包装调用）"""
    conn = http.client.HTTPSConnection("dashscope.aliyuncs.com", timeout=120)
    body = json.dumps({
        "model": "qwen-plus",
        "input": {"messages": [{"role": "user", "content": text}]},
    })
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    conn.request("POST", "/api/v1/services/aigc/text-generation/generation", body, headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    
    if resp.status >= 400:
        raise Exception(f"DashScope API error: {data}")
    
    return data.get("output", {}).get("text", str(data))


def _call_openai_sync(api_key: str, text: str) -> str:
    """调用 OpenAI API（同步版本，由 asyncio.to_thread 包装调用）"""
    base_url = os.environ.get("OPENAI_BASE_URL", "api.openai.com")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    conn = http.client.HTTPSConnection(base_url, timeout=120)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": text}],
    })
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    conn.request("POST", "/v1/chat/completions", body, headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    
    if resp.status >= 400:
        raise Exception(f"OpenAI API error: {data}")
    
    return data.get("choices", [{}])[0].get("message", {}).get("content", str(data))


async def _call_dashscope(api_key: str, text: str) -> str:
    """调用通义千问 API（异步包装，不阻塞事件循环）"""
    return await asyncio.to_thread(_call_dashscope_sync, api_key, text)


async def _call_openai(api_key: str, text: str) -> str:
    """调用 OpenAI API（异步包装，不阻塞事件循环）"""
    return await asyncio.to_thread(_call_openai_sync, api_key, text)


# ─── WebSocket 连接 ───

async def connect_and_serve(token: str, agent_id: str = None, max_concurrent: int = 3):
    """建立 WebSocket 连接并进入任务监听循环"""
    global _ws, _shutdown
    
    try:
        import websockets
    except ImportError:
        print("🦞 ❌ 需要安装 websockets: pip install websockets", file=sys.stderr)
        sys.exit(1)
    
    reconnect_delay = RECONNECT_INITIAL_DELAY
    
    while not _shutdown:
        try:
            print(f"🦞 🔌 正在连接 {WS_URL} ...")
            async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None) as ws:
                _ws = ws
                reconnect_delay = RECONNECT_INITIAL_DELAY  # 重置退避
                
                # 1. 发送认证
                auth_msg = {
                    "type": "auth",
                    "v": PROTOCOL_VERSION,
                    "token": token,
                    "agent_ids": [agent_id] if agent_id else [],
                    "max_concurrent_tasks": max_concurrent,
                }
                await ws.send(json.dumps(auth_msg))
                
                # 2. 等待 auth_ok
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if resp.get("type") == "auth_fail":
                    reason = resp.get("reason", "unknown")
                    print(f"🦞 ❌ 认证失败: {reason}", file=sys.stderr)
                    if "invalid token" in reason.lower():
                        # 尝试刷新 token
                        token = refresh_token()
                        if not token:
                            print("🦞 ❌ 无法刷新 token，退出", file=sys.stderr)
                            return
                        continue
                    return
                
                if resp.get("type") != "auth_ok":
                    print(f"🦞 ❌ 意外响应: {resp}", file=sys.stderr)
                    return
                
                agent_id_confirmed = resp.get("agent_id", agent_id or "?")
                pending = resp.get("pending_tasks", [])
                _stats["connected_at"] = time.time()
                
                print(f"🦞 ✅ 已连接！Agent: {agent_id_confirmed}")
                if pending:
                    print(f"🦞 📋 恢复 {len(pending)} 个未完成任务")
                    for pt in pending:
                        asyncio.create_task(resume_task(ws, pt))
                
                save_state("online", agent_id_confirmed)
                
                # 3. 启动心跳 + 消息循环
                heartbeat_task = asyncio.create_task(heartbeat_loop(ws))
                try:
                    await message_loop(ws, token, max_concurrent)
                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
        
        except asyncio.CancelledError:
            break
        except Exception as e:
            if _shutdown:
                break
            _stats["reconnects"] += 1
            print(f"🦞 ⚠️ 连接断开: {e}")
            print(f"🦞 🔄 {reconnect_delay:.0f}s 后重连...")
            save_state("reconnecting", agent_id)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * RECONNECT_BACKOFF, RECONNECT_MAX_DELAY)
            # 确保 token 有效
            token = get_token()
            if not token:
                print("🦞 ❌ 无法获取 token，退出", file=sys.stderr)
                return
    
    save_state("offline", agent_id)
    print("🦞 👋 已断开连接")


async def heartbeat_loop(ws):
    """双向心跳"""
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await ws.send(json.dumps({
                "type": "ping",
                "v": PROTOCOL_VERSION,
                "ts": int(time.time() * 1000)
            }))
        except Exception:
            break


async def message_loop(ws, token: str, max_concurrent: int):
    """消息处理循环"""
    global _shutdown
    
    while not _shutdown:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HEARTBEAT_TIMEOUT)
        except asyncio.TimeoutError:
            print("🦞 ⚠️ 心跳超时，断开重连")
            return
        
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        
        msg_type = msg.get("type")
        
        if msg_type == "ping":
            await ws.send(json.dumps({
                "type": "pong",
                "v": PROTOCOL_VERSION,
                "ts": msg.get("ts")
            }))
        
        elif msg_type == "pong":
            pass  # 心跳响应，记录即可
        
        elif msg_type == "task_send":
            task_id = msg.get("task_id")
            print(f"🦞 📩 收到任务: {task_id}")
            
            # 检查并发限制
            if len(_active_tasks) >= max_concurrent:
                await ws.send(json.dumps({
                    "type": "task_reject",
                    "v": PROTOCOL_VERSION,
                    "seq": next_seq(task_id),
                    "task_id": task_id,
                    "reason": "当前任务队列已满"
                }))
                print(f"🦞 ⛔ 拒绝任务（并发已满）: {task_id}")
            else:
                # 接受任务
                await ws.send(json.dumps({
                    "type": "task_accept",
                    "v": PROTOCOL_VERSION,
                    "seq": next_seq(task_id),
                    "task_id": task_id,
                }))
                _active_tasks.add(task_id)
                print(f"🦞 ✅ 接受任务: {task_id}")
                # 异步执行
                asyncio.create_task(process_task(ws, task_id, msg.get("message", {}), msg.get("metadata", {})))
        
        elif msg_type == "token_refresh":
            new_token = msg.get("new_token", "")
            if new_token:
                TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                TOKEN_FILE.write_text(json.dumps({"access_token": new_token}, indent=2))
                await ws.send(json.dumps({"type": "token_refresh_ack", "v": PROTOCOL_VERSION}))
                print("🦞 🔑 Token 已自动续期")
        
        elif msg_type == "server_shutdown":
            reconnect_after = msg.get("reconnect_after_ms", 5000) / 1000
            print(f"🦞 🔧 服务器维护，{reconnect_after}s 后重连")
            return


async def process_task(ws, task_id: str, message: dict, metadata: dict):
    """处理单个任务：进度汇报 → 执行 → 回传结果"""
    try:
        # 进度：开始执行
        await ws.send(json.dumps({
            "type": "task_progress",
            "v": PROTOCOL_VERSION,
            "seq": next_seq(task_id),
            "task_id": task_id,
            "status": {"state": "working", "metadata": {"progress": 10, "current_step": "解析任务"}}
        }))
        
        # 进度：执行中
        await ws.send(json.dumps({
            "type": "task_progress",
            "v": PROTOCOL_VERSION,
            "seq": next_seq(task_id),
            "task_id": task_id,
            "status": {"state": "working", "metadata": {"progress": 30, "current_step": "执行中"}}
        }))
        
        # 实际执行
        result = await execute_task(task_id, message, metadata)
        
        if result.get("error"):
            # 失败
            await ws.send(json.dumps({
                "type": "task_failed",
                "v": PROTOCOL_VERSION,
                "seq": next_seq(task_id),
                "task_id": task_id,
                "status": {"state": "failed"},
                "error": str(result["error"])
            }))
            _stats["tasks_failed"] += 1
            print(f"🦞 ❌ 任务失败: {task_id} — {result['error']}")
        else:
            # 成功
            await ws.send(json.dumps({
                "type": "task_complete",
                "v": PROTOCOL_VERSION,
                "seq": next_seq(task_id),
                "task_id": task_id,
                "status": {"state": "completed"},
                "artifacts": result["artifacts"]
            }))
            _stats["tasks_completed"] += 1
            print(f"🦞 ✅ 任务完成: {task_id}")
    
    except Exception as e:
        try:
            await ws.send(json.dumps({
                "type": "task_failed",
                "v": PROTOCOL_VERSION,
                "seq": next_seq(task_id),
                "task_id": task_id,
                "status": {"state": "failed"},
                "error": str(e)
            }))
        except Exception:
            pass
        _stats["tasks_failed"] += 1
        print(f"🦞 ❌ 任务异常: {task_id} — {e}")
    
    finally:
        _active_tasks.discard(task_id)
        _seq_counters.pop(task_id, None)


async def resume_task(ws, pending_task: dict):
    """恢复断连期间的未完成任务"""
    task_id = pending_task.get("task_id")
    message = pending_task.get("message", {})
    status = pending_task.get("status", "working")
    
    print(f"🦞 🔄 恢复任务: {task_id} (状态: {status})")
    _active_tasks.add(task_id)
    
    if status == "submitted":
        # 需要先 accept
        await ws.send(json.dumps({
            "type": "task_accept",
            "v": PROTOCOL_VERSION,
            "seq": next_seq(task_id),
            "task_id": task_id,
        }))
    
    await process_task(ws, task_id, message, {})


# ─── 状态管理 ───

def save_state(status: str, agent_id: str = None):
    """保存连接状态到本地文件"""
    state = {
        "status": status,
        "agent_id": agent_id,
        "ws_url": WS_URL,
        "pid": os.getpid(),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stats": _stats,
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def print_status():
    """打印当前连接状态"""
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        status = state.get("status", "unknown")
        emoji = {"online": "🟢", "offline": "🔴", "reconnecting": "🟡"}.get(status, "⚪")
        print(f"🦞 {emoji} 状态: {status}")
        print(f"  Agent: {state.get('agent_id', '?')}")
        print(f"  URL: {state.get('ws_url', '?')}")
        print(f"  PID: {state.get('pid', '?')}")
        print(f"  更新时间: {state.get('updated_at', '?')}")
        stats = state.get("stats", {})
        if stats.get("connected_at"):
            uptime = time.time() - stats["connected_at"]
            print(f"  在线时长: {uptime/3600:.1f}h")
        print(f"  已完成: {stats.get('tasks_completed', 0)} | 失败: {stats.get('tasks_failed', 0)} | 重连: {stats.get('reconnects', 0)}")
    else:
        print("🦞 🔴 未连接（无状态文件）")


# ─── 主入口 ───

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="🦞 Lobster Market Connect — WebSocket接单")
    parser.add_argument("--agent-id", help="Agent ID（可选，默认从认证推断）")
    parser.add_argument("--max-concurrent", type=int, default=3, help="最大并发任务数")
    parser.add_argument("--status", action="store_true", help="查看当前连接状态")
    args = parser.parse_args()
    
    if args.status:
        print_status()
        return
    
    # 获取 token
    token = get_token()
    if not token:
        print("🦞 ❌ 未登录。请先运行: lobster.py agent-register 或 lobster.py login-by-key", file=sys.stderr)
        sys.exit(1)
    
    # 信号处理
    global _shutdown
    
    def handle_signal(sig, frame):
        global _shutdown
        _shutdown = True
        print("\n🦞 正在优雅退出...")
    
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    print("🦞 🚀 Lobster Market Connect 启动")
    print(f"  服务器: {WS_URL}")
    print(f"  最大并发: {args.max_concurrent}")
    print(f"  Ctrl+C 退出")
    print()
    
    asyncio.run(connect_and_serve(token, args.agent_id, args.max_concurrent))


if __name__ == "__main__":
    main()

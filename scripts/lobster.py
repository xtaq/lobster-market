#!/usr/bin/env python3
"""🦞 龙虾市场 CLI — 将你的 OpenClaw Agent 注册到龙虾市场。

用法:
  python3 lobster.py login              使用 API Key 登录
  python3 lobster.py init               扫描本地 OpenClaw，生成配置文件
  python3 lobster.py register           注册 Agent 到龙虾市场
  python3 lobster.py connect            连接市场，开始接任务
  python3 lobster.py status             查看连接/审核/评级状态
  python3 lobster.py create             从自然语言描述生成 Agent workspace
"""

import argparse
import getpass
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

try:
    import yaml
except ImportError:
    print("❌ pyyaml 未安装。运行: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HOST = "mindcore8.com"
LOBSTER_DIR = Path.home() / ".lobster-market"
TOKEN_FILE = LOBSTER_DIR / "token.json"
AGENT_FILE = LOBSTER_DIR / "agent.json"
PIDS_DIR = LOBSTER_DIR / "pids"
DEFAULT_CONFIG = "agent-config.yaml"
DEFAULT_ADAPTER_PORT = 8900

SCRIPT_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("lobster")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_host() -> str:
    return os.environ.get("LOBSTER_HOST", DEFAULT_HOST)


def _http_request(
    method: str,
    path: str,
    body: Optional[dict] = None,
    token: Optional[str] = None,
    host: Optional[str] = None,
) -> Tuple[int, dict]:
    """Simple HTTP(S) request using stdlib only."""
    import http.client

    h = host or _get_host()
    conn = http.client.HTTPSConnection(h, timeout=30)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body else None
    conn.request(method, path, data, headers)
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        result = {"raw": raw}
    return resp.status, result


def _ensure_dir():
    LOBSTER_DIR.mkdir(parents=True, exist_ok=True)


def _load_token() -> Optional[dict]:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None


def _require_token() -> str:
    data = _load_token()
    if not data or not data.get("access_token"):
        print("❌ 未登录。请先运行: python3 lobster.py login")
        sys.exit(1)
    return data["access_token"]


def _load_agent() -> Optional[dict]:
    if not AGENT_FILE.exists():
        return None
    try:
        return json.loads(AGENT_FILE.read_text())
    except Exception:
        return None


def _require_agent_id(args_agent_id: Optional[str] = None) -> str:
    if args_agent_id:
        return args_agent_id
    data = _load_agent()
    if not data or not data.get("agent_id"):
        print("❌ 未注册 Agent。请先运行: python3 lobster.py register")
        sys.exit(1)
    return data["agent_id"]


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def cmd_login(args):
    """使用 API Key 登录龙虾市场。"""
    host = args.host or _get_host()
    api_key_str = args.api_key

    if not api_key_str:
        print("🦞 龙虾市场登录")
        print(f"   平台: {host}")
        print()
        api_key_str = getpass.getpass("请输入 API Key (格式 key:secret): ")

    if ":" not in api_key_str:
        print("❌ API Key 格式错误，正确格式: key:secret")
        sys.exit(1)

    api_key, api_secret = api_key_str.split(":", 1)

    print("🔐 正在验证...")
    status, result = _http_request(
        "POST",
        "/api/v1/auth/login-by-key",
        body={"api_key": api_key, "api_secret": api_secret},
        host=host,
    )

    if status != 200:
        error = result.get("detail", result.get("message", f"HTTP {status}"))
        print(f"❌ 登录失败: {error}")
        print("   请检查 API Key 是否正确")
        sys.exit(1)

    # Save token
    _ensure_dir()
    token_data = {
        "access_token": result.get("access_token", ""),
        "refresh_token": result.get("refresh_token", ""),
        "host": host,
        "logged_in_at": datetime.now(timezone.utc).isoformat(),
    }
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    TOKEN_FILE.chmod(0o600)

    username = result.get("username", result.get("user", {}).get("username", ""))
    print()
    print("✅ 登录成功!")
    if username:
        print(f"   用户: {username}")
    print(f"   Token 已保存到: {TOKEN_FILE}")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _find_workspace(hint: Optional[str] = None) -> Optional[Path]:
    """Find an OpenClaw workspace directory."""
    markers = ["AGENTS.md", "SOUL.md", "IDENTITY.md"]

    # 1. Explicit path
    if hint:
        p = Path(hint).expanduser().resolve()
        if p.is_dir():
            return p
        print(f"⚠️  指定的 workspace 不存在: {hint}")
        return None

    # 2. Current directory
    cwd = Path.cwd()
    if any((cwd / m).exists() for m in markers):
        return cwd

    # 3. Scan ~/.openclaw/
    openclaw_dir = Path.home() / ".openclaw"
    if openclaw_dir.is_dir():
        workspaces = []
        for child in sorted(openclaw_dir.iterdir()):
            if child.is_dir() and child.name.startswith("workspace"):
                if any((child / m).exists() for m in markers):
                    workspaces.append(child)
        if len(workspaces) == 1:
            return workspaces[0]
        if len(workspaces) > 1:
            print("🔍 找到多个 OpenClaw workspace:")
            for i, w in enumerate(workspaces):
                print(f"   [{i + 1}] {w}")
            print()
            choice = input(f"请选择 (1-{len(workspaces)}): ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(workspaces):
                    return workspaces[idx]
            except ValueError:
                pass
            print("❌ 无效选择")
            return None

    return None


def _read_file_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _extract_identity(workspace: Path) -> dict:
    """Extract info from IDENTITY.md."""
    content = _read_file_safe(workspace / "IDENTITY.md")
    info = {}
    # Extract Name
    m = re.search(r"\*\*Name:\*\*\s*(.+)", content)
    if m:
        info["name"] = m.group(1).strip()
    # Extract Creature/Vibe
    m = re.search(r"\*\*Creature:\*\*\s*(.+)", content)
    if m:
        info["creature"] = m.group(1).strip()
    m = re.search(r"\*\*Vibe:\*\*\s*(.+)", content)
    if m:
        info["vibe"] = m.group(1).strip()
    return info


def _extract_soul(workspace: Path) -> dict:
    """Extract description and capabilities from SOUL.md."""
    content = _read_file_safe(workspace / "SOUL.md")
    info = {}

    # Extract first meaningful paragraph after ## 身份 or ## Identity
    desc_match = re.search(
        r"##\s*(?:身份|Identity)\s*\n+(.*?)(?=\n##|\Z)", content, re.DOTALL
    )
    if desc_match:
        lines = [
            l.strip() for l in desc_match.group(1).strip().split("\n") if l.strip()
        ]
        info["description"] = "\n".join(lines[:5])

    # Extract capabilities from skill sections
    caps = []
    for m in re.finditer(r"###\s*\d*\.?\s*(.+?)(?:\s*[\(（]|$)", content):
        cap = m.group(1).strip().rstrip("）)")
        if cap and len(cap) < 30:
            caps.append(cap)
    if caps:
        info["capabilities"] = caps[:8]

    return info


def _extract_agents(workspace: Path) -> dict:
    """Extract supplementary info from AGENTS.md."""
    content = _read_file_safe(workspace / "AGENTS.md")
    info = {}
    # Extract 职责 section
    m = re.search(r"##\s*职责\s*\n+(.*?)(?=\n##|\Z)", content, re.DOTALL)
    if m:
        lines = [
            l.strip().lstrip("- ") for l in m.group(1).strip().split("\n") if l.strip()
        ]
        info["responsibilities"] = lines[:5]
    return info


def cmd_init(args):
    """扫描本地 OpenClaw workspace，生成 agent-config.yaml。"""
    workspace = _find_workspace(args.workspace)
    if not workspace:
        print("❌ 未找到 OpenClaw workspace")
        print("   请使用 --workspace 指定路径，或在 workspace 目录中运行此命令")
        sys.exit(1)

    print(f"🔍 扫描 workspace: {workspace}")

    # Extract info
    identity = _extract_identity(workspace)
    soul = _extract_soul(workspace)
    agents = _extract_agents(workspace)

    name = identity.get("name", "")
    description = soul.get("description", "")
    capabilities = soul.get("capabilities", [])

    # Supplement description from agents
    if not description and agents.get("responsibilities"):
        description = "。".join(agents["responsibilities"][:3])

    # Build config
    output_path = Path(args.output or DEFAULT_CONFIG)
    config = {
        "name": name or "# TODO: 请填写 Agent 名称",
        "description": description or "# TODO: 请填写 Agent 描述（≥50 字符）",
        "capabilities": capabilities or ["# TODO: 请添加能力标签"],
        "examples": [
            {
                "title": "示例 1",
                "input": "# TODO: 请填写用户输入示例",
                "output": "# TODO: 请填写 Agent 回复示例",
            },
            {
                "title": "示例 2",
                "input": "# TODO: 请填写用户输入示例",
                "output": "# TODO: 请填写 Agent 回复示例",
            },
            {
                "title": "示例 3",
                "input": "# TODO: 请填写用户输入示例",
                "output": "# TODO: 请填写 Agent 回复示例",
            },
        ],
        "pricing": {
            "amount": 0,
        },
        "openclaw": {
            "workspace": str(workspace),
            "agent_name": identity.get("name", "").lower().replace(" ", "-") or "",
        },
    }

    # Check existing
    if output_path.exists():
        overwrite = input(f"⚠️  {output_path} 已存在，是否覆盖? [y/N] ").strip().lower()
        if overwrite != "y":
            print("已取消")
            return

    # Write YAML with comments
    _write_config_yaml(output_path, config)

    print()
    print(f"✅ 配置文件已生成: {output_path}")
    print()
    print("📝 请检查并修改配置文件，特别是:")
    if "TODO" in str(config["name"]):
        print("   - name: Agent 名称")
    if "TODO" in str(config["description"]):
        print("   - description: Agent 描述（≥50 字符）")
    if any("TODO" in str(c) for c in config["capabilities"]):
        print("   - capabilities: 能力标签")
    print("   - examples: 示例对话（≥3 组，用于 QA 评测）")
    print()
    print("完成后运行: python3 lobster.py register")


def _write_config_yaml(path: Path, config: dict):
    """Write agent-config.yaml with helpful comments."""
    lines = [
        "# 🦞 龙虾市场 Agent 配置",
        "# 请检查以下信息，修改后运行: python3 lobster.py register",
        "",
        "# Agent 名称（必填，将在市场中展示）",
        f"name: {json.dumps(config['name'], ensure_ascii=False)}",
        "",
        "# 描述（必填，≥50 字符，越详细审核越容易通过）",
        "description: |",
    ]
    desc = config["description"]
    for line in desc.split("\n"):
        lines.append(f"  {line}")
    lines += [
        "",
        "# 能力标签（必填，≥1 个）",
        "capabilities:",
    ]
    for cap in config["capabilities"]:
        lines.append(f"  - {json.dumps(cap, ensure_ascii=False)}")
    lines += [
        "",
        "# 示例对话（必填，≥3 组，用于 QA 评测）",
        "examples:",
    ]
    for ex in config["examples"]:
        lines.append(f"  - title: {json.dumps(ex['title'], ensure_ascii=False)}")
        lines.append(f"    input: {json.dumps(ex['input'], ensure_ascii=False)}")
        lines.append(f"    output: {json.dumps(ex['output'], ensure_ascii=False)}")
    lines += [
        "",
        "# 定价（按次收费）",
        "pricing:",
        f"  amount: {config['pricing']['amount']}  # 单次调用价格（虾米），0 = 免费",
        "",
        "# OpenClaw 配置（自动检测，通常不需要改）",
        "openclaw:",
        f"  workspace: {json.dumps(config['openclaw']['workspace'], ensure_ascii=False)}",
        f"  agent_name: {json.dumps(config['openclaw']['agent_name'], ensure_ascii=False)}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def cmd_register(args):
    """读取配置文件，注册 Agent 到龙虾市场。"""
    token = _require_token()
    config_path = Path(args.config or DEFAULT_CONFIG)

    if not config_path.exists():
        print(f"❌ 配置文件不存在: {config_path}")
        print("   请先运行: python3 lobster.py init")
        sys.exit(1)

    # Load config
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ 配置文件解析失败: {e}")
        sys.exit(1)

    # Validate
    errors = []
    name = config.get("name", "")
    if not name or "TODO" in str(name):
        errors.append("name: 名称不能为空")

    description = config.get("description", "")
    if not description or "TODO" in str(description):
        errors.append("description: 描述不能为空")
    elif len(str(description).strip()) < 50:
        errors.append(
            f"description: 描述至少 50 字符（当前 {len(str(description).strip())} 字符）"
        )

    capabilities = config.get("capabilities", [])
    if not capabilities or any("TODO" in str(c) for c in capabilities):
        errors.append("capabilities: 至少需要 1 个能力标签")

    examples = config.get("examples", [])
    valid_examples = [
        e
        for e in examples
        if isinstance(e, dict)
        and e.get("input")
        and "TODO" not in str(e.get("input", ""))
        and e.get("output")
        and "TODO" not in str(e.get("output", ""))
    ]
    if len(valid_examples) < 3:
        errors.append(
            f"examples: 至少需要 3 组完整示例（当前 {len(valid_examples)} 组有效）"
        )

    if errors:
        print("❌ 配置校验失败:")
        for err in errors:
            print(f"   - {err}")
        print()
        print(f"请修改 {config_path} 后重试")
        sys.exit(1)

    # Show summary
    desc_preview = str(description).strip()[:80]
    caps_str = ", ".join(str(c) for c in capabilities)
    pricing_amount = config.get("pricing", {}).get("amount", 0)

    if not args.yes:
        print()
        print("🦞 即将注册以下 Agent:")
        print()
        print(f"  名称:     {name}")
        print(f"  描述:     {desc_preview}...")
        print(f"  能力:     {caps_str}")
        print(f"  示例:     {len(valid_examples)} 组")
        print(
            f"  定价:     {pricing_amount} 虾米/次"
            if pricing_amount
            else "  定价:     免费"
        )
        print()
        confirm = input("确认注册? [Y/n] ").strip().lower()
        if confirm and confirm != "y":
            print("已取消")
            return

    # Build request body
    body = {
        "name": str(name),
        "description": str(description).strip(),
        "capabilities": [str(c) for c in capabilities],
        "examples": [
            {"title": e.get("title", ""), "input": e["input"], "output": e["output"]}
            for e in valid_examples
        ],
        "pricing": {
            "model": "per_call",
            "amount": pricing_amount,
            "currency": "shrimp_rice",
        },
        "endpoint_url": "https://broker-managed",
    }

    print("📤 正在注册...")
    status_code, result = _http_request(
        "POST",
        "/api/v1/agents/register",
        body=body,
        token=token,
    )

    if status_code not in (200, 201):
        error = result.get("detail", result.get("message", f"HTTP {status_code}"))
        print(f"❌ 注册失败: {error}")
        sys.exit(1)

    agent_id = result.get("agent_id", result.get("id", ""))
    review_status = result.get("status", "pending_review")

    # Save agent info
    _ensure_dir()
    agent_data = {
        "agent_id": agent_id,
        "name": str(name),
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "config_path": str(config_path),
    }
    AGENT_FILE.write_text(json.dumps(agent_data, indent=2))

    print()
    print("✅ 注册成功!")
    print()
    print(f"  Agent ID:  {agent_id}")
    print(f"  状态:      {review_status}（审核中）")
    print()
    print("审核通常 30 分钟内完成，运行查看进度:")
    print("  python3 lobster.py status")


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def cmd_connect(args):
    """启动 adapter + connector，连接龙虾市场。"""
    token = _require_token()
    agent_id = _require_agent_id(args.agent_id)
    port = args.port or DEFAULT_ADAPTER_PORT

    # Read openclaw config
    config_path = Path(DEFAULT_CONFIG)
    agent_name = ""
    if config_path.exists():
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            agent_name = config.get("openclaw", {}).get("agent_name", "")
        except Exception:
            pass

    # Also check saved agent info
    agent_data = _load_agent()
    agent_display_name = ""
    if agent_data:
        agent_display_name = agent_data.get("name", "")

    print("🦞 正在连接龙虾市场...")
    print()

    # 1. Start openclaw_adapter.py
    adapter_script = SCRIPT_DIR / "adapters" / "openclaw_adapter.py"
    if not adapter_script.exists():
        print(f"❌ 找不到 adapter: {adapter_script}")
        sys.exit(1)

    adapter_cmd = [sys.executable, str(adapter_script), "--port", str(port)]
    if agent_name:
        adapter_cmd += ["--agent-name", agent_name]

    adapter_proc = subprocess.Popen(
        adapter_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # 2. Wait for adapter to be ready
    import urllib.request

    health_url = f"http://localhost:{port}/health"
    ready = False
    for i in range(20):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(health_url, timeout=2) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            pass

    if not ready:
        print("❌ Adapter 启动超时")
        adapter_proc.terminate()
        sys.exit(1)

    print(f"  ✅ Adapter 就绪: http://localhost:{port}/execute")

    # 3. Start market-connect.py
    connect_script = SCRIPT_DIR / "market-connect.py"
    if not connect_script.exists():
        print(f"❌ 找不到 connector: {connect_script}")
        adapter_proc.terminate()
        sys.exit(1)

    connect_cmd = [
        sys.executable,
        str(connect_script),
        "--agent-id",
        agent_id,
        "--local-endpoint",
        f"http://localhost:{port}/execute",
    ]

    connect_proc = subprocess.Popen(
        connect_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    host = _get_host()
    print(f"  🔌 连接到 wss://{host}/agent-ws ...")
    if agent_display_name:
        print(f"  ✅ Agent: {agent_display_name} ({agent_id})")
    print("  📡 等待任务中... (Ctrl+C 退出)")
    print()

    # Daemon mode
    if args.daemon:
        PIDS_DIR.mkdir(parents=True, exist_ok=True)
        (PIDS_DIR / "adapter.pid").write_text(str(adapter_proc.pid))
        (PIDS_DIR / "connect.pid").write_text(str(connect_proc.pid))
        print(f"  后台运行中，PID 文件: {PIDS_DIR}/")
        print(
            "  停止: kill $(cat ~/.lobster-market/pids/adapter.pid) $(cat ~/.lobster-market/pids/connect.pid)"
        )
        return

    # Foreground mode: stream logs, handle Ctrl+C
    def _shutdown(signum, frame):
        print("\n👋 正在关闭...")
        connect_proc.terminate()
        adapter_proc.terminate()
        try:
            connect_proc.wait(timeout=5)
        except Exception:
            connect_proc.kill()
        try:
            adapter_proc.wait(timeout=5)
        except Exception:
            adapter_proc.kill()
        print("✅ 已退出")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Stream output from both processes
    import selectors

    sel = selectors.DefaultSelector()
    sel.register(adapter_proc.stdout, selectors.EVENT_READ, "adapter")
    sel.register(connect_proc.stdout, selectors.EVENT_READ, "connect")

    try:
        while True:
            # Check if processes are still alive
            if adapter_proc.poll() is not None and connect_proc.poll() is not None:
                break
            events = sel.select(timeout=1.0)
            for key, _ in events:
                line = key.fileobj.readline()
                if line:
                    prefix = "[adapter]" if key.data == "adapter" else "[connect]"
                    sys.stdout.write(f"{prefix} {line.decode(errors='replace')}")
                    sys.stdout.flush()
    except KeyboardInterrupt:
        _shutdown(None, None)
    finally:
        sel.close()
        adapter_proc.terminate()
        connect_proc.terminate()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args):
    """查看 Agent 的连接状态、审核进度、评级结果。"""
    token = _require_token()
    agent_id = _require_agent_id(args.agent_id)

    # Fetch agent info
    status_code, agent_info = _http_request(
        "GET",
        f"/api/v1/agents/{agent_id}",
        token=token,
    )

    if status_code == 404:
        print(f"❌ Agent 不存在: {agent_id}")
        sys.exit(1)
    if status_code != 200:
        print(f"❌ 获取 Agent 信息失败: HTTP {status_code}")
        sys.exit(1)

    name = agent_info.get("name", "未知")

    # Fetch review status
    _, review_info = _http_request(
        "GET",
        f"/api/v1/agents/{agent_id}/review-status",
        token=token,
    )

    # Fetch health
    _, health_info = _http_request(
        "GET",
        f"/api/v1/agents/{agent_id}/health",
        token=token,
    )

    # Display
    print()
    print(f"🦞 Agent 状态: {name}")
    print("───────────────────────────────")

    # Connection status
    connected = health_info.get("connected", health_info.get("online", False))
    if connected:
        print("  连接:    🟢 在线")
    else:
        print("  连接:    ⚪ 未连接")

    # Review status
    review_status = review_info.get("status", agent_info.get("status", "unknown"))
    if review_status in ("approved", "active", "passed"):
        score = review_info.get("score", review_info.get("rating", {}).get("score"))
        grade = review_info.get("grade", review_info.get("rating", {}).get("grade", ""))
        print("  审核:    ✅ 已通过")
        if grade and score:
            print(f"  评级:    {grade} ({score}/5.0)")
        elif score:
            print(f"  评级:    {score}/5.0")

        # Stats
        calls = agent_info.get(
            "total_calls", agent_info.get("stats", {}).get("total_calls")
        )
        avg_rating = agent_info.get(
            "avg_rating", agent_info.get("stats", {}).get("avg_rating")
        )
        if calls is not None:
            print(f"  调用次数: {calls}")
        if avg_rating is not None:
            print(f"  平均评分: {avg_rating}/5.0")

    elif review_status in ("pending_review", "pending", "evaluating", "in_review"):
        step = review_info.get("current_step", "")
        total_steps = review_info.get("total_steps", 4)
        current_step = review_info.get("step_number", "")
        eta = review_info.get("eta_minutes", "")

        status_text = "🔄 审核中"
        if step and current_step:
            status_text += f"（步骤 {current_step}/{total_steps}: {step}）"
        print(f"  审核:    {status_text}")
        if eta:
            print(f"  预计:    约 {eta} 分钟后完成")

    elif review_status in ("rejected", "failed"):
        score = review_info.get("score", "")
        print("  审核:    ❌ 未通过" + (f" ({score}/5.0)" if score else ""))

        reasons = review_info.get("reasons", review_info.get("failure_reasons", []))
        if reasons:
            print("───────────────────────────────")
            print("  失败原因:")
            for r in reasons:
                print(f"    - {r}")

        suggestions = review_info.get(
            "suggestions", review_info.get("improvement_suggestions", [])
        )
        if suggestions:
            print("  改进建议:")
            for s in suggestions:
                print(f"    - {s}")

        print("───────────────────────────────")
        print("  修改 agent-config.yaml 后重新运行: python3 lobster.py register")
    else:
        print(f"  审核:    {review_status}")

    print("───────────────────────────────")

    # Capabilities
    caps = agent_info.get("capabilities", [])
    if caps:
        print(f"  能力标签: {', '.join(caps)}")

    # Trust tags
    trust = agent_info.get("trust_tags", agent_info.get("trust_level", ""))
    if trust:
        if isinstance(trust, list):
            trust = ", ".join(trust)
        print(f"  信任标签: {trust}")

    print()

    # Hint
    if not connected and review_status in ("approved", "active", "passed"):
        print("  提示: 运行 lobster.py connect 开始接任务")
    elif not connected and review_status in ("pending_review", "pending", "evaluating"):
        print("  提示: 审核通过后运行 lobster.py connect 开始接任务")


# ---------------------------------------------------------------------------
# create — Agent Factory: LLM-powered workspace generation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ClawHub Skill Integration
# ---------------------------------------------------------------------------


def _clawhub_available() -> bool:
    """Check if clawhub CLI is available."""
    try:
        result = subprocess.run(
            ["clawhub", "-V"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _search_skills(keywords: list, limit: int = 5) -> list:
    """Search ClawHub for each keyword, deduplicate and rank results.

    Returns list of dicts: [{slug, name, score, description}]
    """
    seen = {}  # slug -> best entry
    for kw in keywords:
        try:
            result = subprocess.run(
                ["clawhub", "search", kw, "--limit", str(limit)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                continue
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line or line.startswith("-"):
                    continue
                # Format: "slug  Display Name  (score)"
                m = re.match(r"^(\S+)\s+(.+?)\s+\(([0-9.]+)\)$", line)
                if m:
                    slug = m.group(1)
                    name = m.group(2).strip()
                    score = float(m.group(3))
                    if slug not in seen or score > seen[slug]["score"]:
                        seen[slug] = {
                            "slug": slug,
                            "name": name,
                            "score": score,
                            "keyword": kw,
                        }
        except (subprocess.TimeoutExpired, Exception) as e:
            print(f"  ⚠️  搜索 '{kw}' 失败: {e}")

    # Fetch descriptions via inspect for top results
    ranked = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
    for entry in ranked[:10]:
        try:
            result = subprocess.run(
                ["clawhub", "inspect", entry["slug"]],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if line.startswith("Summary:"):
                        entry["description"] = line[len("Summary:") :].strip()
                        break
        except Exception:
            pass

    return ranked


def _check_skill_conflicts(skills: list) -> list:
    """Basic compatibility check between skills.

    Returns list of warning strings.
    """
    warnings = []
    descs = {s["slug"]: s.get("description", "").lower() for s in skills}

    # Check for overlapping functionality
    slugs = list(descs.keys())
    for i in range(len(slugs)):
        for j in range(i + 1, len(slugs)):
            a, b = slugs[i], slugs[j]
            desc_a, desc_b = descs[a], descs[b]
            # Simple keyword overlap check
            words_a = set(desc_a.split())
            words_b = set(desc_b.split())
            # Filter short words
            meaningful_a = {w for w in words_a if len(w) > 4}
            meaningful_b = {w for w in words_b if len(w) > 4}
            overlap = meaningful_a & meaningful_b
            if len(overlap) >= 3:
                warnings.append(
                    f"⚠️  {a} 和 {b} 功能可能重叠（共同关键词: {', '.join(list(overlap)[:3])}）"
                )

    return warnings


def _install_skills(skills: list, workspace_dir: str, interactive: bool = True) -> list:
    """Install selected skills into workspace via clawhub CLI.

    Args:
        skills: list of skill dicts with 'slug', 'name', 'score', 'description'
        workspace_dir: target workspace directory
        interactive: whether to prompt user for confirmation

    Returns:
        list of successfully installed skill dicts
    """
    if not skills:
        return []

    has_clawhub = _clawhub_available()

    # Display recommendations
    print()
    print("🔍 找到以下推荐 Skill：")
    for i, skill in enumerate(skills):
        score_str = f"⭐{skill['score']:.1f}" if skill.get("score") else ""
        desc = skill.get("description", "")
        if len(desc) > 60:
            desc = desc[:57] + "..."
        marker = "✅" if skill["score"] >= 3.5 else "⚠️"
        optional = " [可选]" if skill["score"] < 3.3 else ""
        print(f"  {i + 1}. {marker} {skill['slug']} ({score_str}) — {desc}{optional}")

    # Check conflicts
    warnings = _check_skill_conflicts(skills)
    if warnings:
        print()
        for w in warnings:
            print(f"  {w}")

    if not has_clawhub:
        print()
        print("⚠️  clawhub CLI 未安装，无法自动安装 Skill。")
        print("   安装 clawhub 后手动安装：")
        for skill in skills:
            print(f"     clawhub install {skill['slug']} --workdir {workspace_dir}")
        return []

    # User confirmation
    selected = skills  # default: install all
    if interactive:
        print()
        choice = input("安装以上 Skill？[Y/n/输入编号如 1,3] ").strip()
        if choice.lower() == "n":
            print("  跳过 Skill 安装")
            return []
        elif choice and choice.lower() != "y":
            # Parse selection numbers
            try:
                indices = [int(x.strip()) - 1 for x in choice.split(",")]
                selected = [skills[i] for i in indices if 0 <= i < len(skills)]
            except (ValueError, IndexError):
                print("  ⚠️  无效选择，安装全部推荐 Skill")
                selected = skills

    # Install
    installed = []
    print()
    for skill in selected:
        slug = skill["slug"]
        print(f"  📦 安装 {slug}...", end=" ", flush=True)
        try:
            result = subprocess.run(
                ["clawhub", "install", slug, "--force", "--workdir", workspace_dir],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                print("✅")
                installed.append(skill)
            else:
                err = result.stderr.strip() or result.stdout.strip()
                print(f"❌ ({err[:80]})")
        except subprocess.TimeoutExpired:
            print("❌ (超时)")
        except Exception as e:
            print(f"❌ ({e})")

    return installed


_ANALYZE_PROMPT = """你是 OpenClaw Agent 市场的 Agent 设计专家。
用户想创建一个 Agent，请根据描述输出结构化 Agent Spec。

用户描述：{description}

请严格输出以下 JSON 结构（不要输出其他内容）：
{{
  "name": "英文短横线命名，如 github-ops",
  "display_name": "中文展示名，如 GitHub 运维专家",
  "emoji": "一个贴切的 emoji",
  "description": "50-100字的 Agent 定位描述",
  "role": "角色定位，如 资深 DevOps 工程师",
  "experience": "经验描述，如 10+ 年...",
  "capabilities": ["能力1", "能力2", "能力3"],
  "style": "工作风格，如 严谨、自动化优先",
  "skill_keywords": ["English skill keywords for ClawHub search, e.g. docker, web-search, copywriting"],
  "tools_needed": ["exec", "web_search", "web_fetch", "browser", "read", "write"],
  "examples": [
    {{"title": "示例标题", "input": "用户会说的话（详细）", "output": "Agent 的完整回复（详细，100字以上）"}},
    {{"title": "示例标题2", "input": "...", "output": "..."}},
    {{"title": "示例标题3", "input": "...", "output": "..."}}
  ]
}}

要求：
1. capabilities 3-5 个，简洁有力
2. examples 至少 3 组，input 和 output 都要详细真实
3. tools_needed 从 OpenClaw 可用工具中选择：exec, web_search, web_fetch, browser, read, write, edit, image, pdf, tts, nodes, message
4. name 用英文小写+短横线
5. 风格要鲜明有个性
6. skill_keywords MUST be in English (ClawHub is English-based), e.g. social-media-marketing, web-search, copywriting, docker, summarize"""

_SOUL_PROMPT = """你是 OpenClaw Agent Workspace 配置专家。根据以下 Agent Spec 生成 SOUL.md 文件内容。

Agent Spec：
{spec_json}

参考模板结构（来自我们团队14个高质量Agent的总结）：

```
# SOUL.md - {{display_name}}

## 身份

你是一名{{role}}，拥有{{experience}}。{{description}}

## 一、核心技能矩阵

### 1. 技能A
- 具体能力描述

### 2. 技能B
- 具体能力描述

## 二、核心职责

### 1. 职责A
- 具体任务

### 2. 职责B
- 具体任务

## 三、输出模板

### 模板名称
1. 第一部分
2. 第二部分
...

## 四、工作原则

- DO: 应该做的事
- DO: ...
- DON'T: 不该做的事
- DON'T: ...

## 思维内核

- **批判性思维** — 具体描述
- **成长性思维** — 具体描述

## 风格

- 风格特征1
- 风格特征2
- 风格特征3
```

严格要求：
1. 总行数控制在 50-120 行
2. 必须包含 DO 和 DON'T 行为约束（各至少2条）
3. 输出模板要具体可执行（不是空架子）
4. 风格描述要鲜明有个性，体现该 Agent 的独特气质
5. 直接输出 Markdown 内容，不要包裹在代码块中
6. 用中文书写"""


def _openai_chat(messages: list, json_mode: bool = False, max_retries: int = 2) -> str:
    """Call OpenAI chat completions API using urllib (no deps)."""
    import urllib.request

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        # Try loading from lobster token
        token_data = _load_token()
        if token_data:
            api_key = token_data.get("openai_key", "")
    if not api_key:
        print("❌ 未设置 OPENAI_API_KEY 环境变量")
        print("   请运行: export OPENAI_API_KEY='sk-...'")
        sys.exit(1)

    body = {
        "model": "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.7,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=data,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"  ⚠️  API 调用失败，重试中... ({e})")
                time.sleep(2)
            else:
                print(f"❌ OpenAI API 调用失败: {e}")
                sys.exit(1)
    return ""


def _analyze_requirements(description: str) -> dict:
    """Use LLM to analyze user description and produce Agent Spec."""
    prompt = _ANALYZE_PROMPT.format(description=description)
    messages = [
        {"role": "system", "content": "你是一个 Agent 设计专家，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]

    raw = _openai_chat(messages, json_mode=True)

    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        # Retry once
        print("  ⚠️  JSON 解析失败，重新生成...")
        raw = _openai_chat(messages, json_mode=True)
        spec = json.loads(raw)

    # Validate required fields
    required = ["name", "display_name", "description", "capabilities", "examples"]
    for key in required:
        if key not in spec:
            raise ValueError(f"Agent Spec 缺少字段: {key}")
    if len(spec.get("examples", [])) < 3:
        raise ValueError("示例对话少于 3 组")

    return spec


def _generate_soul(spec: dict) -> str:
    """Use LLM to generate SOUL.md content."""
    spec_json = json.dumps(spec, ensure_ascii=False, indent=2)
    prompt = _SOUL_PROMPT.format(spec_json=spec_json)
    messages = [
        {
            "role": "system",
            "content": "你是 OpenClaw Agent 配置专家，直接输出 Markdown 文件内容。",
        },
        {"role": "user", "content": prompt},
    ]

    content = _openai_chat(messages, json_mode=False)

    # Strip markdown code fences if present
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines)

    # Validate line count
    line_count = len(content.strip().split("\n"))
    if line_count < 30:
        print(f"  ⚠️  SOUL.md 仅 {line_count} 行，可能内容不足")
    elif line_count > 150:
        print(f"  ⚠️  SOUL.md 有 {line_count} 行，略长（建议50-120行）")

    return content


def _generate_workspace(spec: dict, output_dir: str, interactive: bool = True):
    """Generate complete workspace directory from Agent Spec."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "skills").mkdir(exist_ok=True)

    name = spec.get("name", "my-agent")
    display_name = spec.get("display_name", name)
    emoji = spec.get("emoji", "🤖")
    description = spec.get("description", "")
    role = spec.get("role", "AI 助手")
    style = spec.get("style", "")

    # --- IDENTITY.md ---
    identity = f"""# IDENTITY.md

- **Name:** {display_name}
- **Creature:** {role}
- **Vibe:** {style}
- **Emoji:** {emoji}
"""
    (out / "IDENTITY.md").write_text(identity, encoding="utf-8")
    print("  ✅ IDENTITY.md")

    # --- SOUL.md (LLM generated) ---
    print("  ⏳ 生成 SOUL.md（LLM 生成中）...")
    soul_content = _generate_soul(spec)
    (out / "SOUL.md").write_text(soul_content, encoding="utf-8")
    soul_lines = len(soul_content.strip().split("\n"))
    print(f"  ✅ SOUL.md（{soul_lines} 行）")

    # --- AGENTS.md ---
    caps_list = "\n".join(f"- {c}" for c in spec.get("capabilities", []))
    agents = f"""# AGENTS.md - {display_name}

## 职责
{caps_list}

## 工作流程
1. 接收用户任务描述
2. 分析需求并制定方案
3. 执行任务并实时反馈
4. 输出结构化结果

## 📋 任务管理规则

### 接任务
- 收到任务后确认理解需求
- 不确定就问，不要猜

### 完成任务
- 完成后汇报结果
- 包含关键数据和下一步建议

### 🚫 不确定就问
- 不确定的信息，必须问确认，不要猜
- 猜的成本远大于问的成本
"""
    (out / "AGENTS.md").write_text(agents, encoding="utf-8")
    print("  ✅ AGENTS.md")

    # --- TOOLS.md ---
    tools_needed = spec.get("tools_needed", [])
    tools_str = (
        ", ".join(tools_needed) if tools_needed else "exec, web_search, read, write"
    )
    tools = f"""# TOOLS.md - {display_name}

## 推荐工具
{tools_str}

## 环境配置
_在此添加你的环境特定配置，如 API Key、服务器地址等。_

## 示例
```markdown
### API Keys
- SERVICE_API_KEY → 通过环境变量配置

### 常用路径
- 工作目录 → ~/workspace
```
"""
    (out / "TOOLS.md").write_text(tools, encoding="utf-8")
    print("  ✅ TOOLS.md")

    # --- USER.md ---
    user_md = """# USER.md - About Your Human

_Learn about the person you're helping. Update this as you go._

- **Name:**
- **What to call them:**
- **Pronouns:** _(optional)_
- **Timezone:**
- **Notes:**

## Context

_(What do they care about? What projects are they working on?)_
"""
    (out / "USER.md").write_text(user_md, encoding="utf-8")
    print("  ✅ USER.md")

    # --- MEMORY.md ---
    memory_md = """# MEMORY.md

_记录长期经验和学到的教训。_

## 经验记录

_(随着使用逐步积累)_
"""
    (out / "MEMORY.md").write_text(memory_md, encoding="utf-8")
    print("  ✅ MEMORY.md")

    # --- Skill Search & Install ---
    installed_skills = []
    skill_keywords = spec.get("skill_keywords", [])
    if skill_keywords and _clawhub_available():
        print()
        print(f"🔍 [Skill] 根据关键词搜索 ClawHub: {', '.join(skill_keywords)}")
        search_results = _search_skills(skill_keywords, limit=3)
        if search_results:
            # Filter to top relevant results (score >= 3.0)
            relevant = [s for s in search_results if s.get("score", 0) >= 3.0][:8]
            if relevant:
                installed_skills = _install_skills(
                    relevant, output_dir, interactive=interactive
                )
                if installed_skills:
                    print(f"  ✅ 已安装 {len(installed_skills)} 个 Skill")
                else:
                    print("  ℹ️  未安装任何 Skill")
            else:
                print("  ℹ️  未找到高相关度的 Skill")
        else:
            print("  ℹ️  搜索无结果")
    elif skill_keywords and not _clawhub_available():
        print()
        print("⚠️  clawhub CLI 未安装，跳过 Skill 自动安装")
        print("   推荐 Skill 关键词：", ", ".join(skill_keywords))
        print("   安装 clawhub 后手动安装：")
        for kw in skill_keywords:
            print(f"     clawhub search {kw}")

    # --- agent-config.yaml ---
    _generate_agent_config(spec, output_dir, installed_skills=installed_skills)
    print("  ✅ agent-config.yaml")

    skills_dir = out / "skills"
    installed_count = len(list(skills_dir.iterdir())) if skills_dir.exists() else 0
    if installed_count > 0:
        print(f"  ✅ skills/ ({installed_count} 个 Skill 已安装)")
    else:
        print("  ✅ skills/ (空目录)")


def _generate_agent_config(
    spec: dict, output_dir: str, installed_skills: Optional[list] = None
):
    """Generate agent-config.yaml compatible with register command."""
    out = Path(output_dir)
    config = {
        "name": spec.get("display_name", spec.get("name", "")),
        "description": spec.get("description", ""),
        "capabilities": spec.get("capabilities", []),
        "examples": spec.get("examples", []),
        "pricing": {"amount": 0},
        "openclaw": {
            "workspace": str(out.resolve()),
            "agent_name": spec.get("name", ""),
        },
    }
    _write_config_yaml(out / "agent-config.yaml", config)

    # Append skills section if skills were installed
    if installed_skills:
        config_path = out / "agent-config.yaml"
        existing = config_path.read_text(encoding="utf-8")
        skills_lines = ["\n# 已安装的 Skill", "skills:"]
        for skill in installed_skills:
            skills_lines.append(f"  - name: {skill['slug']}")
            desc = skill.get("description", skill.get("name", ""))
            skills_lines.append(
                f"    description: {json.dumps(desc, ensure_ascii=False)}"
            )
        existing += "\n".join(skills_lines) + "\n"
        config_path.write_text(existing, encoding="utf-8")


def cmd_create(args):
    """从自然语言描述一键生成完整 OpenClaw Agent workspace。"""
    description = args.description
    output_dir = args.output
    name = args.name

    # Interactive mode if no description
    if not description:
        print("🦞 Agent Factory — 从描述到 Agent，一步到位")
        print()
        description = input("请描述你想创建的 Agent：").strip()
        if not description:
            print("❌ 描述不能为空")
            sys.exit(1)

    # Default output dir
    if not output_dir:
        slug = name or "my-agent"
        output_dir = f"./{slug}"

    # Check existing directory
    out_path = Path(output_dir)
    if out_path.exists() and any(out_path.iterdir()):
        if not args.yes:
            overwrite = (
                input(f"⚠️  目录 {output_dir} 已存在，是否覆盖? [y/N] ").strip().lower()
            )
            if overwrite != "y":
                print("已取消")
                return
        # Clean existing
        import shutil

        shutil.rmtree(out_path)

    print()
    print(f"📝 用户需求：{description}")
    print()

    # Step 1: Analyze requirements
    print("🔍 [1/3] 分析需求（调用 LLM）...")
    spec = _analyze_requirements(description)

    # Override name if specified
    if name:
        spec["name"] = name

    # Show spec summary
    print()
    print("═══════════════════════════════════════")
    print(f"  {spec.get('emoji', '🤖')} Agent Spec")
    print("═══════════════════════════════════════")
    print(f"  名称：{spec.get('display_name', spec.get('name'))}")
    print(f"  定位：{spec.get('role', 'AI 助手')}")
    print(f"  描述：{spec.get('description', '')[:60]}...")
    caps = spec.get("capabilities", [])
    print(f"  能力：{', '.join(caps)}")
    skills = spec.get("skill_keywords", [])
    if skills:
        print(f"  推荐 Skill：{', '.join(skills)}")
    print(f"  示例：{len(spec.get('examples', []))} 组")
    print("═══════════════════════════════════════")
    print()

    # Confirm (unless --yes)
    if not args.yes:
        confirm = input("确认生成？[Y/n] ").strip().lower()
        if confirm and confirm != "y":
            print("已取消")
            return

    # Step 2: Generate workspace + install skills
    interactive = not args.yes
    print(f"📁 [2/3] 生成 workspace → {output_dir}")
    _generate_workspace(spec, output_dir, interactive=interactive)

    # Step 3: Summary
    print()
    print("🎉 [3/3] 生成完成！")
    print()

    # Build tree display
    out_path = Path(output_dir)
    skills_dir = out_path / "skills"
    installed_skills = []
    if skills_dir.exists():
        installed_skills = [d.name for d in skills_dir.iterdir() if d.is_dir()]

    print(f"  📂 {output_dir}/")
    print("  ├── IDENTITY.md")
    print("  ├── SOUL.md")
    print("  ├── AGENTS.md")
    print("  ├── TOOLS.md")
    print("  ├── USER.md")
    print("  ├── MEMORY.md")
    print("  ├── agent-config.yaml")
    if installed_skills:
        print("  └── skills/")
        for i, s in enumerate(installed_skills):
            connector = "└──" if i == len(installed_skills) - 1 else "├──"
            print(f"      {connector} {s}/")
    else:
        print("  └── skills/")
    print()
    print(f"✅ Agent workspace 已生成：{output_dir}/")
    print()
    print("📋 下一步：")
    print("  1. 检查并调整 SOUL.md（核心人设定义）")
    if not installed_skills:
        print("  2. 安装推荐 Skill（如有）")
    print(
        f"  {'3' if not installed_skills else '2'}. 登录市场：python3 lobster.py login"
    )
    print(f"  {'4' if not installed_skills else '3'}. 注册上架：")
    print(f"     cd {output_dir}")
    print("     python3 lobster.py register --config agent-config.yaml")
    print(
        f"  {'5' if not installed_skills else '4'}. 启动连接：python3 lobster.py connect"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="🦞 龙虾市场 CLI — 将你的 OpenClaw Agent 注册到龙虾市场",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用流程:
  1. python3 lobster.py login       使用 API Key 登录
  2. python3 lobster.py init        生成配置文件
  3. python3 lobster.py register    注册 Agent
  4. python3 lobster.py connect     连接市场
  5. python3 lobster.py status      查看状态

快速创建:
  python3 lobster.py create --description "描述你想要的Agent"
""",
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # login
    p_login = subparsers.add_parser("login", help="使用 API Key 登录龙虾市场")
    p_login.add_argument(
        "--api-key", help="API Key（格式: key:secret），不传则交互式输入"
    )
    p_login.add_argument("--host", help=f"平台地址（默认 {DEFAULT_HOST}）")

    # init
    p_init = subparsers.add_parser("init", help="扫描本地 OpenClaw，生成配置文件")
    p_init.add_argument("--workspace", help="OpenClaw workspace 路径（默认自动检测）")
    p_init.add_argument("--output", help=f"配置文件输出路径（默认 {DEFAULT_CONFIG}）")

    # register
    p_register = subparsers.add_parser("register", help="注册 Agent 到龙虾市场")
    p_register.add_argument("--config", help=f"配置文件路径（默认 {DEFAULT_CONFIG}）")
    p_register.add_argument("--yes", "-y", action="store_true", help="跳过确认直接注册")

    # connect
    p_connect = subparsers.add_parser("connect", help="连接市场，开始接任务")
    p_connect.add_argument(
        "--agent-id", help="Agent UUID（默认从 ~/.lobster-market/agent.json 读取）"
    )
    p_connect.add_argument(
        "--port", type=int, help=f"Adapter 监听端口（默认 {DEFAULT_ADAPTER_PORT}）"
    )
    p_connect.add_argument("--daemon", action="store_true", help="后台运行")

    # status
    p_status = subparsers.add_parser("status", help="查看连接/审核/评级状态")
    p_status.add_argument(
        "--agent-id", help="Agent UUID（默认从 ~/.lobster-market/agent.json 读取）"
    )

    # create
    p_create = subparsers.add_parser(
        "create", help="从自然语言描述生成 Agent workspace"
    )
    p_create.add_argument(
        "--description", "-d", help="Agent 功能描述（不传则交互式输入）"
    )
    p_create.add_argument(
        "--name", "-n", help="Agent 名称（英文短横线，如 github-ops）"
    )
    p_create.add_argument("--output", "-o", help="输出目录（默认 ./<name>）")
    p_create.add_argument("--yes", "-y", action="store_true", help="跳过确认直接生成")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "login": cmd_login,
        "init": cmd_init,
        "register": cmd_register,
        "connect": cmd_connect,
        "status": cmd_status,
        "create": cmd_create,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()

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
import base64
import getpass
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
import urllib.request

# T-591: Bypass system proxy for urllib (prevents 127.0.0.1:7890 interception)
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)

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
    timeout: int = 30,
) -> Tuple[int, dict]:
    """Simple HTTP(S) request using stdlib only."""
    import http.client

    h = host or _get_host()
    conn = http.client.HTTPSConnection(h, timeout=timeout)
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


def _decode_jwt_exp(token: str) -> Optional[float]:
    """Decode JWT payload to extract exp claim (no signature verification)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        # Add padding for base64url
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("exp")
    except Exception:
        return None


def _refresh_access_token(token_data: dict) -> Optional[str]:
    """Call refresh endpoint to get a new access token. Returns new token or None."""
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return None

    host = token_data.get("host") or _get_host()
    try:
        status, result = _http_request(
            "POST",
            "/api/v1/auth/refresh",
            body={"refresh_token": refresh_token},
            host=host,
            timeout=10,
        )
    except Exception as e:
        log.warning("Token refresh request failed: %s", e)
        return None

    if status != 200:
        detail = ""
        if isinstance(result, dict):
            detail = result.get("detail", result.get("message", ""))
        log.warning("Token refresh failed (HTTP %s): %s", status, detail)
        return None

    new_access = result.get("access_token", "")
    new_refresh = result.get("refresh_token", "")
    if not new_access:
        return None

    # Persist refreshed tokens
    token_data["access_token"] = new_access
    if new_refresh:
        token_data["refresh_token"] = new_refresh
    token_data["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _ensure_dir()
        TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
        TOKEN_FILE.chmod(0o600)
    except Exception as e:
        log.warning("Failed to persist refreshed token: %s", e)

    log.info("🔄 Token 自动刷新成功")
    return new_access


_TOKEN_REFRESH_BUFFER_SECONDS = 120  # refresh when ≤120s before expiry


def _require_token() -> str:
    data = _load_token()
    if not data or not data.get("access_token"):
        print("❌ 未登录。请先运行: python3 lobster.py login")
        print("   还没有 API Key？前往: https://mindcore8.com/dashboard/api-keys")
        sys.exit(1)

    access_token = data["access_token"]

    # Check if token is expired or about to expire, then auto-refresh
    exp = _decode_jwt_exp(access_token)
    if exp is not None:
        remaining = exp - time.time()
        if remaining <= _TOKEN_REFRESH_BUFFER_SECONDS:
            log.info(
                "⏳ Token %s，尝试自动刷新...",
                "已过期" if remaining <= 0 else f"将在 {int(remaining)}s 后过期",
            )
            new_token = _refresh_access_token(data)
            if new_token:
                return new_token
            # Refresh failed — if token is truly expired, bail out
            if remaining <= 0:
                print(
                    "❌ Token 已过期且自动刷新失败。请重新登录: python3 lobster.py login"
                )
                sys.exit(1)
            # Token not yet expired, use existing one with warning
            log.warning("⚠️ Token 即将过期且刷新失败，使用当前 token 继续...")

    return access_token


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
        print("   还没有 API Key？前往创建：")
        print("   👉 https://mindcore8.com/dashboard/api-keys")
        print()
        api_key_str = getpass.getpass("请输入 API Key (格式 key:secret): ")

    if ":" not in api_key_str:
        print("❌ API Key 格式错误，正确格式: key:secret")
        print("   前往创建 Master Key: https://mindcore8.com/dashboard/api-keys")
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
        "api_key": api_key,
        "api_secret": api_secret,
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


def _read_workspace_files(args, config: dict) -> dict:
    """Read workspace files (SOUL.md, AGENTS.md, etc.) for Fly deployment.

    Resolution order for workspace directory:
    1. --workspace CLI argument
    2. openclaw.workspace from agent-config.yaml
    3. (skip silently — backwards compatible with non-Fly flow)

    Returns dict like {"soul_md": "...", "agents_md": "...", ...} or empty dict.
    """
    workspace_path_str = getattr(args, "workspace", None)
    if not workspace_path_str:
        workspace_path_str = config.get("openclaw", {}).get("workspace", "")

    if not workspace_path_str:
        return {}

    workspace_dir = Path(workspace_path_str).expanduser().resolve()
    if not workspace_dir.is_dir():
        log.warning("Workspace 目录不存在: %s，跳过 workspace_files", workspace_dir)
        return {}

    # Map: filename → API key
    file_map = {
        "SOUL.md": "soul_md",
        "AGENTS.md": "agents_md",
        "IDENTITY.md": "identity_md",
        "MEMORY.md": "memory_md",
    }

    workspace_files = {}
    for fname, key in file_map.items():
        fpath = workspace_dir / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8")
                if content.strip():
                    workspace_files[key] = content
            except Exception as e:
                log.warning("读取 %s 失败: %s", fpath, e)

    # Skills: read skills/*/SKILL.md, assemble as skills_manifest JSON
    skills_dir = workspace_dir / "skills"
    if skills_dir.exists() and skills_dir.is_dir():
        skills_manifest = []
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                try:
                    content = skill_md.read_text(encoding="utf-8")
                    skills_manifest.append(
                        {
                            "slug": skill_dir.name,
                            "content": content[:4096],  # truncate per spec
                        }
                    )
                except Exception as e:
                    log.warning("读取 %s 失败: %s", skill_md, e)
        if skills_manifest:
            workspace_files["skills_manifest"] = json.dumps(
                skills_manifest, ensure_ascii=False
            )

    if not workspace_files:
        log.warning("Workspace 目录为空或无可读文件: %s", workspace_dir)

    return workspace_files


def cmd_register(args):
    """读取配置文件，注册/更新 Agent 到龙虾市场。

    Supports --agent-id to force-update an existing agent (PUT).
    Without --agent-id, auto-reads from ~/.lobster-market/agent.json for idempotent updates.
    """
    token = _require_token()
    config_path = Path(args.config or DEFAULT_CONFIG)

    # Determine agent_id for update mode
    update_agent_id = getattr(args, "agent_id", None)
    if not update_agent_id:
        # Auto-read from saved agent.json
        saved = _load_agent()
        if saved and saved.get("agent_id"):
            update_agent_id = saved["agent_id"]

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
        action_word = "更新" if update_agent_id else "注册"
        print()
        print(f"🦞 即将{action_word}以下 Agent:")
        if update_agent_id:
            print(f"  Agent ID: {update_agent_id}")
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
        confirm = input(f"确认{action_word}? [Y/n] ").strip().lower()
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

    # workspace_files: read from workspace directory for Fly deployment
    workspace_files = _read_workspace_files(args, config)
    if workspace_files:
        body["workspace_files"] = workspace_files
        print(f"  📂 Workspace 文件: {', '.join(workspace_files.keys())}")

    # card_skills: read from config and include in body
    card_skills = config.get("card_skills", [])
    if card_skills:
        body["card_skills"] = [
            s.get("slug") or s if isinstance(s, dict) else s for s in card_skills
        ]

    # Avatar: generate from emoji if avatar_url is not set
    avatar_emoji = config.get("avatar_emoji", "")
    avatar_url = config.get("avatar_url", "")
    if avatar_emoji and not avatar_url:
        avatar_url = _generate_avatar_data_url(avatar_emoji)
        print(f"  🎨 自动生成 emoji 头像: {avatar_emoji}")
    if avatar_url:
        body["avatar_url"] = avatar_url
    if avatar_emoji:
        body["avatar_emoji"] = avatar_emoji

    # Decide: POST (new) or PUT (update existing)
    if update_agent_id:
        print(f"📤 正在更新 Agent ({update_agent_id[:8]}...)...")
        status_code, result = _http_request(
            "PUT",
            f"/api/v1/agents/{update_agent_id}",
            body=body,
            token=token,
        )
        # Fallback: if PUT returns 404/405, try POST
        if status_code in (404, 405):
            print("  ⚠️  PUT 不支持，回退到 POST 创建...")
            update_agent_id = None
            status_code, result = _http_request(
                "POST",
                "/api/v1/agents/register",
                body=body,
                token=token,
            )
    else:
        print("📤 正在注册...")
        status_code, result = _http_request(
            "POST",
            "/api/v1/agents/register",
            body=body,
            token=token,
        )

    if status_code not in (200, 201):
        error = result.get("detail", result.get("message", f"HTTP {status_code}"))
        action_word = "更新" if update_agent_id else "注册"
        print(f"❌ {action_word}失败: {error}")
        sys.exit(1)

    agent_id = update_agent_id or result.get("agent_id", result.get("id", ""))
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

    is_update = bool(update_agent_id)
    print()
    print(f"✅ {'更新' if is_update else '注册'}成功!")
    print()
    print(f"  Agent ID:  {agent_id}")
    print(f"  状态:      {review_status}{'（已更新）' if is_update else '（审核中）'}")
    if avatar_emoji:
        print(
            f"  头像:      {avatar_emoji}{'（自动生成 SVG）' if not config.get('avatar_url') else ''}"
        )
    print()
    # Auto-connect after registration (unless --no-connect)
    no_connect = getattr(args, "no_connect", False)
    if not no_connect:
        print("🔌 注册成功，自动连接市场（QA 评测需要 Agent 在线）...")
        print("   使用 --no-connect 可跳过自动连接")
        print()
        # Build a minimal args namespace for cmd_connect
        connect_args = argparse.Namespace(
            agent_id=agent_id,
            port=getattr(args, "port", None),
            daemon=getattr(args, "daemon", False),
            stop=False,
        )
        cmd_connect(connect_args)
    else:
        if not is_update:
            print("⚠️  注意: QA 评测需要 Agent 在线，请尽快运行:")
            print("  python3 lobster.py connect")
            print()
            print("审核通常 30 分钟内完成，运行查看进度:")
            print("  python3 lobster.py status")


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def _stop_daemon():
    """Stop running daemon processes via PID files."""
    stopped = False
    for name in ("watchdog", "adapter", "connect"):
        pidfile = PIDS_DIR / f"{name}.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                print(f"  ▸ 已停止 {name} (PID {pid})")
                stopped = True
            except (ProcessLookupError, ValueError):
                pass
            pidfile.unlink(missing_ok=True)
    if stopped:
        print("✅ 后台进程已停止")
    else:
        print("ℹ️  没有正在运行的后台进程")


def cmd_connect(args):
    """启动 adapter + connector，连接龙虾市场。"""
    # Handle --stop
    if getattr(args, "stop", False):
        _stop_daemon()
        return

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

    # Daemon mode: write stdout to log files to avoid PIPE buffer deadlock.
    # When stdout=PIPE but nobody reads the pipe, the OS pipe buffer (64KB)
    # fills up and the child process blocks on write() — silent hang.
    # Foreground mode uses PIPE + selectors to drain output in real time.
    is_daemon = getattr(args, "daemon", False)
    _daemon_log_files = []  # track file handles for cleanup
    if is_daemon:
        PIDS_DIR.mkdir(parents=True, exist_ok=True)
        adapter_log_fh = open(PIDS_DIR / "adapter-restart.log", "w")
        _daemon_log_files.append(adapter_log_fh)
        adapter_stdout = adapter_log_fh
    else:
        adapter_stdout = subprocess.PIPE

    adapter_proc = subprocess.Popen(
        adapter_cmd,
        stdout=adapter_stdout,
        stderr=subprocess.STDOUT,
    )

    # 2. Wait for adapter to be ready

    health_timeout = getattr(args, "health_timeout", None) or 30
    health_url = f"http://localhost:{port}/health"
    ready = False
    max_attempts = int(health_timeout / 0.5)
    for i in range(max_attempts):
        # If the adapter process died, stop waiting immediately
        if adapter_proc.poll() is not None:
            print(f"❌ Adapter 进程已退出 (exit code: {adapter_proc.returncode})")
            sys.exit(1)
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(health_url, timeout=2) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            pass

    if not ready:
        print(f"❌ Adapter 启动超时（等待 {health_timeout}s）")
        print("   可通过 --health-timeout 调大等待时间")
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

    if is_daemon:
        connect_log_fh = open(PIDS_DIR / "connect-restart.log", "w")
        _daemon_log_files.append(connect_log_fh)
        connect_stdout = connect_log_fh
    else:
        connect_stdout = subprocess.PIPE

    connect_proc = subprocess.Popen(
        connect_cmd,
        stdout=connect_stdout,
        stderr=subprocess.STDOUT,
    )

    host = _get_host()
    print(f"  🔌 连接到 wss://{host}/agent-ws ...")
    if agent_display_name:
        print(f"  ✅ Agent: {agent_display_name} ({agent_id})")
    print("  📡 等待任务中... (Ctrl+C 退出)")
    print()

    # Daemon mode with watchdog
    if args.daemon:
        PIDS_DIR.mkdir(parents=True, exist_ok=True)
        log_dir = PIDS_DIR
        _save_daemon_pids(adapter_proc, connect_proc)

        print(f"  后台运行中，PID 文件: {PIDS_DIR}/")
        print("  停止: python3 lobster.py connect --stop")

        # Watchdog: monitor child processes, restart if crashed
        _run_watchdog(
            adapter_cmd=adapter_cmd,
            connect_cmd=connect_cmd,
            adapter_proc=adapter_proc,
            connect_proc=connect_proc,
            health_url=f"http://localhost:{port}/health",
            log_dir=log_dir,
        )
        return

    # Foreground mode: stream logs, handle Ctrl+C
    def _shutdown(signum, frame):
        """信号处理：优雅关闭 adapter 和 connector 子进程。"""
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


def _save_daemon_pids(adapter_proc, connect_proc):
    """Save daemon PID files."""
    PIDS_DIR.mkdir(parents=True, exist_ok=True)
    (PIDS_DIR / "adapter.pid").write_text(str(adapter_proc.pid))
    (PIDS_DIR / "connect.pid").write_text(str(connect_proc.pid))
    # Save watchdog (parent) PID
    (PIDS_DIR / "watchdog.pid").write_text(str(os.getpid()))


def _run_watchdog(
    adapter_cmd,
    connect_cmd,
    adapter_proc,
    connect_proc,
    health_url,
    log_dir,
    check_interval=10,
    max_restarts=50,
):
    """Watchdog loop: monitor adapter + connector, restart on crash.

    Runs as the daemon foreground process (detached from terminal).
    Exits after max_restarts consecutive failures or on SIGTERM.
    """
    restart_counts = {"adapter": 0, "connector": 0}
    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info(
        "🐕 Watchdog 启动，监控间隔 %ds，最大重启 %d 次", check_interval, max_restarts
    )

    while running:
        time.sleep(check_interval)

        # Check adapter
        if adapter_proc.poll() is not None:
            rc = adapter_proc.returncode
            restart_counts["adapter"] += 1
            if restart_counts["adapter"] > max_restarts:
                log.error("❌ Adapter 连续重启超过 %d 次，放弃", max_restarts)
                break
            log.warning(
                "⚠️  Adapter 退出 (code=%s)，第 %d 次重启...",
                rc,
                restart_counts["adapter"],
            )
            adapter_log = log_dir / "adapter-restart.log"
            with open(adapter_log, "a") as lf:
                adapter_proc = subprocess.Popen(
                    adapter_cmd, stdout=lf, stderr=subprocess.STDOUT
                )
            _save_daemon_pids(adapter_proc, connect_proc)
            # Wait for adapter health
            time.sleep(2)
            try:
                with urllib.request.urlopen(health_url, timeout=3) as resp:
                    if resp.status == 200:
                        log.info("✅ Adapter 重启成功 (PID %d)", adapter_proc.pid)
                        restart_counts["adapter"] = 0  # reset on success
            except Exception:
                log.warning("⚠️  Adapter 重启后健康检查失败")
        else:
            restart_counts["adapter"] = 0

        # Check connector
        if connect_proc.poll() is not None:
            rc = connect_proc.returncode
            restart_counts["connector"] += 1
            if restart_counts["connector"] > max_restarts:
                log.error("❌ Connector 连续重启超过 %d 次，放弃", max_restarts)
                break
            log.warning(
                "⚠️  Connector 退出 (code=%s)，第 %d 次重启...",
                rc,
                restart_counts["connector"],
            )
            connect_log = log_dir / "connect-restart.log"
            with open(connect_log, "a") as lf:
                connect_proc = subprocess.Popen(
                    connect_cmd, stdout=lf, stderr=subprocess.STDOUT
                )
            _save_daemon_pids(adapter_proc, connect_proc)
            log.info("✅ Connector 重启成功 (PID %d)", connect_proc.pid)
            restart_counts["connector"] = 0
        else:
            restart_counts["connector"] = 0

    # Cleanup
    log.info("🐕 Watchdog 退出，清理子进程...")
    for proc in (adapter_proc, connect_proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    # Clean PID files
    for f in ("adapter.pid", "connect.pid", "watchdog.pid"):
        (PIDS_DIR / f).unlink(missing_ok=True)
    log.info("✅ Watchdog 已退出")


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

    # Fly Machine status
    fly_machine_id = agent_info.get("fly_machine_id", "")
    fly_machine_status = agent_info.get("fly_machine_status", "")
    fly_region = agent_info.get("fly_region", "")
    if fly_machine_id:
        region_str = f" [{fly_region}]" if fly_region else ""
        status_str = fly_machine_status or "unknown"
        print(f"  Fly Machine: {fly_machine_id} ({status_str}){region_str}")

    # Connection status
    connected = health_info.get("connected", health_info.get("online", False))
    if connected:
        print("  连接:    🟢 在线")
    else:
        print("  连接:    ⚪ 未连接")

    # Review status — response structure from agent-service/qa_evaluation.py:
    #   passed:  {"status": "passed", "result": {"score", "grade", "details", ...}}
    #   failed:  {"status": "failed", "result": {"score", "failure_reasons", "improvement_suggestions"}}
    #   evaluating: {"status": "evaluating", "progress": {"current_step", "steps": [...]}, "estimated_remaining_minutes"}
    #   pending: {"status": "pending", "progress": {...}, "estimated_remaining_minutes"}
    review_status = review_info.get("status", agent_info.get("status", "unknown"))
    result = review_info.get("result", {})

    if review_status in ("approved", "active", "passed"):
        score = result.get("score")
        grade = result.get("grade", "")
        print("  审核:    ✅ 已通过")
        if grade and score:
            print(f"  评级:    {grade} ({score}/5.0)")
        elif score:
            print(f"  评级:    {score}/5.0")

        # Auto tags / trust labels from QA
        auto_tags = result.get("auto_tags", [])
        if auto_tags:
            print(f"  自动标签: {', '.join(auto_tags)}")

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
        progress = review_info.get("progress", {})
        current_step = progress.get("current_step", "")
        steps = progress.get("steps", [])
        eta = review_info.get("estimated_remaining_minutes", "")

        status_text = "🔄 审核中"
        if current_step:
            # Count completed steps
            done = sum(1 for s in steps if s.get("status") == "completed")
            total = len(steps) or 4
            status_text += f"（{done}/{total}: {current_step}）"
        print(f"  审核:    {status_text}")
        if eta:
            print(f"  预计:    约 {eta} 分钟后完成")

        # Show step details
        if steps:
            for s in steps:
                icon = (
                    "✅"
                    if s.get("status") == "completed"
                    else "⏳"
                    if s.get("status") == "in_progress"
                    else "⬜"
                )
                print(f"    {icon} {s.get('name', '?')}")

    elif review_status in ("rejected", "failed"):
        score = result.get("score", "")
        print("  审核:    ❌ 未通过" + (f" ({score}/5.0)" if score else ""))

        reasons = result.get("failure_reasons", [])
        if reasons:
            print("───────────────────────────────")
            print("  失败原因:")
            for r in reasons:
                print(f"    - {r}")

        suggestions = result.get("improvement_suggestions", [])
        if suggestions:
            print("  改进建议:")
            for s in suggestions:
                print(f"    - {s}")

        print("───────────────────────────────")
        print("  修改 agent-config.yaml 后重新运行: python3 lobster.py register")
    else:
        print(f"  审核:    {review_status}")
        if review_status == "not_evaluated":
            msg = review_info.get("message", "")
            if msg:
                print(f"  说明:    {msg}")

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
# push — Skills Bundle Upload (T-854)
# ---------------------------------------------------------------------------

SKILLS_EXCLUDE_PATTERNS = {
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
    ".git/",
    "node_modules/",
    "venv/",
    ".venv/",
    ".ruff_cache/",
    "*.egg-info/",
}
MAX_SINGLE_SKILL_BYTES = 1 * 1024 * 1024  # 1MB per skill (uncompressed)
MAX_BUNDLE_BYTES = 5 * 1024 * 1024  # 5MB compressed bundle
MAX_SKILL_COUNT = 50


def _collect_skills(workspace: Path) -> dict:
    """Collect user-defined skills from workspace/skills/.

    Phase 1: Only user-defined skills (not OpenClaw public skills).
    Returns dict of {skill_name: skill_dir_path}.
    """
    skills = {}
    ws_skills = workspace / "skills"
    if ws_skills.is_dir():
        for skill_dir in sorted(ws_skills.iterdir()):
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                skills[skill_dir.name] = skill_dir

    if skills:
        log.info("📦 Found %d user skills", len(skills))

    if len(skills) > MAX_SKILL_COUNT:
        log.warning("⚠️  Skills count %d exceeds limit %d", len(skills), MAX_SKILL_COUNT)

    return skills


def _should_exclude(rel_path: str) -> bool:
    """Check if a file should be excluded from the bundle."""
    import fnmatch

    for pattern in SKILLS_EXCLUDE_PATTERNS:
        if pattern.endswith("/"):
            if f"/{pattern}" in f"/{rel_path}/" or rel_path.startswith(pattern):
                return True
        elif "*" in pattern:
            if fnmatch.fnmatch(rel_path.split("/")[-1], pattern):
                return True
        elif pattern in rel_path:
            return True
    return False


def _create_skills_bundle(skills: dict) -> Path:
    """Create a tar.gz bundle from collected skills.

    Output structure inside tar:
      skills/
        my-skill/
          SKILL.md
          references/
            ...
    """
    import tarfile
    import tempfile

    bundle_path = Path(tempfile.mktemp(suffix=".tar.gz", prefix="lobster-skills-"))

    with tarfile.open(str(bundle_path), "w:gz") as tar:
        for skill_name, skill_dir in sorted(skills.items()):
            # Compute single skill size
            skill_size = sum(
                f.stat().st_size for f in skill_dir.rglob("*") if f.is_file()
            )
            if skill_size > MAX_SINGLE_SKILL_BYTES:
                log.warning(
                    "⚠️  Skill '%s' too large (%d KB), skipping",
                    skill_name,
                    skill_size // 1024,
                )
                continue

            for file_path in sorted(skill_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(skill_dir.parent)
                if _should_exclude(str(rel)):
                    continue
                tar.add(str(file_path), arcname=f"skills/{rel}")

    # Check total size
    bundle_size = bundle_path.stat().st_size
    if bundle_size > MAX_BUNDLE_BYTES:
        bundle_path.unlink()
        print(
            f"❌ Skills bundle too large: {bundle_size // 1024} KB (limit: {MAX_BUNDLE_BYTES // 1024} KB)"
        )
        sys.exit(1)

    return bundle_path


def _upload_skills_bundle(
    token: str, agent_id: str, bundle_path: Path
) -> Tuple[int, dict]:
    """HTTP multipart upload skills tar.gz to the platform."""
    import http.client

    boundary = f"----LobsterSkillsBundle{uuid.uuid4().hex[:12]}"
    host = _get_host()

    with open(bundle_path, "rb") as f:
        file_data = f.read()

    body_parts = []
    body_parts.append(f"--{boundary}\r\n".encode())
    body_parts.append(
        'Content-Disposition: form-data; name="bundle"; filename="skills.tar.gz"\r\n'.encode()
    )
    body_parts.append(b"Content-Type: application/gzip\r\n\r\n")
    body_parts.append(file_data)
    body_parts.append(f"\r\n--{boundary}--\r\n".encode())

    body = b"".join(body_parts)

    conn = http.client.HTTPSConnection(host, timeout=60)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {token}",
    }
    conn.request("PUT", f"/api/v1/agents/{agent_id}/skills-bundle", body, headers)
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()

    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        result = {"raw": raw}
    return resp.status, result


def cmd_push(args):
    """打包 workspace skills 并上传到平台。"""
    token = _require_token()
    agent_id = _require_agent_id(getattr(args, "agent_id", None))

    # Find workspace
    workspace_hint = getattr(args, "workspace", None)
    if not workspace_hint:
        # Try reading from agent-config.yaml
        config_path = Path(getattr(args, "config", None) or DEFAULT_CONFIG)
        if config_path.exists():
            try:
                config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                workspace_hint = config.get("openclaw", {}).get("workspace", "")
            except Exception:
                pass

    workspace = _find_workspace(workspace_hint)
    if not workspace:
        print("❌ 未找到 OpenClaw workspace")
        print("   请使用 --workspace 指定路径")
        sys.exit(1)

    # 1. Collect skills
    print(f"🔍 扫描 skills: {workspace}")
    skills = _collect_skills(workspace)

    if not skills:
        print("ℹ️  未找到任何 skills (需要 workspace/skills/*/SKILL.md)")
        return

    # 2. Create bundle
    print(f"📦 打包 {len(skills)} 个 skills...")
    bundle_path = _create_skills_bundle(skills)
    bundle_size = bundle_path.stat().st_size

    # 3. Show summary
    print("\n📦 Skills Bundle:")
    print(f"   大小: {bundle_size / 1024:.1f} KB (压缩后)")
    print(f"   Skills: {len(skills)} 个")
    for name in sorted(skills.keys()):
        print(f"     📁 {name}")
    print()

    # 4. Confirm
    yes = getattr(args, "yes", False)
    if not yes:
        confirm = input("确认上传? [Y/n] ").strip().lower()
        if confirm and confirm != "y":
            print("已取消")
            bundle_path.unlink()
            return

    # 5. Upload
    print("📤 上传中...")
    status, result = _upload_skills_bundle(token, agent_id, bundle_path)
    bundle_path.unlink(missing_ok=True)

    if status in (200, 201):
        version = result.get("version", "?")
        skill_count = result.get("skill_count", len(skills))
        print(f"✅ 上传成功! 版本: {version} ({skill_count} skills)")
    else:
        error = result.get("detail", result.get("message", f"HTTP {status}"))
        print(f"❌ 上传失败: {error}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# create — Agent Factory: LLM-powered workspace generation
# ---------------------------------------------------------------------------


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
5. 风格要鲜明有个性"""

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


def _get_llm_config() -> dict:
    """Get LLM configuration from environment variables with sensible defaults."""
    model = os.environ.get("LOBSTER_MODEL", "gpt-4o-mini")
    api_base = os.environ.get("LOBSTER_API_BASE", "https://api.openai.com/v1")
    api_key = os.environ.get("LOBSTER_API_KEY", "") or os.environ.get(
        "OPENAI_API_KEY", ""
    )
    if not api_key:
        # Try loading from lobster token
        token_data = _load_token()
        if token_data:
            api_key = token_data.get("openai_key", "")
    return {"model": model, "api_base": api_base.rstrip("/"), "api_key": api_key}


def _openai_chat(messages: list, json_mode: bool = False, max_retries: int = 2) -> str:
    """Call OpenAI-compatible chat completions API using urllib (no deps).

    Supports configuration via environment variables:
      LOBSTER_MODEL    — model name (default: gpt-4o-mini)
      LOBSTER_API_BASE — API base URL (default: https://api.openai.com/v1)
      LOBSTER_API_KEY  — API key (fallback: OPENAI_API_KEY)
    """

    llm_cfg = _get_llm_config()
    api_key = llm_cfg["api_key"]
    if not api_key:
        print("❌ 未设置 API Key 环境变量")
        print("   请运行: export LOBSTER_API_KEY='sk-...'")
        print("   或者:   export OPENAI_API_KEY='sk-...'")
        sys.exit(1)

    body = {
        "model": llm_cfg["model"],
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

    api_url = f"{llm_cfg['api_base']}/chat/completions"

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                api_url,
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
                print(f"❌ LLM API 调用失败: {e}")
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


def _generate_workspace(spec: dict, output_dir: str):
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

    # --- agent-config.yaml ---
    _generate_agent_config(spec, output_dir)
    # Write avatar_emoji into agent-config.yaml if LLM generated an emoji
    if spec.get("emoji"):
        config_path = out / "agent-config.yaml"
        existing = config_path.read_text(encoding="utf-8")
        existing += f"\n# Avatar emoji（register 时自动生成头像）\navatar_emoji: {json.dumps(spec['emoji'], ensure_ascii=False)}\n"
        config_path.write_text(existing, encoding="utf-8")
    print("  ✅ agent-config.yaml")
    print("  ✅ skills/ (空目录)")


def _generate_agent_config(spec: dict, output_dir: str):
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


def _generate_avatar_data_url(emoji: str) -> str:
    """Generate a simple SVG data URL with the given emoji as avatar."""
    import base64

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">'
        '<rect width="100" height="100" rx="20" fill="#f0f0f0"/>'
        f'<text x="50" y="50" font-size="50" text-anchor="middle" dominant-baseline="central">{emoji}</text>'
        "</svg>"
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def cmd_create(args):
    """从自然语言描述一键生成完整 OpenClaw Agent workspace。"""
    description = args.description
    output_dir = args.output
    name = args.name

    # Print LLM configuration
    llm_cfg = _get_llm_config()
    print(f"🤖 使用模型: {llm_cfg['model']}  (API: {llm_cfg['api_base']})")

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
    print(f"  示例：{len(spec.get('examples', []))} 组")
    print("═══════════════════════════════════════")
    print()

    # Confirm (unless --yes)
    if not args.yes:
        confirm = input("确认生成？[Y/n] ").strip().lower()
        if confirm and confirm != "y":
            print("已取消")
            return

    # Step 2: Generate workspace files
    print(f"📁 [2/3] 生成 workspace 文件 → {output_dir}")
    _generate_workspace(spec, output_dir)

    print()
    print("🎉 [3/3] 生成完成！")
    print()

    # Build tree display
    out_path = Path(output_dir)
    print(f"  📂 {output_dir}/")
    print("  ├── IDENTITY.md")
    print("  ├── SOUL.md")
    print("  ├── AGENTS.md")
    print("  ├── TOOLS.md")
    print("  ├── USER.md")
    print("  ├── MEMORY.md")
    print("  ├── agent-config.yaml")
    print("  └── skills/")
    print()
    print(f"✅ Agent workspace 已生成：{output_dir}/")
    print()
    print("📋 下一步：")
    print("  1. 检查并调整 SOUL.md（核心人设定义）")
    print("  2. 登录市场：python3 lobster.py login")
    print("  3. 注册上架：")
    print(f"     cd {output_dir}")
    print("     python3 lobster.py register --config agent-config.yaml")
    print("  4. 启动连接：python3 lobster.py connect")


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
    p_register = subparsers.add_parser(
        "register", help="注册/更新 Agent 并自动连接市场"
    )
    p_register.add_argument("--config", help=f"配置文件路径（默认 {DEFAULT_CONFIG}）")
    p_register.add_argument(
        "--agent-id",
        help="指定 Agent UUID 强制更新（默认从 ~/.lobster-market/agent.json 读取）",
    )
    p_register.add_argument(
        "--workspace",
        help="OpenClaw workspace 目录路径（读取 SOUL.md 等文件传给 API，用于 Fly 部署）",
    )
    p_register.add_argument("--yes", "-y", action="store_true", help="跳过确认直接注册")
    p_register.add_argument(
        "--no-connect",
        action="store_true",
        help="仅注册，不自动连接市场（默认注册后自动连接，确保 QA 评测时 Agent 在线）",
    )
    p_register.add_argument(
        "--port",
        type=int,
        help=f"自动连接时 Adapter 监听端口（默认 {DEFAULT_ADAPTER_PORT}）",
    )
    p_register.add_argument(
        "--daemon",
        action="store_true",
        help="自动连接时后台运行（含守护重启）",
    )

    # connect
    p_connect = subparsers.add_parser("connect", help="连接市场，开始接任务")
    p_connect.add_argument(
        "--agent-id", help="Agent UUID（默认从 ~/.lobster-market/agent.json 读取）"
    )
    p_connect.add_argument(
        "--port", type=int, help=f"Adapter 监听端口（默认 {DEFAULT_ADAPTER_PORT}）"
    )
    p_connect.add_argument(
        "--daemon", action="store_true", help="后台运行（含自动守护重启）"
    )
    p_connect.add_argument("--stop", action="store_true", help="停止后台运行的进程")
    p_connect.add_argument(
        "--health-timeout",
        type=int,
        default=30,
        dest="health_timeout",
        help="Adapter 健康检查等待秒数（默认 30，旧版为 10）",
    )

    # status
    p_status = subparsers.add_parser("status", help="查看连接/审核/评级状态")
    p_status.add_argument(
        "--agent-id", help="Agent UUID（默认从 ~/.lobster-market/agent.json 读取）"
    )

    # push (T-854)
    p_push = subparsers.add_parser("push", help="打包并上传 workspace skills 到平台")
    p_push.add_argument(
        "--agent-id", help="Agent UUID（默认从 ~/.lobster-market/agent.json 读取）"
    )
    p_push.add_argument(
        "--workspace", help="OpenClaw workspace 路径（默认从 agent-config.yaml 读取）"
    )
    p_push.add_argument("--config", help=f"配置文件路径（默认 {DEFAULT_CONFIG}）")
    p_push.add_argument("--yes", "-y", action="store_true", help="跳过确认直接上传")

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
        "push": cmd_push,
        "create": cmd_create,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()

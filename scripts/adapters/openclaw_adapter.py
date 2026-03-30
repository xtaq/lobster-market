#!/usr/bin/env python3
"""OpenClaw 适配器 — 将任务转发到 openclaw agent。

用法:
  python3 openclaw_adapter.py --port 8902 --agent-name backend
  python3 openclaw_adapter.py --port 8902 --agent-name backend --agent-id UUID --token JWT

标准接口:
  POST /execute  — 执行任务（调用 openclaw CLI）
  GET  /health   — 健康检查

T-908: 支持检测 Agent 输出中的本地文件引用，自动上传到 OSS 并替换为公开 URL。
"""

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import mimetypes as _mt
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    from aiohttp import web

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("openclaw-adapter")

# ---------------------------------------------------------------------------
# 全局配置（由 main() 解析参数后设置）
# ---------------------------------------------------------------------------

AGENT_NAME = ""
AGENT_ID = ""
TOKEN = ""

# Token 自动刷新相关
TOKEN_FILE = Path.home() / ".lobster-market" / "token.json"
_TOKEN_REFRESH_BUFFER = 120  # 提前 2 分钟刷新
_REFRESH_HOST = "mindcore8.com"


def _get_fresh_token() -> str:
    """每次调用时获取最新 token，过期前自动刷新。
    
    优先从 token.json 读取（market-connect 也会刷新写入同一文件），
    如果即将过期则主动刷新。回退到全局 TOKEN。
    """
    global TOKEN
    
    # 尝试从文件读取最新 token
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        access_token = data.get("access_token", "")
    except Exception:
        return TOKEN  # 文件读取失败，用全局缓存
    
    if not access_token:
        return TOKEN
    
    # 解析 JWT 检查过期时间
    try:
        payload_b64 = access_token.split(".")[1]
        # 补齐 base64 padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        remaining = payload.get("exp", 0) - int(time.time())
    except Exception:
        return access_token  # 解析失败，直接用
    
    if remaining > _TOKEN_REFRESH_BUFFER:
        # token 还有效，更新全局缓存
        TOKEN = access_token
        return access_token
    
    # 即将过期，尝试刷新
    log.info("⏳ Token 即将过期（剩余 %ds），自动刷新...", remaining)
    try:
        new_token = _do_token_refresh(data)
        if new_token:
            TOKEN = new_token
            log.info("🔄 Token 自动刷新成功")
            return new_token
    except Exception as e:
        log.warning("⚠️ Token 刷新失败: %s", e)
    
    # 刷新失败，返回当前 token（可能已过期但也只能试试）
    return access_token


def _do_token_refresh(token_data: dict) -> str | None:
    """调用 /api/v1/auth/refresh 获取新 token 并写回文件。"""
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        log.warning("⚠️ 无 refresh_token，无法刷新")
        return None
    
    host = token_data.get("host", _REFRESH_HOST)
    req = urllib.request.Request(
        f"https://{host}/api/v1/auth/refresh",
        data=json.dumps({"refresh_token": refresh_token}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    
    new_access = result.get("access_token")
    if not new_access:
        return None
    
    # 写回文件
    token_data["access_token"] = new_access
    if result.get("refresh_token"):
        token_data["refresh_token"] = result["refresh_token"]
    try:
        TOKEN_FILE.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
        TOKEN_FILE.chmod(0o600)
    except Exception as e:
        log.warning("⚠️ 写入 token 文件失败: %s", e)
    
    return new_access

# ---------------------------------------------------------------------------
# T-908: 媒体上传集成
# ---------------------------------------------------------------------------
# Phase 1 只支持绝对路径（G3 m1 决策）

# 正则 1: Markdown 图片 ![alt](/absolute/path/to/file.ext)
_RE_MD_IMAGE = re.compile(r"(!\[[^\]]*\]\()(/[^)\s]+)(\))")

# 正则 2: 独立绝对路径（行首或空白后，到空白或行尾）
# 只匹配白名单扩展名，避免误判普通路径（如 /usr/bin/python）
_UPLOADABLE_EXTS = (
    r"\.(?:png|jpg|jpeg|gif|webp|svg|bmp|tiff"
    r"|mp4|webm|mov|avi"
    r"|pdf)"
)
_RE_STANDALONE_PATH = re.compile(
    rf"(?:^|(?<=\s))(/[^\s]+{_UPLOADABLE_EXTS})(?=\s|$)",
    re.MULTILINE | re.IGNORECASE,
)

# T-908 Phase 3: base64 data URL 检测
# 匹配 data:image/png;base64,... 和 data:video/mp4;base64,...
# base64 内容可能非常长，需 re.DOTALL 不影响（base64 无换行符）
_RE_DATA_URL = re.compile(
    r"data:(image/(?:png|jpeg|gif|webp)|video/(?:mp4|webm));base64,([A-Za-z0-9+/=]+)"
)

# MIME → 文件扩展名映射
_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

# T-908 Phase 3: base64 data URL 大小上限（防 OOM）
MAX_B64_DECODE_BYTES = 100 * 1024 * 1024  # 100MB（与 MAX_FILE_SIZE 对齐）
MAX_B64_STRING_LEN = int(MAX_B64_DECODE_BYTES * 4 / 3) + 100  # base64 编码膨胀约 4/3

# T-908 Phase 3: 并行上传并发控制
_UPLOAD_CONCURRENCY = 5

# 延迟导入 media_uploader（同目录的上一级 scripts/）
_uploader = None


def _get_uploader():
    """延迟导入 media_uploader 模块，避免启动时硬依赖。"""
    global _uploader
    if _uploader is None:
        # media_uploader.py 在 scripts/ 目录，adapter 在 scripts/adapters/
        scripts_dir = str(Path(__file__).resolve().parent.parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import media_uploader

        _uploader = media_uploader
    return _uploader


@dataclass
class UploadedFile:
    """上传成功的文件元信息，用于生成 type:file artifact part。"""

    url: str
    filename: str
    mime_type: str
    size: int


@dataclass
class _PendingUpload:
    """待上传的文件信息（统一本地路径和 base64 解码的临时文件）。"""

    filepath: str
    match_str: str  # 原始匹配到的完整文本（用于替换）
    is_temp: bool = False  # 是否为 base64 解码的临时文件，上传后需删除


def _collect_local_file_matches(text: str) -> list[_PendingUpload]:
    """收集文本中的本地文件路径引用。"""
    uploader = _get_uploader()
    pending: list[_PendingUpload] = []
    seen_paths: set[str] = set()

    # Pass 1: Markdown 图片 ![alt](/path)
    for m in _RE_MD_IMAGE.finditer(text):
        filepath = m.group(2)
        if filepath not in seen_paths and uploader.is_uploadable(filepath):
            seen_paths.add(filepath)
            pending.append(_PendingUpload(filepath=filepath, match_str=filepath))

    # Pass 2: 独立绝对路径
    for m in _RE_STANDALONE_PATH.finditer(text):
        filepath = m.group(1)
        if filepath not in seen_paths and uploader.is_uploadable(filepath):
            seen_paths.add(filepath)
            pending.append(_PendingUpload(filepath=filepath, match_str=filepath))

    return pending


def _collect_data_url_matches(text: str) -> list[_PendingUpload]:
    """检测文本中的 base64 data URL，解码为临时文件，返回待上传列表。

    T-908 Phase 3: base64 data URL 检测与上传。
    解码失败的 data URL 会被跳过（不影响主流程）。
    """
    pending: list[_PendingUpload] = []

    for m in _RE_DATA_URL.finditer(text):
        mime_type = m.group(1)
        b64_data = m.group(2)
        full_match = m.group(0)
        ext = _MIME_TO_EXT.get(mime_type, ".bin")

        # M1: base64 长度上限检查，防止超大 data URL 导致 OOM
        if len(b64_data) > MAX_B64_STRING_LEN:
            log.warning(
                "base64 data URL too large (%d chars, limit %d), skipping",
                len(b64_data),
                MAX_B64_STRING_LEN,
            )
            continue

        try:
            file_bytes = base64.b64decode(b64_data, validate=True)
        except Exception as exc:
            log.warning(
                "⚠️ base64 decode failed for %s data URL, skipping: %s", mime_type, exc
            )
            continue

        # 写入临时文件
        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix=ext, prefix="lobster_b64_", delete=False
            )
            tmp.write(file_bytes)
            tmp.close()
            pending.append(
                _PendingUpload(filepath=tmp.name, match_str=full_match, is_temp=True)
            )
            log.info(
                "📦 Decoded base64 %s → %s (%d bytes)",
                mime_type,
                tmp.name,
                len(file_bytes),
            )
        except Exception as exc:
            log.warning("⚠️ Failed to write temp file for base64 data URL: %s", exc)
            continue

    return pending


def _upload_single_sync(
    pu: _PendingUpload, agent_id: str, token: str
) -> tuple[_PendingUpload, UploadedFile | None]:
    """同步上传单个文件，返回 (pending, uploaded_file_or_none)。"""
    uploader = _get_uploader()
    # 每次上传前获取最新 token（自动刷新）
    fresh_token = _get_fresh_token()
    try:
        url = uploader.upload_file(pu.filepath, agent_id, fresh_token)
        if url:
            p = Path(pu.filepath)
            mime = _mt.guess_type(str(p))[0] or "application/octet-stream"
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            return pu, UploadedFile(url=url, filename=p.name, mime_type=mime, size=size)
        return pu, None
    finally:
        # 清理临时文件
        if pu.is_temp:
            try:
                os.unlink(pu.filepath)
            except OSError:
                pass


async def _upload_and_replace(
    text: str, agent_id: str, token: str
) -> tuple[str, list[UploadedFile]]:
    """异步扫描文本中的本地文件和 base64 data URL，并行上传后替换为 OSS URL。

    T-908 Phase 3 增强:
      - 支持 base64 data URL 检测与上传
      - asyncio.gather + Semaphore 并行上传（最多 5 并发）
      - 保持结果顺序与检测顺序一致
    """
    # 1. 收集所有待上传项
    local_pending = await asyncio.to_thread(_collect_local_file_matches, text)
    data_url_pending = await asyncio.to_thread(_collect_data_url_matches, text)

    # M2: data URL 去重 — 相同 data URL 只上传一次
    dedup_cache: dict[str, str] = {}  # hash(match_str) → uploaded url
    unique_data_pending: list[_PendingUpload] = []
    _seen_hashes: dict[str, _PendingUpload] = {}  # hash → first PendingUpload
    for pu in data_url_pending:
        h = hashlib.sha256(pu.match_str.encode()).hexdigest()
        if h not in _seen_hashes:
            _seen_hashes[h] = pu
            unique_data_pending.append(pu)
        else:
            # 重复 data URL，清理临时文件
            if pu.is_temp:
                try:
                    os.unlink(pu.filepath)
                except OSError:
                    pass
            log.info(
                "🔁 Duplicate data URL detected (hash=%s…), will reuse upload", h[:12]
            )

    if len(data_url_pending) != len(unique_data_pending):
        log.info(
            "🔁 Deduped data URLs: %d → %d unique",
            len(data_url_pending),
            len(unique_data_pending),
        )

    all_pending = local_pending + unique_data_pending

    if not all_pending:
        return text, []

    log.info(
        "📎 Found %d file(s) to upload (%d local, %d base64)",
        len(all_pending),
        len(local_pending),
        len(data_url_pending),
    )

    # 2. 并行上传（Semaphore 控制并发）
    sem = asyncio.Semaphore(_UPLOAD_CONCURRENCY)

    async def _sem_upload(
        pu: _PendingUpload,
    ) -> tuple[_PendingUpload, UploadedFile | None]:
        async with sem:
            return await asyncio.to_thread(_upload_single_sync, pu, agent_id, token)

    results = await asyncio.gather(*[_sem_upload(pu) for pu in all_pending])

    # 3. 按结果替换文本（data URL 用 replace 全量替换以覆盖去重的重复项）
    uploaded_files: list[UploadedFile] = []
    for pu, uf in results:
        if uf:
            if pu.is_temp:
                # data URL：替换所有出现（去重后复用同一个 URL）
                text = text.replace(pu.match_str, uf.url)
            else:
                text = text.replace(pu.match_str, uf.url, 1)
            uploaded_files.append(uf)
        else:
            # M3: upload_file 返回 None 时记录 warning
            log.warning(
                "⚠️ Upload returned None for %s (temp=%s)",
                pu.filepath,
                pu.is_temp,
            )
            if not pu.is_temp:
                # 本地文件上传失败 → 替换为失败提示
                text = text.replace(pu.match_str, f"[上传失败: {pu.filepath}]", 1)

    return text, uploaded_files


# ---------------------------------------------------------------------------
# 核心处理逻辑
# ---------------------------------------------------------------------------


def _extract_text(message) -> str:
    if isinstance(message, dict):
        parts = message.get("parts", [])
        texts = [
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(texts) if texts else json.dumps(message, ensure_ascii=False)
    return str(message)


async def _run_openclaw(message_text: str) -> str:
    cmd = ["openclaw", "agent"]
    if AGENT_NAME:
        cmd += ["--agent", AGENT_NAME]
    cmd += ["--message", message_text]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1200)

    if proc.returncode != 0:
        err = stderr.decode().strip() or stdout.decode().strip() or f"openclaw exit code {proc.returncode}"
        raise RuntimeError(err)

    # Filter out [plugins] and other noise lines from stdout
    lines = stdout.decode().splitlines()
    clean_lines = [
        l for l in lines
        if not l.startswith("[plugins]")
        and not l.startswith("Require stack:")
        and not l.startswith("- /")
    ]
    return "\n".join(clean_lines).strip()


async def handle_execute(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"status": "failed", "error": "Invalid JSON"}, status=400
        )

    task_id = body.get("task_id", "")
    message = body.get("message", {})
    user_text = _extract_text(message)
    log.info("📥 Execute task: %s", task_id)

    try:
        result_text = await _run_openclaw(user_text)
    except Exception as e:
        log.error("❌ OpenClaw failed: %s", e)
        return web.json_response({"status": "failed", "error": str(e)})

    # T-908: 检测本地文件引用并上传替换
    uploaded_files: list[UploadedFile] = []
    if AGENT_ID and TOKEN:
        try:
            result_text, uploaded_files = await _upload_and_replace(
                result_text, AGENT_ID, TOKEN
            )
        except Exception as e:
            log.warning("⚠️ Media upload/replace failed (non-fatal): %s", e)

    # T-908 Phase 2: 构建 artifact parts — text part + file parts
    parts: list[dict] = [{"type": "text", "text": result_text}]
    for uf in uploaded_files:
        parts.append(
            {
                "type": "file",
                "url": uf.url,
                "filename": uf.filename,
                "mime_type": uf.mime_type,
                "size": uf.size,
            }
        )
    if uploaded_files:
        log.info("📎 %d file part(s) attached to artifacts", len(uploaded_files))

    log.info("✅ Task completed: %s", task_id)
    return web.json_response(
        {
            "status": "completed",
            "artifacts": [
                {
                    "name": "任务结果",
                    "parts": parts,
                    "metadata": {"mime_type": "text/markdown"},
                }
            ],
        }
    )


async def handle_health(request):
    info = {
        "status": "ok",
        "adapter": "openclaw",
        "agent": AGENT_NAME,
        "media_upload": bool(AGENT_ID and (TOKEN or TOKEN_FILE.exists())),
    }
    # Try to detect workspace info
    try:
        proc = await asyncio.create_subprocess_exec(
            "openclaw",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            info["openclaw_status"] = stdout.decode().strip()[:500]
    except Exception:
        pass
    return web.json_response(info)


def main():
    global AGENT_NAME, AGENT_ID, TOKEN

    parser = argparse.ArgumentParser(description="OpenClaw 适配器")
    parser.add_argument("--port", type=int, default=8902)
    parser.add_argument("--agent-name", default="", help="OpenClaw agent name")
    parser.add_argument(
        "--agent-id",
        default="",
        help="Agent UUID（用于媒体上传，也可通过 LOBSTER_AGENT_ID 环境变量传入）",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Bearer token（用于媒体上传，也可通过 LOBSTER_TOKEN 环境变量传入）",
    )
    args = parser.parse_args()

    AGENT_NAME = args.agent_name

    # T-908 G5 fix: env var takes precedence over CLI arg to encourage secure usage
    # CLI --token kept for backward compat but env var LOBSTER_TOKEN is preferred
    TOKEN = os.environ.get("LOBSTER_TOKEN", "") or args.token
    AGENT_ID = os.environ.get("LOBSTER_AGENT_ID", "") or args.agent_id

    # 如果没传 token 但 token.json 存在，从文件读取
    if not TOKEN and TOKEN_FILE.exists():
        try:
            td = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            TOKEN = td.get("access_token", "")
            if not AGENT_ID:
                # 也尝试从 agent.json 读取 agent_id
                agent_file = Path.home() / ".lobster-market" / "agent.json"
                if agent_file.exists():
                    AGENT_ID = json.loads(agent_file.read_text(encoding="utf-8")).get("agent_id", "")
            log.info("📎 从 token.json 读取凭证（支持自动刷新）")
        except Exception as e:
            log.warning("⚠️ 读取 token.json 失败: %s", e)

    if AGENT_ID and (TOKEN or TOKEN_FILE.exists()):
        log.info("📎 媒体上传已启用 (agent_id=%s…, auto_refresh=True)", AGENT_ID[:8])
    else:
        missing = []
        if not AGENT_ID:
            missing.append("agent-id")
        if not TOKEN and not TOKEN_FILE.exists():
            missing.append("token")
        log.info(
            "📎 媒体上传未启用（缺少 %s），Agent 输出中的本地文件路径将保持原样",
            ", ".join(missing),
        )

    if not HAS_AIOHTTP:
        print("❌ aiohttp required. Run: pip3 install aiohttp", file=sys.stderr)
        sys.exit(1)

    app = web.Application()
    app.router.add_post("/execute", handle_execute)
    app.router.add_get("/health", handle_health)
    log.info(
        "🚀 OpenClaw Adapter on port %d (agent=%s)",
        args.port,
        AGENT_NAME or "(default)",
    )
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()

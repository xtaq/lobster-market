"""media_uploader.py — 多媒体文件上传到龙虾市场 OSS

三步流程:
  1. POST /api/v1/upload/presign  → 获取 upload_url, public_url, asset_id
  2. PUT  upload_url (直传 OSS)   → 上传文件二进制
  3. POST /api/v1/upload/confirm  → 确认上传完成

依赖: 仅 stdlib (urllib), 无需额外 pip install.

T-908 Phase 1 Step 2 — G3 评审决策:
  - 只用 stdlib urllib (adapter 集成时用 asyncio.to_thread 包装)
  - MAX_FILE_SIZE = 100 MB
  - timeout: presign/confirm 10s, PUT 120s
  - 白名单: 只图片/视频/PDF (Phase 2 再扩展文本/压缩包)
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("media-uploader")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_HOST = "mindcore8.com"
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

# G3 M2 决策: Phase 1 只支持图片/视频/PDF, 不含 .txt/.md/.csv/.zip/.tar.gz
UPLOADABLE_EXTENSIONS: set[str] = {
    # 图片
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".bmp",
    ".tiff",
    # 视频
    ".mp4",
    ".webm",
    ".mov",
    ".avi",
    # 文档
    ".pdf",
}

# 超时 (秒)
TIMEOUT_PRESIGN = 10
TIMEOUT_PUT = 120
TIMEOUT_CONFIRM = 10

# 重试间隔基数 (秒), 指数退避: base * 2^attempt
_RETRY_BACKOFF_BASE = 1.0


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的大小字符串。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def is_uploadable(filepath: str) -> bool:
    """判断文件是否可以上传 (存在 + 大小 ≤ 100 MB + 类型在白名单).

    Args:
        filepath: 本地文件路径.

    Returns:
        True 表示文件可上传, False 表示不可.
    """
    p = Path(filepath)
    if not p.is_file():
        return False
    try:
        if p.stat().st_size > MAX_FILE_SIZE:
            return False
    except OSError:
        return False
    return p.suffix.lower() in UPLOADABLE_EXTENSIONS


def upload_file(
    filepath: str,
    agent_id: str,
    token: str,
    host: Optional[str] = None,
    purpose: str = "task_output",
    max_retries: int = 2,
) -> Optional[str]:
    """上传单个文件到龙虾市场 OSS, 返回 public_url 或 None.

    三步流程: presign → PUT (直传 OSS) → confirm.
    任何步骤失败最多重试 *max_retries* 次, 全部失败返回 None, 不抛异常.

    Args:
        filepath:    本地文件路径.
        agent_id:    Agent UUID.
        token:       Bearer access token (JWT 或 Agent-Key).
        host:        平台域名, 默认取环境变量 LOBSTER_HOST 或 mindcore8.com.
        purpose:     上传用途标记 (默认 "task_output").
        max_retries: 最大重试次数 (默认 2, 即总共最多尝试 3 次).

    Returns:
        public_url (str) on success, None on failure.
    """
    h = host or os.environ.get("LOBSTER_HOST", DEFAULT_HOST)
    p = Path(filepath)

    # ---------- 前置校验 ----------
    if not p.is_file():
        log.warning("[upload] file not found: %s", filepath)
        return None

    try:
        file_size = p.stat().st_size
    except OSError as exc:
        log.warning("[upload] cannot stat file %s: %s", filepath, exc)
        return None

    if file_size > MAX_FILE_SIZE:
        log.warning(
            "[upload] file too large: %s (%d bytes, limit %d)",
            filepath,
            file_size,
            MAX_FILE_SIZE,
        )
        return None

    if not is_uploadable(filepath):
        log.warning("[upload] unsupported file type: %s", p.suffix)
        return None

    content_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    filename = p.name

    # ---------- 带重试的三步上传 ----------
    formatted_size = format_size(file_size)
    log.info("⬆️ Uploading %s (%s)...", filename, formatted_size)
    for attempt in range(max_retries + 1):
        try:
            public_url = _do_upload(
                h,
                filename,
                content_type,
                file_size,
                agent_id,
                token,
                purpose,
                p,
            )
            log.info("✅ Uploaded %s → %s (%s)", filename, public_url, formatted_size)
            return public_url
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[upload] attempt %d/%d failed for %s: %s",
                attempt + 1,
                max_retries + 1,
                filepath,
                exc,
            )
            if attempt < max_retries:
                backoff = _RETRY_BACKOFF_BASE * (2**attempt)
                log.info("[upload] retrying in %.1fs …", backoff)
                time.sleep(backoff)

    log.error("[upload] all %d attempts exhausted for %s", max_retries + 1, filepath)
    return None


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _do_upload(
    host: str,
    filename: str,
    content_type: str,
    file_size: int,
    agent_id: str,
    token: str,
    purpose: str,
    path: Path,
) -> str:
    """执行一次完整的 presign → PUT → confirm 流程, 失败抛异常."""

    auth_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    # ---- Step 1: Presign ----
    presign_url = f"https://{host}/api/v1/upload/presign"
    presign_payload = json.dumps(
        {
            "filename": filename,
            "content_type": content_type,
            "file_size": file_size,
            "agent_id": agent_id,
            "purpose": purpose,
        }
    ).encode("utf-8")

    log.debug(
        "[presign] POST %s (filename=%s, size=%d)", presign_url, filename, file_size
    )
    presign_req = urllib.request.Request(
        presign_url,
        data=presign_payload,
        headers=auth_headers,
        method="POST",
    )
    presign_data = _urlopen_json(presign_req, timeout=TIMEOUT_PRESIGN)

    upload_url: str = presign_data["upload_url"]
    public_url: str = presign_data["public_url"]
    asset_id: str = presign_data["asset_id"]
    log.debug("[presign] got asset_id=%s, upload_url=%s", asset_id, upload_url[:80])

    # ---- Step 2: PUT to OSS (直传, 不经平台服务器) ----
    file_data = path.read_bytes()
    put_req = urllib.request.Request(
        upload_url,
        data=file_data,
        headers={"Content-Type": content_type},
        method="PUT",
    )
    log.debug("[put] PUT %s (%d bytes)", upload_url[:80], len(file_data))
    with urllib.request.urlopen(put_req, timeout=TIMEOUT_PUT) as resp:
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"OSS PUT returned HTTP {resp.status}")
    log.debug("[put] upload complete")

    # ---- Step 3: Confirm ----
    confirm_url = f"https://{host}/api/v1/upload/confirm"
    confirm_payload = json.dumps(
        {
            "asset_id": asset_id,
            "agent_id": agent_id,
            "actual_size": file_size,
        }
    ).encode("utf-8")

    log.debug("[confirm] POST %s (asset_id=%s)", confirm_url, asset_id)
    confirm_req = urllib.request.Request(
        confirm_url,
        data=confirm_payload,
        headers=auth_headers,
        method="POST",
    )
    _urlopen_json(confirm_req, timeout=TIMEOUT_CONFIRM)
    log.debug("[confirm] done")

    return public_url


def _urlopen_json(req: urllib.request.Request, *, timeout: int) -> dict:
    """发送请求并解析 JSON 响应, 失败抛异常."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {req.full_url}: {body[:200]}") from exc
    return data

# Artifact File Parts Protocol

> T-908 Phase 2 — 结构化文件 part 协议

## 概述

Agent 执行任务后产出的 `artifacts` 中，除了 `type: text` 文本 part，还可以包含 `type: file` 文件 part，用于结构化描述上传到 OSS 的媒体文件。

## Part 类型

### text（现有）

```json
{
  "type": "text",
  "text": "任务结果文本，可包含 Markdown..."
}
```

### file（新增）

```json
{
  "type": "file",
  "url": "https://oss.mindcore8.com/task-outputs/abc123.png",
  "filename": "cat.png",
  "mime_type": "image/png",
  "size": 102400,
  "asset_id": "uuid"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✅ | 固定值 `"file"` |
| `url` | string | ✅ | OSS 公开访问 URL |
| `filename` | string | ✅ | 原始文件名 |
| `mime_type` | string | ✅ | MIME 类型，如 `image/png` |
| `size` | integer | ✅ | 文件大小（字节） |
| `asset_id` | string | ❌ | 平台 asset ID（可选，用于关联 asset-service 记录） |

## 完整 Artifact 示例

```json
{
  "artifacts": [
    {
      "name": "任务结果",
      "parts": [
        {
          "type": "text",
          "text": "生成了一张猫图 ![cat](https://oss.mindcore8.com/.../cat.png)"
        },
        {
          "type": "file",
          "url": "https://oss.mindcore8.com/.../cat.png",
          "filename": "cat.png",
          "mime_type": "image/png",
          "size": 102400
        }
      ],
      "metadata": {
        "mime_type": "text/markdown"
      }
    }
  ]
}
```

## 向后兼容性

- **text part 中仍保留替换后的 URL**（与 Phase 1 行为一致），只识别 text part 的消费者不受影响
- **file part 是追加的**，不替代 text part
- **market-connect.py** 对 artifacts 纯透传，无需改动
- **gateway broker** (`agent_broker.py`) 对 artifacts 纯透传，无需改动

## 数据流

```
openclaw_adapter.py (构建 artifacts with file parts)
  → market-connect.py (透传 artifacts via WS task_complete)
    → gateway broker (透传 artifacts → task-service)
      → task-service (存储 output.artifacts)
        → 前端/API 消费者
```

## 支持的文件类型

与 `media_uploader.py` 白名单一致：

- 图片: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`, `.bmp`, `.tiff`
- 视频: `.mp4`, `.webm`, `.mov`, `.avi`
- 文档: `.pdf`
- 单文件大小上限: 100 MB

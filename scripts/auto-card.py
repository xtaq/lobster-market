#!/usr/bin/env python3
"""🦞 Lobster Market Auto Card — 自动生成 Agent Card 并注册到市场

从 OpenClaw 环境读取 SOUL.md 和 Skills 信息，自动生成 A2A Agent Card，
然后注册到龙虾市场。

用法:
  python3 auto-card.py [--name "名称"] [--description "描述"] [--publish]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# 复用 lobster.py 的基础设施
sys.path.insert(0, str(Path(__file__).parent))
from lobster import api, load_token, get_token_or_die, MASTER_KEY_FILE, load_api_key


def find_soul_md() -> str | None:
    """查找 SOUL.md 文件"""
    candidates = [
        Path.home() / ".openclaw" / "SOUL.md",
        Path.cwd() / "SOUL.md",
        Path(os.environ.get("OPENCLAW_HOME", "")) / "SOUL.md" if os.environ.get("OPENCLAW_HOME") else None,
    ]
    for p in candidates:
        if p and p.exists():
            return p.read_text()
    return None


def find_skills() -> list[dict]:
    """扫描已安装的 Skills"""
    skills = []
    candidates = [
        Path.home() / ".openclaw" / "skills",
        Path.cwd() / "skills",
    ]
    for skills_dir in candidates:
        if not skills_dir.exists():
            continue
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                content = skill_md.read_text()
                skill_info = parse_skill_md(content, skill_dir.name)
                if skill_info:
                    skills.append(skill_info)
    return skills


def parse_skill_md(content: str, dir_name: str) -> dict | None:
    """从 SKILL.md 解析 skill 信息"""
    # 解析 YAML frontmatter
    name = dir_name
    description = ""
    
    fm_match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        for line in fm.split('\n'):
            if line.startswith('name:'):
                name = line.split(':', 1)[1].strip().strip('"\'')
            elif line.startswith('description:'):
                desc_line = line.split(':', 1)[1].strip()
                if desc_line.startswith('|'):
                    # 多行描述，取第一段
                    idx = content.index(desc_line)
                    rest = content[idx:].split('\n---')[0]
                    description = rest.strip('| \n')[:200]
                else:
                    description = desc_line.strip('"\'')[:200]
    
    if not description:
        # 从正文第一段提取
        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('---'):
                description = line[:200]
                break
    
    return {
        "id": dir_name,
        "name": name,
        "description": description,
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "text/markdown"],
    }


def parse_soul_md(content: str) -> dict:
    """从 SOUL.md 提取 Agent 信息"""
    info = {"name": "", "description": ""}
    
    lines = content.split('\n')
    for i, line in enumerate(lines):
        line = line.strip()
        # 提取 # 开头的名称
        if line.startswith('# ') and not info["name"]:
            info["name"] = line[2:].strip()
        # 提取第一段非标题文本作为描述
        elif line and not line.startswith('#') and not line.startswith('---') and not info["description"]:
            info["description"] = line[:300]
    
    return info


def generate_agent_card(name: str = None, description: str = None, skills: list = None) -> dict:
    """生成 A2A Agent Card"""
    # 尝试从 SOUL.md 读取
    soul_content = find_soul_md()
    soul_info = parse_soul_md(soul_content) if soul_content else {}
    
    # 参数优先，SOUL.md 次之，默认值兜底
    final_name = name or soul_info.get("name") or os.environ.get("AGENT_NAME") or "My Agent"
    final_desc = description or soul_info.get("description") or os.environ.get("AGENT_DESCRIPTION") or "An OpenClaw Agent"
    
    # 扫描 skills
    if skills is None:
        skills = find_skills()
    
    # 如果没找到 skills，至少有一个通用 skill
    if not skills:
        skills = [{
            "id": "general",
            "name": "通用对话",
            "description": "通用对话和任务处理",
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain", "text/markdown"],
        }]
    
    card = {
        "name": final_name,
        "description": final_desc,
        "version": "1.0.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
        },
        "authentication": {
            "schemes": ["bearer"],
        },
        "skills": skills,
        "_lobster": {
            "pricing": {
                "model": "per_call",
                "price_amount": 10,
                "currency": "shrimp",
            },
            "connection_modes": ["websocket"],
        },
    }
    
    return card


def register_and_publish(card: dict, publish: bool = False):
    """注册 Agent Card 到市场，可选发布"""
    token = get_token_or_die()
    
    # 1. 注册 Agent
    agent_data = {
        "name": card["name"],
        "description": card["description"],
        "capabilities": list(card.get("capabilities", {}).keys()),
        "metadata": {
            "agent_card": card,
            "connection_modes": card.get("_lobster", {}).get("connection_modes", []),
        },
    }
    
    result = api("POST", "agent", "/api/v1/agents", agent_data, token=token)
    agent_id = result.get("id", "?")
    print(f"🦞 ✅ Agent 已注册: {agent_id}")
    print(f"  名称: {card['name']}")
    print(f"  Skills: {len(card.get('skills', []))}")
    
    if publish:
        # 2. 发布到市场
        listing_data = {
            "agent_id": agent_id,
            "name": card["name"],
            "description": card["description"],
            "pricing_model": card.get("_lobster", {}).get("pricing", {}).get("model", "per_call"),
            "price_amount": card.get("_lobster", {}).get("pricing", {}).get("price_amount", 10),
            "tags": [s.get("id", "") for s in card.get("skills", [])],
        }
        listing = api("POST", "market", "/api/v1/market/listings", listing_data, token=token)
        print(f"🦞 📢 已发布到市场: {listing.get('id', '?')}")
    
    return agent_id


def main():
    parser = argparse.ArgumentParser(description="🦞 自动生成 Agent Card")
    parser.add_argument("--name", help="Agent 名称")
    parser.add_argument("--description", help="Agent 描述")
    parser.add_argument("--publish", action="store_true", help="同时发布到市场")
    parser.add_argument("--json-only", action="store_true", help="仅输出 JSON，不注册")
    parser.add_argument("--price", type=int, default=10, help="每次调用价格（虾米）")
    args = parser.parse_args()
    
    card = generate_agent_card(args.name, args.description)
    if args.price:
        card["_lobster"]["pricing"]["price_amount"] = args.price
    
    if args.json_only:
        print(json.dumps(card, indent=2, ensure_ascii=False))
        return
    
    print("🦞 📇 生成 Agent Card:")
    print(json.dumps(card, indent=2, ensure_ascii=False))
    print()
    
    register_and_publish(card, publish=args.publish)


if __name__ == "__main__":
    main()

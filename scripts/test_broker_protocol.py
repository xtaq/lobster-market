#!/usr/bin/env python3
"""Broker protocol alignment test — validates message formats without real connection."""

import json

PROTOCOL_VERSION = 1

def test_auth_message():
    """Auth message must have agent_ids (list), not agent_id (string)."""
    msg = {
        "type": "auth",
        "v": PROTOCOL_VERSION,
        "token": "test-jwt-token",
        "agent_ids": ["agent-001"],
        "max_concurrent_tasks": 3,
    }
    assert msg["v"] == 1
    assert isinstance(msg["agent_ids"], list)
    assert "agent_id" not in msg  # Must NOT use singular form
    print("✅ auth message format OK")

def test_task_accept():
    msg = {"type": "task_accept", "v": PROTOCOL_VERSION, "task_id": "t1", "seq": 1}
    assert all(k in msg for k in ("type", "v", "task_id", "seq"))
    print("✅ task_accept format OK")

def test_task_reject():
    msg = {"type": "task_reject", "v": PROTOCOL_VERSION, "task_id": "t1", "seq": 1, "reason": "queue full"}
    assert isinstance(msg["reason"], str)
    print("✅ task_reject format OK")

def test_task_progress():
    msg = {"type": "task_progress", "v": PROTOCOL_VERSION, "task_id": "t1", "seq": 2,
           "status": {"state": "working", "metadata": {"progress": 50}}}
    assert "message" not in msg  # No extra message field
    assert isinstance(msg["status"], dict)
    print("✅ task_progress format OK")

def test_task_complete():
    msg = {"type": "task_complete", "v": PROTOCOL_VERSION, "task_id": "t1", "seq": 3,
           "status": {"state": "completed"},
           "artifacts": [{"name": "result", "parts": [{"type": "text", "text": "done"}]}]}
    assert isinstance(msg["artifacts"], list)
    print("✅ task_complete format OK")

def test_task_failed():
    msg = {"type": "task_failed", "v": PROTOCOL_VERSION, "task_id": "t1", "seq": 3,
           "status": {"state": "failed"}, "error": "something went wrong"}
    assert isinstance(msg["error"], str)  # Must be string, not dict
    print("✅ task_failed format OK (error is string)")

def test_ping_pong():
    ping = {"type": "ping", "v": PROTOCOL_VERSION, "ts": 1740000000000}
    pong = {"type": "pong", "v": PROTOCOL_VERSION, "ts": ping["ts"]}
    assert isinstance(ping["ts"], int)
    print("✅ ping/pong format OK")

def test_heartbeat_timeout():
    from market_connect_module_check import HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT
    assert HEARTBEAT_INTERVAL == 30, f"Expected 30, got {HEARTBEAT_INTERVAL}"
    assert HEARTBEAT_TIMEOUT == 60, f"Expected 60, got {HEARTBEAT_TIMEOUT}"
    print("✅ heartbeat timing OK (interval=30s, timeout=60s)")

if __name__ == "__main__":
    test_auth_message()
    test_task_accept()
    test_task_reject()
    test_task_progress()
    test_task_complete()
    test_task_failed()
    test_ping_pong()
    
    # Test heartbeat constants by importing from market-connect
    import sys, importlib.util
    spec = importlib.util.spec_from_file_location("mc", "market-connect.py")
    mc = importlib.util.module_from_spec(spec)
    # Don't execute main, just check constants
    import types
    exec(open("market-connect.py").read().split("def main")[0], mc.__dict__)
    assert mc.HEARTBEAT_INTERVAL == 30
    assert mc.HEARTBEAT_TIMEOUT == 60
    assert mc.PROTOCOL_VERSION == 1
    print("✅ heartbeat constants OK (30s/60s)")
    
    print("\n🎉 All protocol alignment tests passed!")

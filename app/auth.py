"""
认证模块
"""
from datetime import datetime, timedelta
from typing import Optional
import secrets
import hashlib
import json
from pathlib import Path

# 写死的用户凭证
USERNAME = "adminkk"
PASSWORD = "Admin888"
PASSWORD_HASH = hashlib.sha256(PASSWORD.encode()).hexdigest()

# Session 存储
SESSIONS_FILE = Path(__file__).parent.parent / "data" / "sessions.json"
SESSION_EXPIRE_HOURS = 24


def _load_sessions() -> dict:
    """从文件加载 session"""
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}


def _save_sessions(sessions: dict):
    """保存 session 到文件"""
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSIONS_FILE, 'w') as f:
        json.dump(sessions, f)


def verify_password(username: str, password: str) -> bool:
    """验证用户名和密码"""
    if username != USERNAME:
        return False
    return hashlib.sha256(password.encode()).hexdigest() == PASSWORD_HASH


def create_session(username: str) -> str:
    """创建 session，返回 session token"""
    sessions = _load_sessions()
    
    # 清理过期 session
    now = datetime.now()
    sessions = {
        k: v for k, v in sessions.items()
        if datetime.fromisoformat(v["expires_at"]) > now
    }
    
    token = secrets.token_urlsafe(32)
    sessions[token] = {
        "username": username,
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(hours=SESSION_EXPIRE_HOURS)).isoformat()
    }
    
    _save_sessions(sessions)
    return token


def get_session(token: str) -> Optional[dict]:
    """获取 session"""
    if not token:
        return None
    
    sessions = _load_sessions()
    session = sessions.get(token)
    
    if not session:
        return None
    
    # 检查是否过期
    if datetime.now() > datetime.fromisoformat(session["expires_at"]):
        del sessions[token]
        _save_sessions(sessions)
        return None
    
    return session


def delete_session(token: str):
    """删除 session"""
    sessions = _load_sessions()
    if token in sessions:
        del sessions[token]
        _save_sessions(sessions)


def is_valid_session(token: str) -> bool:
    """检查 session 是否有效"""
    return get_session(token) is not None
"""
数据库模型和操作
"""
import aiosqlite
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = DATA_DIR / "monitor.db"


async def init_db():
    """初始化数据库表"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    async with aiosqlite.connect(DB_PATH) as db:
        # 服务器表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER DEFAULT 22,
                auth_type TEXT DEFAULT 'key',
                username TEXT DEFAULT 'root',
                key_path TEXT,
                key_content TEXT,
                password TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        
        # 指标记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER,
                cpu_percent REAL,
                memory_percent REAL,
                memory_used INTEGER,
                memory_total INTEGER,
                disk_data TEXT,  -- JSON 格式存储多个磁盘
                collected_at TEXT,
                FOREIGN KEY (server_id) REFERENCES servers(id)
            )
        """)
        
        # 告警记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER,
                alert_type TEXT,  -- cpu, memory, disk
                message TEXT,
                threshold REAL,
                actual_value REAL,
                status TEXT DEFAULT 'active',  -- active, resolved
                created_at TEXT,
                resolved_at TEXT,
                FOREIGN KEY (server_id) REFERENCES servers(id)
            )
        """)
        
        # Docker 容器配置表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS container_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER,
                container_name TEXT NOT NULL,
                custom_commands TEXT,  -- JSON 格式，自定义命令列表
                monitor_enabled INTEGER DEFAULT 1,
                created_at TEXT,
                FOREIGN KEY (server_id) REFERENCES servers(id)
            )
        """)
        
        # 批量命令历史
        await db.execute("""
            CREATE TABLE IF NOT EXISTS batch_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                target_servers TEXT,  -- JSON 格式，目标服务器ID列表
                results TEXT,  -- JSON 格式，执行结果
                executed_at TEXT
            )
        """)
        
        # 告警阈值配置
        await db.execute("""
            CREATE TABLE IF NOT EXISTS thresholds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric_type TEXT UNIQUE,  -- cpu, memory, disk
                warning_threshold REAL DEFAULT 80,
                critical_threshold REAL DEFAULT 90,
                enabled INTEGER DEFAULT 1
            )
        """)
        
        # 插入默认阈值
        await db.execute("""
            INSERT OR IGNORE INTO thresholds (metric_type, warning_threshold, critical_threshold)
            VALUES 
                ('cpu', 80, 90),
                ('memory', 80, 90),
                ('disk', 85, 95)
        """)
        
        await db.commit()


# ============ 服务器操作 ============

async def add_server(
    name: str,
    host: str,
    port: int = 22,
    auth_type: str = "key",
    username: str = "root",
    key_path: Optional[str] = None,
    key_content: Optional[str] = None,
    password: Optional[str] = None,
    enabled: bool = True
) -> int:
    """添加服务器"""
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """
            INSERT INTO servers (name, host, port, auth_type, username, key_path, key_content, password, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, host, port, auth_type, username, key_path, key_content, password, int(enabled), now, now)
        )
        await db.commit()
        return cursor.lastrowid


async def get_servers(enabled_only: bool = True) -> List[Dict[str, Any]]:
    """获取所有服务器"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if enabled_only:
            cursor = await db.execute("SELECT * FROM servers WHERE enabled = 1 ORDER BY name")
        else:
            cursor = await db.execute("SELECT * FROM servers ORDER BY name")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_server(server_id: int) -> Optional[Dict[str, Any]]:
    """获取单个服务器"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM servers WHERE id = ?", (server_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_server(server_id: int, **kwargs) -> bool:
    """更新服务器"""
    allowed_fields = {'name', 'host', 'port', 'auth_type', 'username', 'key_path', 'key_content', 'password', 'enabled'}
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
    if not updates:
        return False
    
    updates['updated_at'] = datetime.now().isoformat()
    set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE servers SET {set_clause} WHERE id = ?",
            list(updates.values()) + [server_id]
        )
        await db.commit()
    return True


async def delete_server(server_id: int) -> bool:
    """删除服务器"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM servers WHERE id = ?", (server_id,))
        await db.commit()
    return True


# ============ 指标操作 ============

async def save_metrics(
    server_id: int,
    cpu_percent: float,
    memory_percent: float,
    memory_used: int,
    memory_total: int,
    disk_data: List[Dict[str, Any]]
) -> int:
    """保存指标"""
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """
            INSERT INTO metrics (server_id, cpu_percent, memory_percent, memory_used, memory_total, disk_data, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (server_id, cpu_percent, memory_percent, memory_used, memory_total, json.dumps(disk_data), now)
        )
        await db.commit()
        return cursor.lastrowid


async def get_latest_metrics(server_id: int) -> Optional[Dict[str, Any]]:
    """获取服务器最新指标"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM metrics WHERE server_id = ? ORDER BY collected_at DESC LIMIT 1",
            (server_id,)
        )
        row = await cursor.fetchone()
        if row:
            result = dict(row)
            result['disk_data'] = json.loads(result['disk_data'])
            return result
    return None


async def get_metrics_history(server_id: int, hours: int = 24) -> List[Dict[str, Any]]:
    """获取历史指标"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM metrics 
            WHERE server_id = ? AND collected_at >= datetime('now', ?)
            ORDER BY collected_at DESC
            """,
            (server_id, f'-{hours} hours')
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            r = dict(row)
            r['disk_data'] = json.loads(r['disk_data'])
            results.append(r)
        return results


# ============ 告警操作 ============

async def create_alert(
    server_id: int,
    alert_type: str,
    message: str,
    threshold: float,
    actual_value: float
) -> int:
    """创建告警"""
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """
            INSERT INTO alerts (server_id, alert_type, message, threshold, actual_value, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (server_id, alert_type, message, threshold, actual_value, now)
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_alerts() -> List[Dict[str, Any]]:
    """获取活动告警"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT a.*, s.name as server_name, s.host 
            FROM alerts a 
            JOIN servers s ON a.server_id = s.id 
            WHERE a.status = 'active' 
            ORDER BY a.created_at DESC
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def resolve_alert(alert_id: int) -> bool:
    """解决告警"""
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE id = ?",
            (now, alert_id)
        )
        await db.commit()
    return True


# ============ 阈值操作 ============

async def get_thresholds() -> List[Dict[str, Any]]:
    """获取所有阈值"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM thresholds")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_threshold(metric_type: str, warning: float, critical: float) -> bool:
    """更新阈值"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE thresholds SET warning_threshold = ?, critical_threshold = ? WHERE metric_type = ?",
            (warning, critical, metric_type)
        )
        await db.commit()
    return True


# ============ 批量命令操作 ============

async def save_batch_command(command: str, target_servers: List[int], results: Dict[str, Any]) -> int:
    """保存批量命令记录"""
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """
            INSERT INTO batch_commands (command, target_servers, results, executed_at)
            VALUES (?, ?, ?, ?)
            """,
            (command, json.dumps(target_servers), json.dumps(results), now)
        )
        await db.commit()
        return cursor.lastrowid


async def get_batch_commands(limit: int = 50) -> List[Dict[str, Any]]:
    """获取批量命令历史"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM batch_commands ORDER BY executed_at DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            r = dict(row)
            r['target_servers'] = json.loads(r['target_servers'])
            r['results'] = json.loads(r['results'])
            results.append(r)
        return results


# ============ 容器配置操作 ============

async def save_container_config(
    server_id: int,
    container_name: str,
    custom_commands: Optional[List[str]] = None,
    monitor_enabled: bool = True
) -> int:
    """保存容器配置"""
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """
            INSERT OR REPLACE INTO container_configs (server_id, container_name, custom_commands, monitor_enabled, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (server_id, container_name, json.dumps(custom_commands or []), int(monitor_enabled), now)
        )
        await db.commit()
        return cursor.lastrowid


async def get_container_configs(server_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """获取容器配置"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if server_id:
            cursor = await db.execute(
                "SELECT * FROM container_configs WHERE server_id = ?", (server_id,)
            )
        else:
            cursor = await db.execute("SELECT * FROM container_configs")
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            r = dict(row)
            r['custom_commands'] = json.loads(r['custom_commands'])
            results.append(r)
        return results
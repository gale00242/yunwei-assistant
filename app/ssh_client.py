"""
SSH 连接管理和命令执行
"""
import paramiko
from typing import Optional, Dict, Any, Tuple
from io import StringIO
from pathlib import Path
import asyncio
import threading
import logging
import time

# 配置日志
logger = logging.getLogger(__name__)

# 密钥目录（Docker 挂载）
KEYS_DIR = Path(__file__).parent.parent / "data" / "keys"


class SSHClient:
    """SSH 客户端管理器"""
    
    def __init__(self):
        self.connections: Dict[int, paramiko.SSHClient] = {}
        self._locks: Dict[int, threading.Lock] = {}
        self._global_lock = threading.Lock()
    
    def _get_lock(self, server_id: int) -> threading.Lock:
        """获取服务器级别的锁"""
        with self._global_lock:
            if server_id not in self._locks:
                self._locks[server_id] = threading.Lock()
            return self._locks[server_id]
    
    def _resolve_key_path(self, key_path: Optional[str]) -> Optional[str]:
        """
        解析密钥路径
        - 绝对路径直接使用
        - 相对路径相对于 data/keys 目录
        - None 则使用默认密钥
        """
        if not key_path:
            return None
        
        key = Path(key_path)
        if key.is_absolute():
            return str(key)
        else:
            # 相对路径，相对于 data/keys 目录
            return str(KEYS_DIR / key)
    
    def _load_key(self, key_content: Optional[str] = None, key_path: Optional[str] = None) -> paramiko.PKey:
        """
        加载 SSH 密钥
        优先使用 key_content（PEM 内容），其次 key_path（文件路径）
        """
        # 优先使用直接传入的密钥内容
        if key_content:
            key_content = key_content.strip()
            try:
                # 尝试 RSA
                return paramiko.RSAKey.from_private_key(StringIO(key_content))
            except:
                pass
            try:
                # 尝试 Ed25519
                return paramiko.Ed25519Key.from_private_key(StringIO(key_content))
            except:
                pass
            try:
                # 尝试 ECDSA
                return paramiko.ECDSAKey.from_private_key(StringIO(key_content))
            except:
                pass
            raise ValueError("无法解析密钥内容，请确保是有效的 PEM 格式私钥")
        
        # 从文件路径加载
        resolved_path = self._resolve_key_path(key_path)
        if resolved_path:
            try:
                return paramiko.RSAKey.from_private_key_file(resolved_path)
            except:
                pass
            try:
                return paramiko.Ed25519Key.from_private_key_file(resolved_path)
            except:
                pass
            try:
                return paramiko.ECDSAKey.from_private_key_file(resolved_path)
            except:
                pass
            raise ValueError(f"无法加载密钥文件: {resolved_path}")
        
        # 使用默认密钥
        default_paths = [
            KEYS_DIR / 'id_rsa',
            KEYS_DIR / 'id_ed25519',
            Path.home() / '.ssh' / 'id_rsa',
            Path.home() / '.ssh' / 'id_ed25519',
        ]
        
        for p in default_paths:
            if p.exists():
                try:
                    if 'rsa' in p.name:
                        return paramiko.RSAKey.from_private_key_file(str(p))
                    else:
                        return paramiko.Ed25519Key.from_private_key_file(str(p))
                except:
                    continue
        
        raise ValueError("No SSH key found. Place key in data/keys/ or paste PEM content.")
    
    def _create_client(
        self,
        host: str,
        port: int,
        username: str,
        auth_type: str,
        key_path: Optional[str] = None,
        key_content: Optional[str] = None,
        password: Optional[str] = None
    ) -> paramiko.SSHClient:
        """创建 SSH 连接"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        if auth_type == 'key':
            key = self._load_key(key_content=key_content, key_path=key_path)
            client.connect(host, port=port, username=username, pkey=key, timeout=10, banner_timeout=20)
        else:
            if not password:
                raise ValueError("Password required for password auth")
            client.connect(host, port=port, username=username, password=password, timeout=10, banner_timeout=20)
        
        return client
    
    def _is_connection_valid(self, client: paramiko.SSHClient) -> bool:
        """检查连接是否有效"""
        try:
            transport = client.get_transport()
            if not transport:
                return False
            if not transport.is_active():
                return False
            # 发送 keepalive 检查
            transport.send_ignore()
            return True
        except Exception as e:
            logger.debug(f"Connection check failed: {e}")
            return False
    
    def connect(self, server: Dict[str, Any], force_new: bool = False) -> paramiko.SSHClient:
        """连接服务器（带重试机制）"""
        server_id = server['id']
        lock = self._get_lock(server_id)
        
        with lock:
            # 检查现有连接
            if not force_new and server_id in self.connections:
                client = self.connections[server_id]
                if self._is_connection_valid(client):
                    return client
                else:
                    # 连接无效，清理
                    logger.info(f"Server {server_id}: 连接已失效，重新连接")
                    try:
                        client.close()
                    except:
                        pass
                    del self.connections[server_id]
            
            # 创建新连接（带重试）
            max_retries = 2
            last_error = None
            for attempt in range(max_retries):
                try:
                    client = self._create_client(
                        host=server['host'],
                        port=server['port'],
                        username=server['username'],
                        auth_type=server['auth_type'],
                        key_path=server.get('key_path'),
                        key_content=server.get('key_content'),
                        password=server.get('password')
                    )
                    self.connections[server_id] = client
                    logger.info(f"Server {server_id}: 连接成功")
                    return client
                except Exception as e:
                    last_error = e
                    logger.warning(f"Server {server_id}: 连接失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                    if attempt < max_retries - 1:
                        time.sleep(1)  # 等待后重试
            
            # 所有重试失败
            logger.error(f"Server {server_id}: 连接最终失败: {last_error}")
            raise last_error if last_error else Exception("连接失败")
    
    def disconnect(self, server_id: int):
        """断开连接"""
        lock = self._get_lock(server_id)
        with lock:
            if server_id in self.connections:
                try:
                    self.connections[server_id].close()
                    logger.info(f"Server {server_id}: 已断开连接")
                except:
                    pass
                del self.connections[server_id]
    
    def disconnect_all(self):
        """断开所有连接"""
        with self._global_lock:
            for server_id, client in list(self.connections.items()):
                try:
                    client.close()
                except:
                    pass
            self.connections.clear()
            self._locks.clear()
    
    def execute(self, server: Dict[str, Any], command: str, timeout: int = 30, retries: int = 2) -> Tuple[int, str, str]:
        """
        执行命令（带重试机制）
        
        Returns:
            (exit_code, stdout, stderr)
        """
        last_error = None
        
        for attempt in range(retries):
            try:
                client = self.connect(server, force_new=(attempt > 0))
                stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
                
                exit_code = stdout.channel.recv_exit_status()
                stdout_text = stdout.read().decode('utf-8', errors='replace')
                stderr_text = stderr.read().decode('utf-8', errors='replace')
                
                logger.debug(f"Server {server['id']}: 命令执行成功 - {command[:50]}...")
                return exit_code, stdout_text, stderr_text
                
            except Exception as e:
                last_error = e
                logger.warning(f"Server {server['id']}: 命令执行失败 (尝试 {attempt + 1}/{retries}): {e}")
                
                # 强制断开连接，下次重试会重新建立
                self.disconnect(server['id'])
                
                if attempt < retries - 1:
                    time.sleep(0.5)
        
        logger.error(f"Server {server['id']}: 命令执行最终失败: {last_error}")
        raise last_error if last_error else Exception("命令执行失败")
    
    async def execute_async(self, server: Dict[str, Any], command: str, timeout: int = 30) -> Tuple[int, str, str]:
        """异步执行命令"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.execute, server, command, timeout)


# 全局 SSH 客户端实例
ssh_client = SSHClient()
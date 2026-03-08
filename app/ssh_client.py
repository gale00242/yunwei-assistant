"""
SSH 连接管理和命令执行
"""
import paramiko
from typing import Optional, Dict, Any, Tuple
from io import StringIO
from pathlib import Path
import asyncio
from functools import wraps

# 密钥目录（Docker 挂载）
KEYS_DIR = Path(__file__).parent.parent / "data" / "keys"


class SSHClient:
    """SSH 客户端管理器"""
    
    def __init__(self):
        self.connections: Dict[int, paramiko.SSHClient] = {}
    
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
            client.connect(host, port=port, username=username, pkey=key, timeout=10)
        else:
            if not password:
                raise ValueError("Password required for password auth")
            client.connect(host, port=port, username=username, password=password, timeout=10)
        
        return client
    
    def connect(self, server: Dict[str, Any]) -> paramiko.SSHClient:
        """连接服务器"""
        server_id = server['id']
        
        # 检查现有连接
        if server_id in self.connections:
            client = self.connections[server_id]
            # 测试连接是否有效
            try:
                transport = client.get_transport()
                if transport and transport.is_active():
                    return client
            except:
                pass
        
        # 创建新连接
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
        return client
    
    def disconnect(self, server_id: int):
        """断开连接"""
        if server_id in self.connections:
            self.connections[server_id].close()
            del self.connections[server_id]
    
    def disconnect_all(self):
        """断开所有连接"""
        for client in self.connections.values():
            try:
                client.close()
            except:
                pass
        self.connections.clear()
    
    def execute(self, server: Dict[str, Any], command: str, timeout: int = 30) -> Tuple[int, str, str]:
        """
        执行命令
        
        Returns:
            (exit_code, stdout, stderr)
        """
        client = self.connect(server)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        
        exit_code = stdout.channel.recv_exit_status()
        stdout_text = stdout.read().decode('utf-8', errors='replace')
        stderr_text = stderr.read().decode('utf-8', errors='replace')
        
        return exit_code, stdout_text, stderr_text
    
    async def execute_async(self, server: Dict[str, Any], command: str, timeout: int = 30) -> Tuple[int, str, str]:
        """异步执行命令"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.execute, server, command, timeout)


# 全局 SSH 客户端实例
ssh_client = SSHClient()
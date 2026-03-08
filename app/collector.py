"""
系统指标采集器
"""
import re
from typing import Dict, Any, List, Tuple
from app.ssh_client import ssh_client


async def collect_cpu(server: Dict[str, Any]) -> float:
    """
    采集 CPU 使用率
    
    Returns:
        CPU 使用率百分比 (0-100)
    """
    # 使用 top 命令获取 CPU 使用率
    command = "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | cut -d'%' -f1"
    exit_code, stdout, stderr = await ssh_client.execute_async(server, command)
    
    if exit_code != 0 or not stdout.strip():
        # 备用方案：从 /proc/stat 计算
        command = "grep 'cpu ' /proc/stat | awk '{usage=($2+$4)*100/($2+$4+$5)} END {print usage}'"
        exit_code, stdout, stderr = await ssh_client.execute_async(server, command)
    
    try:
        return round(float(stdout.strip()), 2)
    except:
        return 0.0


async def collect_memory(server: Dict[str, Any]) -> Tuple[float, int, int]:
    """
    采集内存使用情况
    
    Returns:
        (使用率百分比, 已用MB, 总量MB)
    """
    command = "free -m | grep Mem"
    exit_code, stdout, stderr = await ssh_client.execute_async(server, command)
    
    if exit_code != 0:
        return 0.0, 0, 0
    
    # 格式: Mem: total used free shared buff/cache available
    parts = stdout.strip().split()
    if len(parts) >= 3:
        try:
            total = int(parts[1])
            used = int(parts[2])
            percent = round((used / total) * 100, 2) if total > 0 else 0.0
            return percent, used, total
        except:
            pass
    
    return 0.0, 0, 0


async def collect_disk(server: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    采集磁盘使用情况
    
    Returns:
        [{"mount": "/", "total": "100G", "used": "50G", "percent": 50}, ...]
    """
    command = "df -h | grep -E '^/dev'"
    exit_code, stdout, stderr = await ssh_client.execute_async(server, command)
    
    if exit_code != 0:
        return []
    
    disks = []
    for line in stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 6:
            try:
                percent_str = parts[4].replace('%', '')
                disks.append({
                    "device": parts[0],
                    "total": parts[1],
                    "used": parts[2],
                    "available": parts[3],
                    "percent": int(percent_str),
                    "mount": parts[5]
                })
            except:
                continue
    
    return disks


async def collect_all(server: Dict[str, Any]) -> Dict[str, Any]:
    """
    采集所有指标
    
    Returns:
        {
            "cpu": float,
            "memory_percent": float,
            "memory_used": int,
            "memory_total": int,
            "disks": [...]
        }
    """
    cpu = await collect_cpu(server)
    mem_percent, mem_used, mem_total = await collect_memory(server)
    disks = await collect_disk(server)
    
    return {
        "cpu": cpu,
        "memory_percent": mem_percent,
        "memory_used": mem_used,
        "memory_total": mem_total,
        "disks": disks
    }


async def collect_docker_containers(server: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    获取 Docker 容器列表
    
    Returns:
        [{"name": "xxx", "status": "running", "image": "xxx", "ports": "xxx"}, ...]
    """
    command = "docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}|{{.Ports}}'"
    exit_code, stdout, stderr = await ssh_client.execute_async(server, command)
    
    if exit_code != 0:
        # Docker 可能未安装或无权限
        return []
    
    containers = []
    for line in stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split('|')
        if len(parts) >= 3:
            status = parts[1].lower()
            containers.append({
                "name": parts[0],
                "status": "running" if "up" in status else "stopped",
                "status_detail": parts[1],
                "image": parts[2],
                "ports": parts[3] if len(parts) > 3 else ""
            })
    
    return containers


async def execute_container_command(
    server: Dict[str, Any],
    container_name: str,
    command: str
) -> Tuple[int, str, str]:
    """
    在容器内执行命令
    """
    docker_cmd = f"docker exec {container_name} {command}"
    return await ssh_client.execute_async(server, docker_cmd)


async def get_container_logs(
    server: Dict[str, Any],
    container_name: str,
    lines: int = 100
) -> str:
    """
    获取容器日志
    """
    command = f"docker logs --tail {lines} {container_name}"
    exit_code, stdout, stderr = await ssh_client.execute_async(server, command)
    return stdout + stderr
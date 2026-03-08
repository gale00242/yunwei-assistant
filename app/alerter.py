"""
告警判断和通知
"""
from typing import Dict, Any, List, Optional
from app.database import (
    get_thresholds, create_alert, get_active_alerts,
    resolve_alert, get_servers
)
from app.collector import collect_all


async def check_alerts(server: Dict[str, Any], metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    检查指标是否触发告警
    
    Returns:
        告警列表
    """
    thresholds = await get_thresholds()
    threshold_map = {t['metric_type']: t for t in thresholds}
    
    alerts = []
    
    # 检查 CPU
    cpu_threshold = threshold_map.get('cpu', {})
    if metrics['cpu'] >= cpu_threshold.get('critical_threshold', 90):
        alerts.append({
            "type": "cpu",
            "level": "critical",
            "message": f"CPU 使用率过高: {metrics['cpu']}%",
            "threshold": cpu_threshold.get('critical_threshold', 90),
            "actual": metrics['cpu']
        })
    elif metrics['cpu'] >= cpu_threshold.get('warning_threshold', 80):
        alerts.append({
            "type": "cpu",
            "level": "warning",
            "message": f"CPU 使用率警告: {metrics['cpu']}%",
            "threshold": cpu_threshold.get('warning_threshold', 80),
            "actual": metrics['cpu']
        })
    
    # 检查内存
    mem_threshold = threshold_map.get('memory', {})
    if metrics['memory_percent'] >= mem_threshold.get('critical_threshold', 90):
        alerts.append({
            "type": "memory",
            "level": "critical",
            "message": f"内存使用率过高: {metrics['memory_percent']}%",
            "threshold": mem_threshold.get('critical_threshold', 90),
            "actual": metrics['memory_percent']
        })
    elif metrics['memory_percent'] >= mem_threshold.get('warning_threshold', 80):
        alerts.append({
            "type": "memory",
            "level": "warning",
            "message": f"内存使用率警告: {metrics['memory_percent']}%",
            "threshold": mem_threshold.get('warning_threshold', 80),
            "actual": metrics['memory_percent']
        })
    
    # 检查磁盘
    disk_threshold = threshold_map.get('disk', {})
    for disk in metrics.get('disks', []):
        if disk['percent'] >= disk_threshold.get('critical_threshold', 95):
            alerts.append({
                "type": "disk",
                "level": "critical",
                "message": f"磁盘 {disk['mount']} 空间严重不足: {disk['percent']}% 已使用 (挂载点: {disk['mount']})",
                "threshold": disk_threshold.get('critical_threshold', 95),
                "actual": disk['percent'],
                "mount": disk['mount']
            })
        elif disk['percent'] >= disk_threshold.get('warning_threshold', 85):
            alerts.append({
                "type": "disk",
                "level": "warning",
                "message": f"磁盘 {disk['mount']} 空间警告: {disk['percent']}% 已使用 (挂载点: {disk['mount']})",
                "threshold": disk_threshold.get('warning_threshold', 85),
                "actual": disk['percent'],
                "mount": disk['mount']
            })
    
    return alerts


async def process_alerts(server: Dict[str, Any], metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    处理告警：创建记录、发送通知
    
    Returns:
        新产生的告警列表
    """
    alerts = await check_alerts(server, metrics)
    new_alerts = []
    
    for alert in alerts:
        # 创建告警记录
        alert_id = await create_alert(
            server_id=server['id'],
            alert_type=alert['type'],
            message=alert['message'],
            threshold=alert['threshold'],
            actual_value=alert['actual']
        )
        alert['id'] = alert_id
        alert['server_name'] = server['name']
        alert['server_host'] = server['host']
        new_alerts.append(alert)
    
    return new_alerts


def format_alert_message(alerts: List[Dict[str, Any]]) -> str:
    """
    格式化告警消息用于 Telegram
    """
    if not alerts:
        return ""
    
    lines = ["🚨 <b>服务器告警</b>\n"]
    
    for alert in alerts:
        level_emoji = "🔴" if alert.get('level') == 'critical' else "🟡"
        type_emoji = {
            'cpu': '💻',
            'memory': '🧠',
            'disk': '💾'
        }.get(alert['type'], '⚠️')
        
        lines.append(f"{level_emoji} {type_emoji} <b>{alert['server_name']}</b> ({alert['server_host']})")
        lines.append(f"   {alert['message']}")
        lines.append("")
    
    return '\n'.join(lines)
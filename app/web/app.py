"""
FastAPI Web 应用
"""
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime
from pathlib import Path

from app.database import init_db, get_servers, get_server, add_server, update_server, delete_server
from app.database import get_latest_metrics, get_thresholds, update_threshold
from app.database import get_active_alerts, resolve_alert
from app.database import get_batch_commands, save_batch_command
from app.database import save_container_config, get_container_configs
from app.collector import collect_all, collect_docker_containers, execute_container_command, get_container_logs
from app.alerter import process_alerts, format_alert_message
from app.notifier import init_notifier, get_notifier
from app.ssh_client import ssh_client

# 配置
BASE_DIR = Path(__file__).parent  # web 目录
APP_DIR = BASE_DIR.parent  # app 目录
PROJECT_DIR = APP_DIR.parent  # 项目根目录
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = PROJECT_DIR / "data"

# 全局变量
scheduler_task = None
collect_interval = 30 * 60  # 30 分钟


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 启动时
    await init_db()
    
    # 加载配置并初始化通知器
    config = await load_config()
    if config.get('telegram_bot_token') and config.get('telegram_chat_id'):
        init_notifier(config['telegram_bot_token'], config['telegram_chat_id'])
    
    # 启动定时采集任务
    global scheduler_task
    scheduler_task = asyncio.create_task(scheduled_collection())
    
    yield
    
    # 关闭时
    if scheduler_task:
        scheduler_task.cancel()
    ssh_client.disconnect_all()


app = FastAPI(title="运维助手", lifespan=lifespan)

# 静态文件和模板
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def load_config():
    """加载配置"""
    import yaml
    config_path = DATA_DIR / "config.yaml"
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}


async def save_config(config: dict):
    """保存配置"""
    import yaml
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config_path = DATA_DIR / "config.yaml"
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)


async def scheduled_collection():
    """定时采集任务"""
    while True:
        try:
            servers = await get_servers(enabled_only=True)
            for server in servers:
                try:
                    metrics = await collect_all(server)
                    from database import save_metrics
                    await save_metrics(
                        server_id=server['id'],
                        cpu_percent=metrics['cpu'],
                        memory_percent=metrics['memory_percent'],
                        memory_used=metrics['memory_used'],
                        memory_total=metrics['memory_total'],
                        disk_data=metrics['disks']
                    )
                    
                    # 检查告警
                    alerts = await process_alerts(server, metrics)
                    if alerts:
                        notifier = get_notifier()
                        if notifier:
                            message = format_alert_message(alerts)
                            await notifier.send_alert(message)
                    
                    # 断开连接，释放资源
                    ssh_client.disconnect(server['id'])
                    
                except Exception as e:
                    print(f"采集服务器 {server['name']} 失败: {e}")
            
        except Exception as e:
            print(f"定时采集任务异常: {e}")
        
        await asyncio.sleep(collect_interval)


# ============ 页面路由 ============

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """首页 - 仪表盘"""
    servers = await get_servers(enabled_only=False)
    alerts = await get_active_alerts()
    thresholds = await get_thresholds()
    
    # 获取每个服务器的最新指标
    server_metrics = {}
    for server in servers:
        metrics = await get_latest_metrics(server['id'])
        if metrics:
            server_metrics[server['id']] = metrics
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "servers": servers,
        "alerts": alerts,
        "thresholds": thresholds,
        "server_metrics": server_metrics
    })


@app.get("/servers", response_class=HTMLResponse)
async def servers_page(request: Request):
    """服务器管理页面"""
    servers = await get_servers(enabled_only=False)
    return templates.TemplateResponse("servers.html", {
        "request": request,
        "servers": servers
    })


@app.get("/servers/add", response_class=HTMLResponse)
async def add_server_page(request: Request):
    """添加服务器页面"""
    return templates.TemplateResponse("server_form.html", {
        "request": request,
        "server": None,
        "is_edit": False
    })


@app.post("/servers/add")
async def add_server_submit(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(22),
    auth_type: str = Form("key"),
    username: str = Form("root"),
    key_path: str = Form(None),
    key_content: str = Form(None),
    password: str = Form(None),
    enabled: bool = Form(True)
):
    """添加服务器"""
    # 获取表单数据
    form = await request.form()
    key_path = form.get("key_path") or None
    key_content = form.get("key_content") or None
    
    server_id = await add_server(
        name=name,
        host=host,
        port=port,
        auth_type=auth_type,
        username=username,
        key_path=key_path,
        key_content=key_content,
        password=password or None,
        enabled=enabled
    )
    return RedirectResponse(url=f"/servers/{server_id}", status_code=303)


@app.get("/servers/{server_id}", response_class=HTMLResponse)
async def server_detail(request: Request, server_id: int):
    """服务器详情页"""
    server = await get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="服务器不存在")
    
    metrics = await get_latest_metrics(server_id)
    containers = []
    try:
        containers = await collect_docker_containers(server)
    except:
        pass
    
    return templates.TemplateResponse("server_detail.html", {
        "request": request,
        "server": server,
        "metrics": metrics,
        "containers": containers
    })


@app.get("/servers/{server_id}/edit", response_class=HTMLResponse)
async def edit_server_page(request: Request, server_id: int):
    """编辑服务器页面"""
    server = await get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="服务器不存在")
    
    return templates.TemplateResponse("server_form.html", {
        "request": request,
        "server": server,
        "is_edit": True
    })


@app.post("/servers/{server_id}/edit")
async def edit_server_submit(
    request: Request,
    server_id: int,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(22),
    auth_type: str = Form("key"),
    username: str = Form("root"),
    key_path: str = Form(None),
    key_content: str = Form(None),
    password: str = Form(None),
    enabled: bool = Form(True)
):
    """编辑服务器"""
    form = await request.form()
    key_path = form.get("key_path") or None
    key_content = form.get("key_content") or None
    
    await update_server(
        server_id,
        name=name,
        host=host,
        port=port,
        auth_type=auth_type,
        username=username,
        key_path=key_path,
        key_content=key_content,
        password=password or None,
        enabled=enabled
    )
    return RedirectResponse(url=f"/servers/{server_id}", status_code=303)


@app.post("/servers/{server_id}/delete")
async def delete_server_submit(server_id: int):
    """删除服务器"""
    await delete_server(server_id)
    return RedirectResponse(url="/servers", status_code=303)


@app.get("/commands", response_class=HTMLResponse)
async def commands_page(request: Request):
    """批量命令页面"""
    servers = await get_servers(enabled_only=True)
    history = await get_batch_commands(limit=20)
    return templates.TemplateResponse("commands.html", {
        "request": request,
        "servers": servers,
        "history": history
    })


@app.post("/api/commands/execute")
async def execute_command(request: Request):
    """执行批量命令"""
    data = await request.json()
    command = data.get("command", "").strip()
    server_ids = data.get("server_ids", [])
    
    if not command or not server_ids:
        return JSONResponse({"error": "参数不完整"}, status_code=400)
    
    results = {}
    for sid in server_ids:
        server = await get_server(sid)
        if not server:
            results[f"server_{sid}"] = {"success": False, "error": "服务器不存在"}
            continue
        
        try:
            exit_code, stdout, stderr = await ssh_client.execute_async(server, command)
            results[server['name']] = {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr
            }
            ssh_client.disconnect(sid)
        except Exception as e:
            results[server['name']] = {"success": False, "error": str(e)}
    
    # 保存记录
    await save_batch_command(command, server_ids, results)
    
    return JSONResponse({"results": results})


@app.get("/containers", response_class=HTMLResponse)
async def containers_page(request: Request):
    """容器管理页面"""
    servers = await get_servers(enabled_only=True)
    server_containers = {}
    
    for server in servers:
        try:
            containers = await collect_docker_containers(server)
            server_containers[server['id']] = {
                "server": server,
                "containers": containers
            }
            ssh_client.disconnect(server['id'])
        except Exception as e:
            server_containers[server['id']] = {
                "server": server,
                "containers": [],
                "error": str(e)
            }
    
    return templates.TemplateResponse("containers.html", {
        "request": request,
        "server_containers": server_containers
    })


@app.post("/api/containers/{server_id}/{container_name}/command")
async def container_command(server_id: int, container_name: str, request: Request):
    """在容器内执行命令"""
    data = await request.json()
    command = data.get("command", "").strip()
    
    if not command:
        return JSONResponse({"error": "命令不能为空"}, status_code=400)
    
    server = await get_server(server_id)
    if not server:
        return JSONResponse({"error": "服务器不存在"}, status_code=404)
    
    try:
        exit_code, stdout, stderr = await execute_container_command(server, container_name, command)
        ssh_client.disconnect(server_id)
        return JSONResponse({
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/api/containers/{server_id}/{container_name}/logs")
async def container_logs(server_id: int, container_name: str, lines: int = 100):
    """获取容器日志"""
    server = await get_server(server_id)
    if not server:
        return JSONResponse({"error": "服务器不存在"}, status_code=404)
    
    try:
        logs = await get_container_logs(server, container_name, lines)
        ssh_client.disconnect(server_id)
        return JSONResponse({"logs": logs})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request):
    """告警管理页面"""
    alerts = await get_active_alerts()
    thresholds = await get_thresholds()
    return templates.TemplateResponse("alerts.html", {
        "request": request,
        "alerts": alerts,
        "thresholds": thresholds
    })


@app.post("/alerts/{alert_id}/resolve")
async def resolve_alert_submit(alert_id: int):
    """解决告警"""
    await resolve_alert(alert_id)
    return RedirectResponse(url="/alerts", status_code=303)


@app.post("/api/thresholds")
async def update_thresholds(request: Request):
    """更新告警阈值"""
    data = await request.json()
    for metric_type, values in data.items():
        await update_threshold(
            metric_type,
            values.get('warning', 80),
            values.get('critical', 90)
        )
    return JSONResponse({"success": True})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """设置页面"""
    config = await load_config()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "config": config
    })


@app.post("/settings")
async def settings_submit(request: Request):
    """保存设置"""
    data = await request.json()
    
    config = await load_config()
    config.update(data)
    await save_config(config)
    
    # 更新通知器
    if data.get('telegram_bot_token') and data.get('telegram_chat_id'):
        init_notifier(data['telegram_bot_token'], data['telegram_chat_id'])
    
    return JSONResponse({"success": True})


# ============ API 路由 ============

@app.get("/api/servers")
async def api_get_servers():
    """获取服务器列表 API"""
    servers = await get_servers(enabled_only=False)
    return JSONResponse(servers)


@app.get("/api/servers/{server_id}/metrics")
async def api_get_metrics(server_id: int):
    """获取服务器指标 API"""
    metrics = await get_latest_metrics(server_id)
    if not metrics:
        return JSONResponse({"error": "无数据"}, status_code=404)
    return JSONResponse(metrics)


@app.post("/api/collect")
async def api_collect_now():
    """立即采集一次"""
    servers = await get_servers(enabled_only=True)
    results = []
    
    for server in servers:
        try:
            metrics = await collect_all(server)
            from database import save_metrics
            await save_metrics(
                server_id=server['id'],
                cpu_percent=metrics['cpu'],
                memory_percent=metrics['memory_percent'],
                memory_used=metrics['memory_used'],
                memory_total=metrics['memory_total'],
                disk_data=metrics['disks']
            )
            
            alerts = await process_alerts(server, metrics)
            if alerts:
                notifier = get_notifier()
                if notifier:
                    message = format_alert_message(alerts)
                    await notifier.send_alert(message)
            
            ssh_client.disconnect(server['id'])
            results.append({"server": server['name'], "success": True, "metrics": metrics})
        except Exception as e:
            results.append({"server": server['name'], "success": False, "error": str(e)})
    
    return JSONResponse({"results": results})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
"""
FastAPI Web 应用
"""
from fastapi import FastAPI, Request, Form, HTTPException, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.database import init_db, get_servers, get_server, add_server, update_server, delete_server
from app.database import get_latest_metrics, get_thresholds, update_threshold
from app.database import get_active_alerts, resolve_alert
from app.database import get_batch_commands, save_batch_command, clear_batch_commands, get_batch_commands_count
from app.database import save_container_config, get_container_configs
from app.collector import collect_all, collect_docker_containers, execute_container_command, get_container_logs
from app.alerter import process_alerts, format_alert_message
from app.notifier import init_notifier, get_notifier
from app.ssh_client import ssh_client
from app.auth import verify_password, create_session, get_session, delete_session, is_valid_session

# 配置
BASE_DIR = Path(__file__).parent  # web 目录
APP_DIR = BASE_DIR.parent  # app 目录
PROJECT_DIR = APP_DIR.parent  # 项目根目录
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = PROJECT_DIR / "data"
KEYS_DIR = DATA_DIR / "keys"


def get_key_files() -> List[str]:
    """获取可用的密钥文件列表"""
    key_files = []
    if KEYS_DIR.exists():
        for f in KEYS_DIR.iterdir():
            if f.is_file() and not f.name.startswith('.'):
                key_files.append(f.name)
    return sorted(key_files)

# 全局变量
scheduler_task = None
collect_interval = None  # 将从配置文件加载


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 启动时
    await init_db()
    
    # 加载配置并初始化通知器
    global collect_interval
    config = await load_config()
    collect_interval = (config.get('collect_interval', 30) or 30) * 60  # 分钟转秒
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


# ============ 认证相关 ============

# 不需要认证的路径
PUBLIC_PATHS = ["/login", "/static", "/favicon.ico"]


async def get_current_user(request: Request, session_token: Optional[str] = Cookie(None)) -> Optional[str]:
    """获取当前登录用户"""
    if not session_token:
        return None
    session = get_session(session_token)
    if not session:
        return None
    return session.get("username")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """认证中间件"""
    path = request.url.path
    
    # 静态文件和登录页面不需要认证
    if path.startswith("/static") or path == "/login" or path == "/favicon.ico":
        return await call_next(request)
    
    # 检查 session
    session_token = request.cookies.get("session_token")
    if not session_token or not is_valid_session(session_token):
        # API 请求返回 401
        if path.startswith("/api"):
            return JSONResponse({"error": "未授权"}, status_code=401)
        # 页面请求重定向到登录
        return RedirectResponse(url="/login", status_code=302)
    
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    """登录页面"""
    # 已登录则跳转首页
    session_token = request.cookies.get("session_token")
    if session_token and is_valid_session(session_token):
        return RedirectResponse(url="/", status_code=302)
    
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error
    })


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    """处理登录"""
    if not verify_password(username, password):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "用户名或密码错误"
        })
    
    # 创建 session
    token = create_session(username)
    
    # 重定向到首页，设置 cookie
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        max_age=24 * 60 * 60,  # 24 小时
        samesite="lax"
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    """登出"""
    session_token = request.cookies.get("session_token")
    if session_token:
        delete_session(session_token)
    
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


# ============ 原有路由 ============


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
                    from app.database import save_metrics
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

@app.post("/collect")
async def collect_page(request: Request):
    """立即采集 - 表单提交版本，采集完成后重定向到首页"""
    # 检查认证
    session_token = request.cookies.get("session_token")
    if not session_token or not is_valid_session(session_token):
        return RedirectResponse(url="/login", status_code=302)
    
    servers = await get_servers(enabled_only=True)
    results = []
    
    # 找出未知状态的服务器
    unknown_servers = []
    for server in servers:
        metrics = await get_latest_metrics(server['id'])
        if not metrics:
            unknown_servers.append(server)
    
    servers_to_collect = unknown_servers if unknown_servers else servers
    
    success_count = 0
    fail_count = 0
    
    for server in servers_to_collect:
        try:
            collected = await collect_all(server)
            from app.database import save_metrics
            await save_metrics(
                server_id=server['id'],
                cpu_percent=collected['cpu'],
                memory_percent=collected['memory_percent'],
                memory_used=collected['memory_used'],
                memory_total=collected['memory_total'],
                disk_data=collected['disks']
            )
            ssh_client.disconnect(server['id'])
            results.append({"server": server['name'], "success": True, "metrics": collected})
            success_count += 1
        except Exception as e:
            results.append({"server": server['name'], "success": False, "error": str(e)})
            fail_count += 1
    
    # 重定向到首页，带结果参数
    from urllib.parse import urlencode
    params = {
        "collected": success_count,
        "failed": fail_count
    }
    return RedirectResponse(url=f"/?{urlencode(params)}", status_code=302)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, collected: int = 0, failed: int = 0):
    """首页 - 仪表盘"""
    servers = await get_servers(enabled_only=False)
    alerts = await get_active_alerts()
    thresholds = await get_thresholds()
    config = await load_config()
    
    # 获取每个服务器的最新指标
    server_metrics = {}
    for server in servers:
        metrics = await get_latest_metrics(server['id'])
        if metrics:
            server_metrics[server['id']] = metrics
    
    # 构建采集结果
    collect_result = None
    if collected > 0 or failed > 0:
        collect_result = {
            "message": f"采集完成：成功 {collected} 台，失败 {failed} 台",
            "success": failed == 0
        }
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "servers": servers,
        "alerts": alerts,
        "thresholds": thresholds,
        "server_metrics": server_metrics,
        "collect_interval": config.get('collect_interval', 30),
        "collect_result": collect_result
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
        "is_edit": False,
        "key_files": get_key_files()
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
    
    # 采集新服务器数据
    server = await get_server(server_id)
    if server:
        try:
            metrics = await collect_all(server)
            from app.database import save_metrics
            await save_metrics(
                server_id=server_id,
                cpu_percent=metrics['cpu'],
                memory_percent=metrics['memory_percent'],
                memory_used=metrics['memory_used'],
                memory_total=metrics['memory_total'],
                disk_data=metrics['disks']
            )
        except Exception as e:
            print(f"添加服务器后采集失败: {e}")
        finally:
            ssh_client.disconnect(server_id)
    
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
        "is_edit": True,
        "key_files": get_key_files()
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
    history_count = await get_batch_commands_count()
    return templates.TemplateResponse("commands.html", {
        "request": request,
        "servers": servers,
        "history": history,
        "history_count": history_count
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


@app.post("/api/commands/clear")
async def clear_commands_history():
    """清空命令历史"""
    count = await clear_batch_commands()
    return JSONResponse({"success": True, "deleted": count})


@app.get("/containers", response_class=HTMLResponse)
async def containers_page(request: Request):
    """容器管理页面"""
    servers = await get_servers(enabled_only=True)
    return templates.TemplateResponse("containers.html", {
        "request": request,
        "servers": servers
    })


@app.get("/api/servers/{server_id}/containers")
async def get_server_containers(server_id: int):
    """获取服务器容器列表 API"""
    server = await get_server(server_id)
    if not server:
        return JSONResponse({"error": "服务器不存在"}, status_code=404)
    
    try:
        containers = await collect_docker_containers(server)
        ssh_client.disconnect(server_id)
        return JSONResponse({"containers": containers})
    except Exception as e:
        return JSONResponse({"error": str(e)})


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


@app.post("/api/containers/{server_id}/{container_name}/restart")
async def restart_container_api(server_id: int, container_name: str):
    """重启容器"""
    server = await get_server(server_id)
    if not server:
        return JSONResponse({"error": "服务器不存在"}, status_code=404)
    
    try:
        from app.collector import restart_container, get_container_logs
        exit_code, stdout, stderr = await restart_container(server, container_name)
        ssh_client.disconnect(server_id)
        
        # 获取重启后的日志
        logs = await get_container_logs(server, container_name, 100)
        
        return JSONResponse({
            "success": exit_code == 0,
            "message": "容器已重启",
            "logs": logs
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/containers/{server_id}/{container_name}/stop")
async def stop_container_api(server_id: int, container_name: str):
    """停止容器"""
    server = await get_server(server_id)
    if not server:
        return JSONResponse({"error": "服务器不存在"}, status_code=404)
    
    try:
        from app.collector import stop_container
        exit_code, stdout, stderr = await stop_container(server, container_name)
        ssh_client.disconnect(server_id)
        return JSONResponse({
            "success": exit_code == 0,
            "message": stdout or stderr
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/containers/{server_id}/{container_name}/start")
async def start_container_api(server_id: int, container_name: str):
    """启动容器"""
    server = await get_server(server_id)
    if not server:
        return JSONResponse({"error": "服务器不存在"}, status_code=404)
    
    try:
        from app.collector import start_container
        exit_code, stdout, stderr = await start_container(server, container_name)
        ssh_client.disconnect(server_id)
        return JSONResponse({
            "success": exit_code == 0,
            "message": stdout or stderr
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


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
    """立即采集一次 - 优先采集未知状态的服务器，若无则采集全部"""
    servers = await get_servers(enabled_only=True)
    results = []
    
    # 找出未知状态的服务器（没有历史数据）
    unknown_servers = []
    known_servers = []
    
    for server in servers:
        metrics = await get_latest_metrics(server['id'])
        if metrics:
            known_servers.append(server)
        else:
            unknown_servers.append(server)
    
    # 决定采集哪些服务器
    servers_to_collect = unknown_servers if unknown_servers else servers
    
    for server in servers_to_collect:
        try:
            collected = await collect_all(server)
            from app.database import save_metrics
            await save_metrics(
                server_id=server['id'],
                cpu_percent=collected['cpu'],
                memory_percent=collected['memory_percent'],
                memory_used=collected['memory_used'],
                memory_total=collected['memory_total'],
                disk_data=collected['disks']
            )
            
            alerts = await process_alerts(server, collected)
            if alerts:
                notifier = get_notifier()
                if notifier:
                    message = format_alert_message(alerts)
                    await notifier.send_alert(message)
            
            ssh_client.disconnect(server['id'])
            results.append({"server": server['name'], "success": True, "metrics": collected})
        except Exception as e:
            results.append({"server": server['name'], "success": False, "error": str(e)})
    
    # 返回结果
    if unknown_servers:
        message = f"采集 {len(results)} 台未知状态服务器"
    else:
        message = f"所有服务器已有数据，重新采集全部 {len(results)} 台"
    
    return JSONResponse({
        "results": results,
        "message": message
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
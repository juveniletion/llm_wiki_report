"""
模拟RPA整改任务服务 - 用于AI大模型应用大赛
提供整改任务接收、查询、微信消息模拟功能
启动方式: python3 server/mock_rpa.py
迁移说明: 原先在 raw/rpa/ 下。raw/ 是**赛题数据**目录、不外传，
          但本文件是本项目**自研的模拟服务**、不是赛题数据——
          放在 raw/ 里会被一并排除，导致模块四在公开仓库里跑不起来。
访问地址: http://localhost:8090
API文档: http://localhost:8090/docs
"""
import json
import os
import time
import uuid
import threading
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import uvicorn

app = FastAPI(
    title="模拟RPA整改任务服务",
    description="重庆创灵境数字技术有限公司 - AI大模型应用大赛模拟RPA接口",
    version="1.0.0"
)

# 对外公开基址：用于拼 tracking_url（让调用方知道去哪儿查这个任务）。
# 本机直跑保持 8090；容器里 mock 的端口不映射出去，故由 compose 注入一个
# **从宿主机能打开**的地址（通常指向看板服务的转发接口）。
PUBLIC_BASE = os.getenv("RPA_PUBLIC_BASE", "http://localhost:8090").rstrip("/")

# 内存存储（模拟环境，不持久化）
tasks_db: dict = {}
notifications_db: list = []

# ==================== 数据模型 ====================

class Assignee(BaseModel):
    name: str
    department: str
    role: Optional[str] = None

class Source(BaseModel):
    analysis_type: str
    analysis_month: str
    product: str
    finding: str

class TaskCreateRequest(BaseModel):
    task_id: str
    task_title: str
    assignee: Assignee
    source: Source
    priority: str  # high/medium/low
    deadline: str  # YYYY-MM-DD
    suggestion: Optional[str] = None
    notify_method: Optional[str] = "wechat"
    created_at: str

class WechatNotifyRequest(BaseModel):
    recipient: str
    department: str
    message: str

# ==================== 状态自动推进 ====================

def auto_advance_status(task_id: str):
    """模拟任务状态自动推进"""
    time.sleep(30)  # 30秒后从 sent → received
    if task_id in tasks_db:
        tasks_db[task_id]["status"] = "received"
        tasks_db[task_id]["status_history"].append({
            "status": "received",
            "time": datetime.now().isoformat()
        })
    
    # 特殊后缀处理
    if "-FAST" in task_id:
        time.sleep(10)
        if task_id in tasks_db:
            tasks_db[task_id]["status"] = "confirmed"
            tasks_db[task_id]["status_history"].append({
                "status": "confirmed",
                "time": datetime.now().isoformat()
            })
    elif "-DONE" in task_id:
        time.sleep(10)
        if task_id in tasks_db:
            for s in ["confirmed", "in_progress", "completed"]:
                time.sleep(5)
                tasks_db[task_id]["status"] = s
                tasks_db[task_id]["status_history"].append({
                    "status": s,
                    "time": datetime.now().isoformat()
                })

# ==================== API接口 ====================

@app.get("/health")
def health_check():
    return {"status": "ok", "tasks_count": len(tasks_db), "timestamp": datetime.now().isoformat()}

@app.post("/api/rpa/tasks")
def create_task(req: TaskCreateRequest):
    """创建整改任务"""
    # 优先级校验
    if req.priority not in ("high", "medium", "low"):
        raise HTTPException(400, "priority必须为 high/medium/low")
    
    # 任务ID去重
    if req.task_id in tasks_db:
        raise HTTPException(400, f"任务ID {req.task_id} 已存在")
    
    now = datetime.now().isoformat()
    task = {
        "task_id": req.task_id,
        "task_title": req.task_title,
        "assignee": req.assignee.model_dump(),
        "source": req.source.model_dump(),
        "priority": req.priority,
        "deadline": req.deadline,
        "suggestion": req.suggestion,
        "notify_method": req.notify_method,
        "created_at": req.created_at,
        "status": "sent",
        "status_history": [
            {"status": "sent", "time": now}
        ],
        "progress": ""
    }
    
    tasks_db[req.task_id] = task
    
    # 发送微信通知
    notify_result = None
    if req.notify_method == "wechat":
        notify_result = {
            "wechat": f"已发送至 {req.assignee.name}({req.assignee.department})",
            "sent_at": now
        }
        notifications_db.append({
            "message_id": f"WX-{datetime.now().strftime('%Y%m%d')}-{len(notifications_db)+1:03d}",
            "recipient": req.assignee.name,
            "task_id": req.task_id,
            "sent_at": now
        })
    
    # 后台自动推进状态
    threading.Thread(target=auto_advance_status, args=(req.task_id,), daemon=True).start()
    
    return {
        "code": 200,
        "message": "任务创建成功，已分发至责任人",
        "data": {
            "task_id": req.task_id,
            "status": "sent",
            "notify_status": notify_result,
            # ⚠️ 不能写死 localhost:8090：容器里 mock 是独立服务，
            #    对外既非 localhost 也非 8090（端口不映射）。写死会让这个
            #    "查询地址"从容器外点不开——而且它**看起来**是个完整 URL，
            #    不容易怀疑。改用可配的公开基址（默认与旧行为一致）。
            "tracking_url": f"{PUBLIC_BASE}/api/rpa/tasks/{req.task_id}"
        }
    }

@app.get("/api/rpa/tasks/{task_id}")
def get_task(task_id: str):
    """查询任务状态"""
    if task_id not in tasks_db:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    task = tasks_db[task_id]
    return {
        "code": 200,
        "message": "查询成功",
        "data": task
    }

@app.get("/api/rpa/tasks")
def list_tasks(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    product: Optional[str] = None,
    month: Optional[str] = None,
    page: int = 1,
    page_size: int = 20
):
    """查询任务列表"""
    tasks = list(tasks_db.values())
    
    # 筛选
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if priority:
        tasks = [t for t in tasks if t["priority"] == priority]
    if product:
        tasks = [t for t in tasks if t["source"]["product"] == product]
    if month:
        tasks = [t for t in tasks if t["source"]["analysis_month"] == month]
    
    # 按创建时间倒序
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    
    total = len(tasks)
    start = (page - 1) * page_size
    end = start + page_size
    page_tasks = tasks[start:end]
    
    # 简化返回
    simple_tasks = [{
        "task_id": t["task_id"],
        "task_title": t["task_title"],
        "priority": t["priority"],
        "status": t["status"],
        "deadline": t["deadline"],
        "assignee": f"{t['assignee']['name']}({t['assignee']['department']})",
        "created_at": t["created_at"]
    } for t in page_tasks]
    
    return {
        "code": 200,
        "message": "查询成功",
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "tasks": simple_tasks
        }
    }

@app.post("/api/notify/wechat")
def send_wechat(req: WechatNotifyRequest):
    """模拟发送微信消息"""
    now = datetime.now().isoformat()
    msg_id = f"WX-{datetime.now().strftime('%Y%m%d')}-{len(notifications_db)+1:03d}"
    notifications_db.append({
        "message_id": msg_id,
        "recipient": req.recipient,
        "department": req.department,
        "message": req.message,
        "sent_at": now
    })
    return {
        "code": 200,
        "message": "微信消息发送成功",
        "data": {
            "recipient": req.recipient,
            "message_id": msg_id,
            "sent_at": now,
            "status": "delivered"
        }
    }

@app.get("/api/stats")
def get_stats():
    """获取统计信息"""
    total = len(tasks_db)
    by_status = {}
    by_priority = {}
    for t in tasks_db.values():
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        by_priority[t["priority"]] = by_priority.get(t["priority"], 0) + 1
    
    return {
        "code": 200,
        "data": {
            "total_tasks": total,
            "total_notifications": len(notifications_db),
            "by_status": by_status,
            "by_priority": by_priority
        }
    }

@app.delete("/api/admin/reset")
def reset_all():
    """重置所有数据（仅测试用）"""
    tasks_db.clear()
    notifications_db.clear()
    return {"code": 200, "message": "所有数据已重置"}


# ==================== 启动 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("  模拟RPA整改任务服务")
    print("  重庆创灵境数字技术有限公司")
    print("  AI大模型应用大赛专用")
    print("=" * 60)
    print(f"  启动地址: http://localhost:8090")
    print(f"  API文档:  http://localhost:8090/docs")
    print(f"  健康检查: http://localhost:8090/health")
    print(f"  任务列表: http://localhost:8090/api/rpa/tasks")
    print(f"  统计信息: http://localhost:8090/api/stats")
    print("=" * 60)
    # 主机/端口可配：本机直跑用默认 8090；容器里由 compose 指定。
    import os as _os
    uvicorn.run(app,
                host=_os.getenv("RPA_HOST", "0.0.0.0"),
                port=int(_os.getenv("RPA_PORT", "8090")),
                log_level="info")

"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import cold_integrity, resource

# 固定路径的专用路由必须排在通用 /api/{module} 之前注册。
ROUTERS = [cold_integrity, resource]

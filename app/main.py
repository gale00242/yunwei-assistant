"""
运维助手 - 主入口
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from app.web.app import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
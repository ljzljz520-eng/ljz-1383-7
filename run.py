#!/usr/bin/env python3
"""启动：python3 run.py [port]  （管理后台密钥环境变量 ADMIN_KEY，默认 admin-dev-key）"""
import os, sys
from podcast.db import connect, init, DB_PATH
from podcast.web import make_server

if not os.path.exists(DB_PATH):
    import seed
    db = connect(); init(db); seed.seed(db)

db = connect()
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
print(f"公开站点  http://127.0.0.1:{port}/")
print(f"管理后台  http://127.0.0.1:{port}/admin?key={os.environ.get('ADMIN_KEY','admin-dev-key')}")
make_server(db, port).serve_forever()

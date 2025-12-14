from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx
import hashlib
import os

# ============ 配置 ============
NEW_API_URL = os.getenv("NEW_API_URL", "https://velvenode.zeabur.app")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./coupon.db")
CLAIM_COOLDOWN_HOURS = int(os.getenv("CLAIM_COOLDOWN_HOURS", "8"))
SITE_NAME = os.getenv("SITE_NAME", "我的公益站")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")  # 管理员密码，用于添加兑换码

# ============ 数据库 ============
Base = declarative_base()

class CouponPool(Base):
    """兑换码池 - 预先导入的兑换码"""
    __tablename__ = "coupon_pool"
    id = Column(Integer, primary_key=True, autoincrement=True)
    coupon_code = Column(String(64), unique=True, nullable=False)
    quota = Column(Integer, default=500000)
    is_claimed = Column(Boolean, default=False)
    claimed_by = Column(String(64), nullable=True)  # 领取者的 key hash
    claimed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class ClaimRecord(Base):
    """领取记录"""
    __tablename__ = "claim_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_key_hash = Column(String(64), index=True, nullable=False)
    coupon_code = Column(String(64), nullable=False)
    claim_time = Column(DateTime, default=lambda: datetime.now(timezone.utc))

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ============ FastAPI App ============
app = FastAPI(title="兑换券系统")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ 工具函数 ============
def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:32]

def now_utc():
    return datetime.now(timezone.utc)

async def verify_api_key(api_key: str) -> bool:
    if not api_key or not api_key.startswith("sk-"):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{NEW_API_URL}/v1/models",
                headers={"Authorization": f"Bearer {api_key}"}
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("success", False) or "data" in data
    except Exception as e:
        print(f"API Key verify error: {e}")
    return False

# ============ 用户 API ============
@app.post("/api/verify")
async def verify_user(request: Request):
    body = await request.json()
    api_key = body.get("api_key", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请输入 API Key")
    is_valid = await verify_api_key(api_key)
    if not is_valid:
        raise HTTPException(status_code=401, detail="API Key 无效或已过期")
    key_hash = hash_api_key(api_key)
    return {
        "success": True,
        "data": {
            "key_hash": key_hash,
            "key_preview": api_key[:10] + "****" + api_key[-4:]
        }
    }

@app.post("/api/claim/status")
async def get_claim_status(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    api_key = body.get("api_key", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请输入 API Key")
    is_valid = await verify_api_key(api_key)
    if not is_valid:
        raise HTTPException(status_code=401, detail="API Key 无效或已过期")
    
    key_hash = hash_api_key(api_key)
    now = now_utc()
    
    # 查询最近领取
    last_claim = db.query(ClaimRecord).filter(
        ClaimRecord.user_key_hash == key_hash
    ).order_by(ClaimRecord.claim_time.desc()).first()
    
    can_claim = True
    cooldown_text = None
    
    if last_claim:
        # 确保时区一致
        last_time = last_claim.claim_time
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        next_claim_time = last_time + timedelta(hours=CLAIM_COOLDOWN_HOURS)
        if now < next_claim_time:
            can_claim = False
            remaining = next_claim_time - now
            total_seconds = int(remaining.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60
            cooldown_text = f"{hours}小时 {minutes}分钟 {seconds}秒"
    
    # 检查是否还有可用兑换码
    available_count = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    if available_count == 0:
        can_claim = False
        cooldown_text = "兑换码已领完，请等待管理员补充"
    
    # 历史记录
    history = db.query(ClaimRecord).filter(
        ClaimRecord.user_key_hash == key_hash
    ).order_by(ClaimRecord.claim_time.desc()).limit(10).all()
    
    return {
        "success": True,
        "data": {
            "can_claim": can_claim,
            "cooldown_text": cooldown_text,
            "available_count": available_count,
            "history": [
                {
                    "coupon_code": r.coupon_code,
                    "claim_time": r.claim_time.isoformat() if r.claim_time else "",
                }
                for r in history
            ]
        }
    }

@app.post("/api/claim")
async def claim_coupon(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    api_key = body.get("api_key", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请输入 API Key")
    is_valid = await verify_api_key(api_key)
    if not is_valid:
        raise HTTPException(status_code=401, detail="API Key 无效或已过期")
    
    key_hash = hash_api_key(api_key)
    now = now_utc()
    
    # 检查冷却
    last_claim = db.query(ClaimRecord).filter(
        ClaimRecord.user_key_hash == key_hash
    ).order_by(ClaimRecord.claim_time.desc()).first()
    
    if last_claim:
        last_time = last_claim.claim_time
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        next_claim_time = last_time + timedelta(hours=CLAIM_COOLDOWN_HOURS)
        if now < next_claim_time:
            remaining = next_claim_time - now
            total_seconds = int(remaining.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            raise HTTPException(status_code=400, detail=f"冷却中，请在 {hours}小时 {minutes}分钟 后再试")
    
    # 从池中获取一个未领取的兑换码
    coupon = db.query(CouponPool).filter(
        CouponPool.is_claimed == False
    ).first()
    
    if not coupon:
        raise HTTPException(status_code=400, detail="兑换码已领完，请等待管理员补充")
    
    # 标记为已领取
    coupon.is_claimed = True
    coupon.claimed_by = key_hash
    coupon.claimed_at = now
    
    # 记录领取
    record = ClaimRecord(
        user_key_hash=key_hash,
        coupon_code=coupon.coupon_code,
        claim_time=now
    )
    db.add(record)
    db.commit()
    
    return {
        "success": True,
        "data": {"coupon_code": coupon.coupon_code}
    }

# ============ 管理员 API ============
@app.post("/api/admin/add-coupons")
async def add_coupons(request: Request, db: Session = Depends(get_db)):
    """管理员添加兑换码到池中"""
    body = await request.json()
    password = body.get("password", "")
    coupons = body.get("coupons", [])  # 兑换码列表
    
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="管理员密码错误")
    
    if not coupons:
        raise HTTPException(status_code=400, detail="请提供兑换码列表")
    
    added = 0
    for code in coupons:
        code = code.strip()
        if not code:
            continue
        # 检查是否已存在
        exists = db.query(CouponPool).filter(CouponPool.coupon_code == code).first()
        if not exists:
            db.add(CouponPool(coupon_code=code))
            added += 1
    
    db.commit()
    
    total = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    
    return {
        "success": True,
        "message": f"成功添加 {added} 个兑换码，当前可用: {total} 个"
    }

@app.get("/api/admin/stats")
async def get_stats(password: str, db: Session = Depends(get_db)):
    """获取统计信息"""
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="管理员密码错误")
    
    total = db.query(CouponPool).count()
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    claimed = db.query(CouponPool).filter(CouponPool.is_claimed == True).count()
    
    return {
        "success": True,
        "data": {
            "total": total,
            "available": available,
            "claimed": claimed
        }
    }

# ============ 页面 ============
@app.get("/", response_class=HTMLResponse)
async def index(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return get_user_page(available)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return get_admin_page()

def get_user_page(available_count):
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>兑换券领取中心 - {SITE_NAME}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .gradient-header {{ background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); }}
        .card {{ background: white; border-radius: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}
        .btn-primary {{ background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); }}
        .btn-claim {{ background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%); }}
        .btn-claim:disabled {{ background: #9ca3af; cursor: not-allowed; }}
        .coupon-card {{ background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%); }}
        .loading {{ display: inline-block; width: 18px; height: 18px; border: 2px solid #fff; border-radius: 50%; border-top-color: transparent; animation: spin 1s linear infinite; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        .toast {{ position: fixed; top: 20px; left: 50%; transform: translateX(-50%); padding: 12px 24px; border-radius: 8px; color: white; font-weight: 500; z-index: 1000; }}
    </style>
</head>
<body class="bg-gray-100 min-h-screen">
    <nav class="gradient-header text-white py-4 px-6 shadow-lg">
        <div class="container mx-auto flex justify-between items-center">
            <div class="flex items-center space-x-2">
                <span class="text-yellow-300 text-2xl">★</span>
                <span class="font-bold text-xl">{SITE_NAME}</span>
            </div>
            <a href="{NEW_API_URL}" target="_blank" class="hover:text-gray-200">返回主站</a>
        </div>
    </nav>

    <main class="container mx-auto px-4 py-8 max-w-2xl">
        <div id="login-section" class="card p-8">
            <div class="text-center mb-6">
                <div class="text-5xl mb-4">🎫</div>
                <h1 class="text-2xl font-bold text-gray-800">兑换券领取中心</h1>
                <p class="text-gray-500 mt-2">请输入您的 API Key 验证身份</p>
                <p class="text-sm text-green-600 mt-2">📦 当前可领取: <span id="available-count">{available_count}</span> 个</p>
            </div>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-2">API Key</label>
                    <input type="password" id="api-key-input" 
                           class="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
                           placeholder="sk-xxxxxxxxxxxxxxxx">
                    <p class="text-xs text-gray-400 mt-2">
                        💡 在 <a href="{NEW_API_URL}/console/token" target="_blank" class="text-blue-500 hover:underline">主站控制台 → 令牌管理</a> 中创建
                    </p>
                </div>
                <button onclick="verifyKey()" id="verify-btn"
                        class="w-full btn-primary text-white py-3 rounded-lg font-semibold hover:opacity-90 transition">
                    验证并登录
                </button>
            </div>
        </div>

        <div id="claim-section" class="hidden">
            <div class="card p-6 mb-6">
                <div class="flex items-center justify-between">
                    <div>
                        <p class="text-gray-500 text-sm">当前 API Key</p>
                        <p id="key-preview" class="font-mono text-gray-800"></p>
                    </div>
                    <button onclick="logout()" class="text-gray-400 hover:text-gray-600 text-sm">切换账号</button>
                </div>
            </div>

            <div class="card p-6 mb-6">
                <div class="flex items-center justify-between mb-6">
                    <h2 class="text-lg font-semibold text-gray-800">领取状态</h2>
                    <span id="status-badge" class="px-3 py-1 rounded-full text-sm font-medium"></span>
                </div>
                <div class="text-center py-6">
                    <button id="claim-btn" onclick="claimCoupon()" 
                            class="btn-claim text-white py-4 px-10 rounded-xl text-lg font-bold shadow-lg hover:opacity-90 transition">
                        ⬇️ 领取兑换券
                    </button>
                    <p id="cooldown-msg" class="text-gray-500 mt-4"></p>
                    <p class="text-gray-400 text-sm mt-2">每 {CLAIM_COOLDOWN_HOURS} 小时可领取一次</p>
                </div>
            </div>

            <div class="card p-6">
                <h2 class="text-lg font-semibold text-gray-800 mb-4">📋 领取记录</h2>
                <div id="history-container"></div>
            </div>

            <div class="card p-6 mt-6">
                <h2 class="text-lg font-semibold text-gray-800 mb-4">📖 使用说明</h2>
                <ol class="list-decimal list-inside space-y-2 text-gray-600 text-sm">
                    <li>点击"领取兑换券"获取兑换码</li>
                    <li>复制兑换码</li>
                    <li>前往 <a href="{NEW_API_URL}/topup" target="_blank" class="text-blue-500 hover:underline">主站钱包管理</a></li>
                    <li>在"兑换码充值"处粘贴并兑换</li>
                </ol>
            </div>
        </div>
    </main>

    <footer class="text-center py-6 text-gray-400 text-sm">{SITE_NAME} © 2025</footer>

    <script>
        let apiKey = localStorage.getItem('coupon_api_key') || '';
        let keyPreview = '';

        document.addEventListener('DOMContentLoaded', () => {{
            if (apiKey) {{
                document.getElementById('api-key-input').value = apiKey;
                verifyKey();
            }}
        }});

        function showToast(msg, ok = true) {{
            const t = document.createElement('div');
            t.className = `toast ${{ok ? 'bg-green-500' : 'bg-red-500'}}`;
            t.textContent = msg;
            document.body.appendChild(t);
            setTimeout(() => t.remove(), 3000);
        }}

        async function verifyKey() {{
            const input = document.getElementById('api-key-input');
            const btn = document.getElementById('verify-btn');
            apiKey = input.value.trim();
            if (!apiKey) {{ showToast('请输入 API Key', false); return; }}
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> 验证中...';
            try {{
                const resp = await fetch('/api/verify', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{api_key: apiKey}})
                }});
                const data = await resp.json();
                if (resp.ok && data.success) {{
                    keyPreview = data.data.key_preview;
                    localStorage.setItem('coupon_api_key', apiKey);
                    showLoggedIn();
                    await loadStatus();
                }} else {{
                    showToast(data.detail || 'API Key 无效', false);
                }}
            }} catch (e) {{ showToast('网络错误', false); }}
            btn.disabled = false;
            btn.textContent = '验证并登录';
        }}

        function showLoggedIn() {{
            document.getElementById('login-section').classList.add('hidden');
            document.getElementById('claim-section').classList.remove('hidden');
            document.getElementById('key-preview').textContent = keyPreview;
        }}

        function logout() {{
            localStorage.removeItem('coupon_api_key');
            apiKey = '';
            document.getElementById('api-key-input').value = '';
            document.getElementById('login-section').classList.remove('hidden');
            document.getElementById('claim-section').classList.add('hidden');
        }}

        async function loadStatus() {{
            try {{
                const resp = await fetch('/api/claim/status', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{api_key: apiKey}})
                }});
                const data = await resp.json();
                if (data.success) updateUI(data.data);
            }} catch (e) {{ console.error(e); }}
        }}

        function updateUI(data) {{
            const btn = document.getElementById('claim-btn');
            const badge = document.getElementById('status-badge');
            const msg = document.getElementById('cooldown-msg');
            document.getElementById('available-count').textContent = data.available_count;
            if (data.can_claim) {{
                btn.disabled = false;
                badge.textContent = '✅ 可领取';
                badge.className = 'px-3 py-1 rounded-full text-sm font-medium bg-green-100 text-green-600';
                msg.textContent = '';
            }} else {{
                btn.disabled = true;
                badge.textContent = '⏳ 冷却中';
                badge.className = 'px-3 py-1 rounded-full text-sm font-medium bg-yellow-100 text-yellow-600';
                msg.textContent = data.cooldown_text || '';
            }}
            renderHistory(data.history || []);
        }}

        function renderHistory(records) {{
            const c = document.getElementById('history-container');
            if (!records.length) {{ c.innerHTML = '<p class="text-gray-400 text-center py-4">暂无记录</p>'; return; }}
            c.innerHTML = records.map(r => `
                <div class="coupon-card text-white p-4 rounded-xl mb-3">
                    <div class="flex justify-between items-center">
                        <div class="font-mono">${{r.coupon_code}}</div>
                        <button onclick="copyCode('${{r.coupon_code}}')" class="bg-white/20 hover:bg-white/30 px-3 py-1 rounded text-sm">📋 复制</button>
                    </div>
                    <div class="text-blue-200 text-xs mt-2">领取: ${{new Date(r.claim_time).toLocaleString('zh-CN')}}</div>
                </div>
            `).join('');
        }}

        async function claimCoupon() {{
            const btn = document.getElementById('claim-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> 领取中...';
            try {{
                const resp = await fetch('/api/claim', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{api_key: apiKey}})
                }});
                const data = await resp.json();
                if (resp.ok && data.success) {{
                    showToast('领取成功！已复制到剪贴板');
                    await navigator.clipboard.writeText(data.data.coupon_code);
                }} else {{
                    showToast(data.detail || '领取失败', false);
                }}
            }} catch (e) {{ showToast('网络错误', false); }}
            btn.innerHTML = '⬇️ 领取兑换券';
            await loadStatus();
        }}

        async function copyCode(code) {{
            await navigator.clipboard.writeText(code);
            showToast('已复制');
        }}
    </script>
</body>
</html>'''

def get_admin_page():
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台 - {SITE_NAME}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-8">
    <div class="max-w-2xl mx-auto">
        <h1 class="text-2xl font-bold mb-6">🔧 兑换码管理</h1>
        
        <div class="bg-white rounded-lg p-6 shadow mb-6">
            <h2 class="font-semibold mb-4">添加兑换码</h2>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm mb-1">管理员密码</label>
                    <input type="password" id="admin-pwd" class="w-full border rounded px-3 py-2" placeholder="输入管理员密码">
                </div>
                <div>
                    <label class="block text-sm mb-1">兑换码（每行一个）</label>
                    <textarea id="coupons-input" rows="10" class="w-full border rounded px-3 py-2 font-mono text-sm" 
                              placeholder="粘贴从 New API 复制的兑换码，每行一个"></textarea>
                </div>
                <button onclick="addCoupons()" class="bg-blue-500 text-white px-6 py-2 rounded hover:bg-blue-600">
                    添加兑换码
                </button>
            </div>
        </div>

        <div class="bg-white rounded-lg p-6 shadow">
            <h2 class="font-semibold mb-4">统计信息</h2>
            <div id="stats">点击下方按钮加载</div>
            <button onclick="loadStats()" class="mt-4 bg-gray-500 text-white px-4 py-2 rounded hover:bg-gray-600">
                刷新统计
            </button>
        </div>

        <p class="text-center text-gray-400 mt-6">
            <a href="/" class="hover:text-gray-600">← 返回领取页面</a>
        </p>
    </div>

    <script>
        async function addCoupons() {{
            const pwd = document.getElementById('admin-pwd').value;
            const text = document.getElementById('coupons-input').value;
            const coupons = text.split('\\n').map(s => s.trim()).filter(s => s);
            
            if (!pwd) {{ alert('请输入管理员密码'); return; }}
            if (!coupons.length) {{ alert('请输入兑换码'); return; }}
            
            try {{
                const resp = await fetch('/api/admin/add-coupons', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{password: pwd, coupons: coupons}})
                }});
                const data = await resp.json();
                alert(data.message || data.detail);
                if (data.success) {{
                    document.getElementById('coupons-input').value = '';
                    loadStats();
                }}
            }} catch (e) {{
                alert('网络错误');
            }}
        }}

        async function loadStats() {{
            const pwd = document.getElementById('admin-pwd').value;
            if (!pwd) {{ alert('请先输入管理员密码'); return; }}
            try {{
                const resp = await fetch(`/api/admin/stats?password=${{encodeURIComponent(pwd)}}`);
                const data = await resp.json();
                if (data.success) {{
                    document.getElementById('stats').innerHTML = `
                        <div class="grid grid-cols-3 gap-4 text-center">
                            <div class="bg-gray-50 p-4 rounded">
                                <div class="text-2xl font-bold">${{data.data.total}}</div>
                                <div class="text-gray-500 text-sm">总数</div>
                            </div>
                            <div class="bg-green-50 p-4 rounded">
                                <div class="text-2xl font-bold text-green-600">${{data.data.available}}</div>
                                <div class="text-gray-500 text-sm">可用</div

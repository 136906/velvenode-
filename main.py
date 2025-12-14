from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime, timedelta
from typing import Optional
import httpx
import os

# ============ 配置 ============
NEW_API_URL = os.getenv("NEW_API_URL", "https://velvenode.zeabur.app")
NEW_API_ADMIN_TOKEN = os.getenv("NEW_API_ADMIN_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./coupon.db")
CLAIM_COOLDOWN_HOURS = int(os.getenv("CLAIM_COOLDOWN_HOURS", "8"))
COUPON_QUOTA = int(os.getenv("COUPON_QUOTA", "500000"))
COUPON_NAME_PREFIX = os.getenv("COUPON_NAME_PREFIX", "公益券")
SITE_NAME = os.getenv("SITE_NAME", "我的公益站")

# ============ 数据库 ============
Base = declarative_base()

class ClaimRecord(Base):
    __tablename__ = "claim_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, index=True, nullable=False)
    username = Column(String(255), nullable=False)
    coupon_code = Column(String(64), unique=True, nullable=False)
    quota = Column(Integer, default=500000)
    claim_time = Column(DateTime, default=datetime.utcnow)
    expire_time = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False)

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

# ============ 验证 API Key ============
async def verify_api_key(api_key: str) -> Optional[dict]:
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{NEW_API_URL}/api/user/self",
                headers={"Authorization": f"Bearer {api_key}"}
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return data.get("data")
    except Exception as e:
        print(f"API Key verify error: {e}")
    return None

# ============ 创建兑换码 ============
async def create_redemption_code(name: str, quota: int) -> Optional[str]:
    if not NEW_API_ADMIN_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{NEW_API_URL}/api/redemption/",
                headers={
                    "Authorization": f"Bearer {NEW_API_ADMIN_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={"name": name, "quota": quota, "count": 1}
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data"):
                    codes = data.get("data", [])
                    if codes:
                        return codes[0] if isinstance(codes[0], str) else codes[0].get("key")
    except Exception as e:
        print(f"Create redemption error: {e}")
    return None

# ============ API ============
@app.post("/api/verify")
async def verify_user(request: Request):
    body = await request.json()
    api_key = body.get("api_key", "")
    user = await verify_api_key(api_key)
    if not user:
        raise HTTPException(status_code=401, detail="API Key 无效")
    return {"success": True, "data": user}

@app.post("/api/claim/status")
async def get_claim_status(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    api_key = body.get("api_key", "")
    user = await verify_api_key(api_key)
    if not user:
        raise HTTPException(status_code=401, detail="API Key 无效")
    
    user_id = user.get("id")
    now = datetime.utcnow()
    
    last_claim = db.query(ClaimRecord).filter(
        ClaimRecord.user_id == user_id
    ).order_by(ClaimRecord.claim_time.desc()).first()
    
    can_claim = True
    cooldown_text = None
    
    if last_claim:
        next_claim_time = last_claim.claim_time + timedelta(hours=CLAIM_COOLDOWN_HOURS)
        if now < next_claim_time:
            can_claim = False
            remaining = next_claim_time - now
            total_seconds = int(remaining.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60
            cooldown_text = f"{hours}小时 {minutes}分钟 {seconds}秒"
    
    history = db.query(ClaimRecord).filter(
        ClaimRecord.user_id == user_id
    ).order_by(ClaimRecord.claim_time.desc()).limit(10).all()
    
    return {
        "success": True,
        "data": {
            "can_claim": can_claim,
            "cooldown_text": cooldown_text,
            "history": [
                {
                    "coupon_code": r.coupon_code,
                    "claim_time": r.claim_time.isoformat() + "Z",
                    "is_expired": now > r.expire_time,
                }
                for r in history
            ]
        }
    }

@app.post("/api/claim")
async def claim_coupon(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    api_key = body.get("api_key", "")
    user = await verify_api_key(api_key)
    if not user:
        raise HTTPException(status_code=401, detail="API Key 无效")
    
    user_id = user.get("id")
    username = user.get("username", "unknown")
    now = datetime.utcnow()
    
    last_claim = db.query(ClaimRecord).filter(
        ClaimRecord.user_id == user_id
    ).order_by(ClaimRecord.claim_time.desc()).first()
    
    if last_claim:
        next_claim_time = last_claim.claim_time + timedelta(hours=CLAIM_COOLDOWN_HOURS)
        if now < next_claim_time:
            remaining = next_claim_time - now
            total_seconds = int(remaining.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            raise HTTPException(status_code=400, detail=f"冷却中，请在 {hours}小时 {minutes}分钟 后再试")
    
    code_name = f"{COUPON_NAME_PREFIX}-{user_id}-{now.strftime('%Y%m%d%H%M%S')}"
    coupon_code = await create_redemption_code(code_name, COUPON_QUOTA)
    
    if not coupon_code:
        raise HTTPException(status_code=500, detail="创建兑换码失败，请联系管理员检查 NEW_API_ADMIN_TOKEN 配置")
    
    expire_time = now + timedelta(hours=24)
    record = ClaimRecord(
        user_id=user_id,
        username=username,
        coupon_code=coupon_code,
        quota=COUPON_QUOTA,
        claim_time=now,
        expire_time=expire_time
    )
    db.add(record)
    db.commit()
    
    return {
        "success": True,
        "data": {"coupon_code": coupon_code}
    }

# ============ 页面 ============
@app.get("/", response_class=HTMLResponse)
async def index():
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
        .expired-stamp {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%) rotate(-20deg); font-size: 1.2rem; font-weight: bold; color: rgba(239, 68, 68, 0.9); border: 3px solid; padding: 0.25rem 1rem; border-radius: 8px; background: rgba(255,255,255,0.95); }}
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
        <!-- 登录区域 -->
        <div id="login-section" class="card p-8">
            <div class="text-center mb-6">
                <div class="text-5xl mb-4">🎫</div>
                <h1 class="text-2xl font-bold text-gray-800">兑换券领取中心</h1>
                <p class="text-gray-500 mt-2">请输入您的 API Key 验证身份</p>
            </div>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-2">API Key</label>
                    <input type="password" id="api-key-input" 
                           class="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                           placeholder="sk-xxxxxxxxxxxxxxxx">
                    <p class="text-xs text-gray-400 mt-2">
                        在 <a href="{NEW_API_URL}/token" target="_blank" class="text-blue-500 hover:underline">主站令牌管理</a> 中获取
                    </p>
                </div>
                <button onclick="verifyKey()" id="verify-btn"
                        class="w-full btn-primary text-white py-3 rounded-lg font-semibold hover:opacity-90 transition">
                    验证并登录
                </button>
            </div>
        </div>

        <!-- 领取区域 -->
        <div id="claim-section" class="hidden">
            <div class="card p-6 mb-6">
                <div class="flex items-center justify-between mb-4">
                    <div>
                        <p class="text-gray-500 text-sm">当前用户</p>
                        <p id="user-info" class="font-semibold text-gray-800"></p>
                    </div>
                    <button onclick="logout()" class="text-gray-400 hover:text-gray-600">退出</button>
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
                    <li>前往 <a href="{NEW_API_URL}/topup" target="_blank" class="text-blue-500 hover:underline">主站充值页面</a></li>
                    <li>在"兑换码充值"处粘贴并兑换</li>
                </ol>
            </div>
        </div>
    </main>

    <footer class="text-center py-6 text-gray-400 text-sm">{SITE_NAME} © 2025</footer>

    <script>
        let apiKey = localStorage.getItem('coupon_api_key') || '';
        let currentUser = null;

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
            
            if (!apiKey) {{
                showToast('请输入 API Key', false);
                return;
            }}

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
                    currentUser = data.data;
                    localStorage.setItem('coupon_api_key', apiKey);
                    showLoggedIn();
                    await loadStatus();
                }} else {{
                    showToast(data.detail || 'API Key 无效', false);
                }}
            }} catch (e) {{
                showToast('网络错误', false);
            }}

            btn.disabled = false;
            btn.textContent = '验证并登录';
        }}

        function showLoggedIn() {{
            document.getElementById('login-section').classList.add('hidden');
            document.getElementById('claim-section').classList.remove('hidden');
            document.getElementById('user-info').textContent = 
                `${{currentUser.display_name || currentUser.username}} (ID: ${{currentUser.id}})`;
        }}

        function logout() {{
            localStorage.removeItem('coupon_api_key');
            apiKey = '';
            currentUser = null;
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
            }} catch (e) {{
                console.error(e);
            }}
        }}

        function updateUI(data) {{
            const btn = document.getElementById('claim-btn');
            const badge = document.getElementById('status-badge');
            const msg = document.getElementById('cooldown-msg');
            
            if (data.can_claim) {{
                btn.disabled = false;
                badge.textContent = '✅ 可领取';
                badge.className = 'px-3 py-1 rounded-full text-sm font-medium bg-green-100 text-green-600';
                msg.textContent = '';
            }} else {{
                btn.disabled = true;
                badge.textContent = '⏳ 冷却中';
                badge.className = 'px-3 py-1 rounded-full text-sm font-medium bg-yellow-100 text-yellow-600';
                msg.textContent = `请在 ${{data.cooldown_text}} 后再试`;
            }}
            
            renderHistory(data.history || []);
        }}

        function renderHistory(records) {{
            const c = document.getElementById('history-container');
            if (!records.length) {{
                c.innerHTML = '<p class="text-gray-400 text-center py-4">暂无记录</p>';
                return;
            }}
            c.innerHTML = records.map(r => `
                <div class="coupon-card text-white p-4 rounded-xl mb-3 relative">
                    <div class="flex justify-between items-center">
                        <div class="font-mono">${{r.coupon_code}}</div>
                        <button onclick="copyCode('${{r.coupon_code}}')" class="bg-white/20 hover:bg-white/30 px-3 py-1 rounded text-sm">📋 复制</button>
                    </div>
                    <div class="text-blue-200 text-xs mt-2">领取: ${{new Date(r.claim_time).toLocaleString('zh-CN')}}</div>
                    ${{r.is_expired ? '<div class="expired-stamp">已过期</div>' : ''}}
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
                    showToast('领取成功！');
                    await navigator.clipboard.writeText(data.data.coupon_code);
                    showToast('已复制到剪贴板');
                }} else {{
                    showToast(data.detail || '领取失败', false);
                }}
            }} catch (e) {{
                showToast('网络错误', false);
            }}

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

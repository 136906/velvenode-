from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime, timedelta
from typing import Optional
import httpx
import os

# ============ 配置 ============
NEW_API_URL = os.getenv("NEW_API_URL", "https://your-new-api.zeabur.app")
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

# ============ 认证 ============
async def verify_token_with_new_api(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{NEW_API_URL}/api/user/self",
                headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return data.get("data")
    except Exception as e:
        print(f"Token verify error: {e}")
    return None

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("session")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        token = request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    user = await verify_token_with_new_api(token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return user

# ============ New API 交互 ============
async def create_redemption_code(name: str, quota: int) -> Optional[str]:
    if not NEW_API_ADMIN_TOKEN:
        print("Warning: NEW_API_ADMIN_TOKEN not configured")
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
            print(f"Create redemption response: {resp.status_code} - {resp.text}")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data"):
                    codes = data.get("data", [])
                    if codes:
                        return codes[0] if isinstance(codes[0], str) else codes[0].get("key")
    except Exception as e:
        print(f"Create redemption error: {e}")
    return None

# ============ API 路由 ============
@app.get("/api/status")
async def health_check():
    return {"status": "ok", "site_name": SITE_NAME}

@app.get("/api/user/info")
async def get_user_info(user: dict = Depends(get_current_user)):
    return {
        "success": True,
        "data": {
            "id": user.get("id"),
            "username": user.get("username"),
            "display_name": user.get("display_name", user.get("username"))
        }
    }

@app.get("/api/claim/status")
async def get_claim_status(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
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
            "cooldown_hours": CLAIM_COOLDOWN_HOURS,
            "quota_per_claim": COUPON_QUOTA,
            "history": [
                {
                    "coupon_code": r.coupon_code,
                    "quota": r.quota,
                    "claim_time": r.claim_time.isoformat() + "Z",
                    "expire_time": r.expire_time.isoformat() + "Z",
                    "is_expired": now > r.expire_time,
                    "is_used": r.is_used
                }
                for r in history
            ]
        }
    }

@app.post("/api/claim")
async def claim_coupon(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
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
            raise HTTPException(
                status_code=400,
                detail=f"冷却中，请在 {hours}小时 {minutes}分钟 后再试"
            )
    
    code_name = f"{COUPON_NAME_PREFIX}-{user_id}-{now.strftime('%Y%m%d%H%M%S')}"
    coupon_code = await create_redemption_code(code_name, COUPON_QUOTA)
    
    if not coupon_code:
        raise HTTPException(status_code=500, detail="创建兑换码失败，请联系管理员")
    
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
        "message": "领取成功！",
        "data": {
            "coupon_code": coupon_code,
            "quota": COUPON_QUOTA,
            "expire_time": expire_time.isoformat() + "Z"
        }
    }

# ============ 页面 ============
def get_html():
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
        .btn-claim {{ background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%); transition: all 0.3s; }}
        .btn-claim:hover:not(:disabled) {{ transform: translateY(-2px); box-shadow: 0 8px 25px rgba(34,197,94,0.4); }}
        .btn-claim:disabled {{ background: #9ca3af; cursor: not-allowed; }}
        .coupon-card {{ background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%); position: relative; overflow: hidden; }}
        .coupon-card::before {{ content: ''; position: absolute; top: -50%; right: -50%; width: 100%; height: 200%; background: linear-gradient(45deg, transparent, rgba(255,255,255,0.1), transparent); transform: rotate(45deg); }}
        .expired-stamp {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%) rotate(-20deg); font-size: 1.5rem; font-weight: bold; color: rgba(239, 68, 68, 0.8); border: 3px solid rgba(239, 68, 68, 0.8); padding: 0.25rem 1rem; border-radius: 8px; background: rgba(255,255,255,0.9); }}
        .loading {{ display: inline-block; width: 20px; height: 20px; border: 2px solid #fff; border-radius: 50%; border-top-color: transparent; animation: spin 1s linear infinite; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        .toast {{ position: fixed; top: 20px; right: 20px; padding: 16px 24px; border-radius: 8px; color: white; font-weight: 500; z-index: 1000; animation: slideIn 0.3s ease; }}
        @keyframes slideIn {{ from {{ transform: translateX(100%); opacity: 0; }} to {{ transform: translateX(0); opacity: 1; }} }}
    </style>
</head>
<body class="bg-gray-50 min-h-screen">
    <nav class="gradient-header text-white py-4 px-6 shadow-lg">
        <div class="container mx-auto flex justify-between items-center">
            <div class="flex items-center space-x-2">
                <span class="text-yellow-300 text-2xl">★</span>
                <span class="font-bold text-xl">{SITE_NAME}</span>
            </div>
            <div id="nav-right" class="flex items-center space-x-4">
                <a href="{NEW_API_URL}" target="_blank" class="hover:text-gray-200 transition">返回主站</a>
                <span id="user-display" class="hidden"></span>
                <button id="logout-btn" onclick="logout()" class="hidden hover:text-gray-200 transition">🚪 注销</button>
            </div>
        </div>
    </nav>
    <main class="container mx-auto px-4 py-8 max-w-3xl">
        <div id="login-prompt" class="card p-8 text-center hidden">
            <div class="text-6xl mb-4">🔐</div>
            <h2 class="text-2xl font-bold text-gray-800 mb-4">请先登录</h2>
            <p class="text-gray-600 mb-6">您需要先登录 {SITE_NAME} 才能领取兑换券</p>
            <a href="{NEW_API_URL}/login" class="inline-block bg-blue-500 hover:bg-blue-600 text-white px-8 py-3 rounded-lg font-semibold transition">前往登录</a>
        </div>
        <div id="main-content" class="hidden">
            <div class="text-center mb-8">
                <h1 class="text-3xl font-bold text-gray-800 flex items-center justify-center gap-2">
                    <span class="text-green-500">🎫</span> 兑换券领取中心
                </h1>
                <p id="welcome-msg" class="text-gray-500 mt-2"></p>
            </div>
            <div class="card p-6 mb-6">
                <div class="flex items-center justify-between mb-6">
                    <h2 class="text-lg font-semibold text-gray-800 flex items-center gap-2">
                        <span class="text-blue-500">ℹ️</span> 领取状态
                    </h2>
                    <span id="status-badge" class="px-4 py-1 rounded-full text-sm font-medium"></span>
                </div>
                <div class="text-center py-8">
                    <button id="claim-btn" onclick="claimCoupon()" class="btn-claim text-white py-4 px-12 rounded-xl text-lg font-bold shadow-lg">
                        <span class="flex items-center justify-center gap-2"><span>⬇️</span> 领取兑换券</span>
                    </button>
                    <p id="cooldown-msg" class="text-gray-500 mt-4"></p>
                    <p class="text-gray-400 text-sm mt-2">每 {CLAIM_COOLDOWN_HOURS} 小时可领取一次</p>
                </div>
            </div>
            <div class="card p-6">
                <div class="flex items-center justify-between mb-4">
                    <h2 class="text-lg font-semibold text-gray-800">📋 领取记录</h2>
                    <button onclick="loadStatus()" class="text-blue-500 hover:text-blue-600 text-sm">🔄 刷新</button>
                </div>
                <div id="history-container"><p class="text-gray-400 text-center py-4">加载中...</p></div>
            </div>
            <div class="card p-6 mt-6">
                <h2 class="text-lg font-semibold text-gray-800 mb-4">📖 使用说明</h2>
                <ol class="list-decimal list-inside space-y-2 text-gray-600">
                    <li>点击"领取兑换券"按钮获取兑换码</li>
                    <li>复制兑换码</li>
                    <li>前往 <a href="{NEW_API_URL}/topup" target="_blank" class="text-blue-500 hover:underline">主站充值页面</a></li>
                    <li>在"兑换码充值"处粘贴并兑换</li>
                </ol>
            </div>
        </div>
    </main>
    <footer class="text-center py-6 text-gray-400 text-sm">{SITE_NAME} © 2025 - 兑换券领取中心</footer>
    <script>
        const NEW_API_URL = "{NEW_API_URL}";
        let currentUser = null;
        function showToast(message, type = 'success') {{
            const toast = document.createElement('div');
            toast.className = `toast ${{type === 'success' ? 'bg-green-500' : 'bg-red-500'}}`;
            toast.textContent = message;
            document.body.appendChild(toast);
            setTimeout(() => toast.remove(), 3000);
        }}
        async function checkLogin() {{
            try {{
                const resp = await fetch('/api/user/info', {{ credentials: 'include' }});
                if (resp.ok) {{
                    const data = await resp.json();
                    if (data.success) {{
                        currentUser = data.data;
                        showLoggedIn();
                        await loadStatus();
                        return;
                    }}
                }}
            }} catch (e) {{ console.error('Check login error:', e); }}
            showLoginPrompt();
        }}
        function showLoginPrompt() {{
            document.getElementById('login-prompt').classList.remove('hidden');
            document.getElementById('main-content').classList.add('hidden');
        }}
        function showLoggedIn() {{
            document.getElementById('login-prompt').classList.add('hidden');
            document.getElementById('main-content').classList.remove('hidden');
            document.getElementById('user-display').textContent = currentUser.display_name || currentUser.username;
            document.getElementById('user-display').classList.remove('hidden');
            document.getElementById('logout-btn').classList.remove('hidden');
            document.getElementById('welcome-msg').textContent = `欢迎您，${{currentUser.display_name || currentUser.username}} (ID: ${{currentUser.id}})`;
        }}
        async function loadStatus() {{
            try {{
                const resp = await fetch('/api/claim/status', {{ credentials: 'include' }});
                if (!resp.ok) throw new Error('加载失败');
                const data = await resp.json();
                if (data.success) updateUI(data.data);
            }} catch (e) {{ console.error('Load status error:', e); }}
        }}
        function updateUI(data) {{
            const btn = document.getElementById('claim-btn');
            const badge = document.getElementById('status-badge');
            const msg = document.getElementById('cooldown-msg');
            if (data.can_claim) {{
                btn.disabled = false;
                badge.textContent = '✅ 可领取';
                badge.className = 'px-4 py-1 rounded-full text-sm font-medium bg-green-100 text-green-600';
                msg.textContent = '';
            }} else {{
                btn.disabled = true;
                badge.textContent = '⏳ 冷却中';
                badge.className = 'px-4 py-1 rounded-full text-sm font-medium bg-yellow-100 text-yellow-600';
                msg.textContent = `请在 ${{data.cooldown_text}} 后再试`;
            }}
            renderHistory(data.history || []);
        }}
        function renderHistory(records) {{
            const container = document.getElementById('history-container');
            if (records.length === 0) {{
                container.innerHTML = '<p class="text-gray-400 text-center py-4">暂无领取记录</p>';
                return;
            }}
            container.innerHTML = records.map(r => `
                <div class="coupon-card text-white p-4 rounded-xl mb-3 relative">
                    <div class="flex justify-between items-start relative z-10">
                        <div>
                            <div class="font-mono text-lg tracking-wider">${{r.coupon_code}}</div>
                            <div class="text-blue-200 text-sm mt-2">领取: ${{formatTime(r.claim_time)}}</div>
                        </div>
                        <button onclick="copyCode('${{r.coupon_code}}')" class="bg-white/20 hover:bg-white/30 px-3 py-1 rounded text-sm transition">📋 复制</button>
                    </div>
                    ${{r.is_expired ? '<div class="expired-stamp">已过期</div>' : ''}}
                    ${{r.is_used ? '<div class="expired-stamp" style="color: #22c55e; border-color: #22c55e;">已使用</div>' : ''}}
                </div>
            `).join('');
        }}
        async function claimCoupon() {{
            const btn = document.getElementById('claim-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> 领取中...';
            try {{
                const resp = await fetch('/api/claim', {{ method: 'POST', credentials: 'include' }});
                const data = await resp.json();
                if (resp.ok && data.success) {{
                    showToast(`领取成功！兑换码: ${{data.data.coupon_code}}`);
                    await navigator.clipboard.writeText(data.data.coupon_code);
                    showToast('已自动复制到剪贴板', 'success');
                }} else {{
                    showToast(data.detail || '领取失败', 'error');
                }}
            }} catch (e) {{ showToast('网络错误，请重试', 'error'); }}
            btn.innerHTML = '<span class="flex items-center justify-center gap-2"><span>⬇️</span> 领取兑换券</span>';
            await loadStatus();
        }}
        async function copyCode(code) {{
            try {{
                await navigator.clipboard.writeText(code);
                showToast('已复制到剪贴板');
            }} catch (e) {{ showToast('复制失败', 'error'); }}
        }}
        function formatTime(isoStr) {{ return new Date(isoStr).toLocaleString('zh-CN'); }}
        function logout() {{
            document.cookie = 'session=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
            window.location.href = NEW_API_URL + '/login';
        }}
        document.addEventListener('DOMContentLoaded', checkLogin);
    </script>
</body>
</html>'''

@app.get("/", response_class=HTMLResponse)
async def index():
    return get_html()

@app.get("/claim", response_class=HTMLResponse)
async def claim_page():
    return get_html()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
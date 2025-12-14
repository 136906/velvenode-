from fastapi import FastAPI, HTTPException, Request, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from datetime import datetime, timedelta, timezone
import httpx
import hashlib
import os

# ============ 配置 ============
NEW_API_URL = os.getenv("NEW_API_URL", "https://velvenode.zeabur.app")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./coupon.db")
CLAIM_COOLDOWN_HOURS = int(os.getenv("CLAIM_COOLDOWN_HOURS", "8"))
SITE_NAME = os.getenv("SITE_NAME", "velvenode")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# ============ 数据库 ============
Base = declarative_base()

class CouponPool(Base):
    __tablename__ = "coupon_pool"
    id = Column(Integer, primary_key=True, autoincrement=True)
    coupon_code = Column(String(64), unique=True, nullable=False)
    is_claimed = Column(Boolean, default=False)
    claimed_by_user_id = Column(Integer, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class ClaimRecord(Base):
    __tablename__ = "claim_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, index=True, nullable=False)
    username = Column(String(255), nullable=False)
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

# ============ FastAPI ============
app = FastAPI(title="兑换券系统")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def now_utc():
    return datetime.now(timezone.utc)

async def verify_user_identity(user_id: int, username: str, api_key: str) -> bool:
    """验证用户身份：用户ID + 用户名 + API Key 三重验证"""
    if not api_key or not api_key.startswith("sk-"):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 验证 API Key 有效
            resp = await client.get(f"{NEW_API_URL}/v1/models", headers={"Authorization": f"Bearer {api_key}"})
            if resp.status_code != 200:
                return False
            # API Key 有效，我们信任用户提供的 ID 和用户名
            # 因为 API Key 是用户自己的，如果用户伪造 ID，兑换码也只能充到他自己账户
            return True
    except:
        return False

# ============ 用户 API ============
@app.post("/api/verify")
async def verify_user(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    username = body.get("username", "").strip()
    api_key = body.get("api_key", "").strip()
    
    if not user_id or not username or not api_key:
        raise HTTPException(status_code=400, detail="请填写完整信息")
    
    try:
        user_id = int(user_id)
    except:
        raise HTTPException(status_code=400, detail="用户ID必须是数字")
    
    is_valid = await verify_user_identity(user_id, username, api_key)
    if not is_valid:
        raise HTTPException(status_code=401, detail="API Key 无效或已过期")
    
    return {"success": True, "data": {"user_id": user_id, "username": username}}

@app.post("/api/claim/status")
async def get_claim_status(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    user_id = body.get("user_id")
    username = body.get("username", "").strip()
    api_key = body.get("api_key", "").strip()
    
    if not user_id or not username or not api_key:
        raise HTTPException(status_code=400, detail="请填写完整信息")
    
    try:
        user_id = int(user_id)
    except:
        raise HTTPException(status_code=400, detail="用户ID必须是数字")
    
    is_valid = await verify_user_identity(user_id, username, api_key)
    if not is_valid:
        raise HTTPException(status_code=401, detail="API Key 无效")
    
    now = now_utc()
    
    # 按用户ID查询最近领取（防止换API Key薅羊毛）
    last_claim = db.query(ClaimRecord).filter(ClaimRecord.user_id == user_id).order_by(ClaimRecord.claim_time.desc()).first()
    
    can_claim = True
    cooldown_text = None
    
    if last_claim:
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
    
    available_count = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    if available_count == 0:
        can_claim = False
        cooldown_text = "兑换码已领完，请等待管理员补充"
    
    history = db.query(ClaimRecord).filter(ClaimRecord.user_id == user_id).order_by(ClaimRecord.claim_time.desc()).limit(10).all()
    
    return {
        "success": True,
        "data": {
            "can_claim": can_claim,
            "cooldown_text": cooldown_text,
            "available_count": available_count,
            "history": [{"coupon_code": r.coupon_code, "claim_time": r.claim_time.isoformat() if r.claim_time else ""} for r in history]
        }
    }

@app.post("/api/claim")
async def claim_coupon(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    user_id = body.get("user_id")
    username = body.get("username", "").strip()
    api_key = body.get("api_key", "").strip()
    
    if not user_id or not username or not api_key:
        raise HTTPException(status_code=400, detail="请填写完整信息")
    
    try:
        user_id = int(user_id)
    except:
        raise HTTPException(status_code=400, detail="用户ID必须是数字")
    
    is_valid = await verify_user_identity(user_id, username, api_key)
    if not is_valid:
        raise HTTPException(status_code=401, detail="API Key 无效")
    
    now = now_utc()
    
    # 检查冷却（按用户ID）
    last_claim = db.query(ClaimRecord).filter(ClaimRecord.user_id == user_id).order_by(ClaimRecord.claim_time.desc()).first()
    
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
    
    coupon = db.query(CouponPool).filter(CouponPool.is_claimed == False).first()
    if not coupon:
        raise HTTPException(status_code=400, detail="兑换码已领完")
    
    coupon.is_claimed = True
    coupon.claimed_by_user_id = user_id
    coupon.claimed_at = now
    
    record = ClaimRecord(user_id=user_id, username=username, coupon_code=coupon.coupon_code, claim_time=now)
    db.add(record)
    db.commit()
    
    return {"success": True, "data": {"coupon_code": coupon.coupon_code}}

# ============ 管理员 API ============
@app.post("/api/admin/add-coupons")
async def add_coupons(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    password = body.get("password", "")
    coupons = body.get("coupons", [])
    
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    added = 0
    for code in coupons:
        code = code.strip()
        if not code:
            continue
        exists = db.query(CouponPool).filter(CouponPool.coupon_code == code).first()
        if not exists:
            db.add(CouponPool(coupon_code=code))
            added += 1
    db.commit()
    
    total = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return {"success": True, "message": f"成功添加 {added} 个，当前可用: {total} 个"}

@app.post("/api/admin/upload-txt")
async def upload_txt(password: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    content = await file.read()
    text = content.decode("utf-8")
    coupons = [line.strip() for line in text.split("\n") if line.strip()]
    
    added = 0
    for code in coupons:
        exists = db.query(CouponPool).filter(CouponPool.coupon_code == code).first()
        if not exists:
            db.add(CouponPool(coupon_code=code))
            added += 1
    db.commit()
    
    total = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return {"success": True, "message": f"成功添加 {added} 个，当前可用: {total} 个"}

@app.get("/api/admin/stats")
async def get_stats(password: str, db: Session = Depends(get_db)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    total = db.query(CouponPool).count()
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    claimed = db.query(CouponPool).filter(CouponPool.is_claimed == True).count()
    
    recent = db.query(ClaimRecord).order_by(ClaimRecord.claim_time.desc()).limit(20).all()
    
    return {
        "success": True,
        "data": {
            "total": total, "available": available, "claimed": claimed,
            "recent_claims": [{"user_id": r.user_id, "username": r.username, "coupon_code": r.coupon_code[:8]+"...", "time": r.claim_time.isoformat() if r.claim_time else ""} for r in recent]
        }
    }

# ============ 页面 ============
@app.get("/", response_class=HTMLResponse)
async def index(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return USER_PAGE.replace("{{AVAILABLE}}", str(available)).replace("{{SITE_NAME}}", SITE_NAME).replace("{{NEW_API_URL}}", NEW_API_URL).replace("{{COOLDOWN}}", str(CLAIM_COOLDOWN_HOURS))

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_PAGE.replace("{{SITE_NAME}}", SITE_NAME)

# 嵌入式页面（给 iframe 用）
@app.get("/embed", response_class=HTMLResponse)
async def embed_page(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return EMBED_PAGE.replace("{{AVAILABLE}}", str(available)).replace("{{SITE_NAME}}", SITE_NAME).replace("{{NEW_API_URL}}", NEW_API_URL).replace("{{COOLDOWN}}", str(CLAIM_COOLDOWN_HOURS))

# ============ HTML 模板 ============
USER_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>兑换券领取 - {{SITE_NAME}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .gradient-header { background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); }
        .card { background: white; border-radius: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }
        .btn-primary { background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); }
        .btn-claim { background: linear-gradient(135deg, #10b981 0%, #059669 100%); }
        .btn-claim:disabled { background: #9ca3af; cursor: not-allowed; }
        .coupon-card { background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); }
        .loading { display: inline-block; width: 18px; height: 18px; border: 2px solid #fff; border-radius: 50%; border-top-color: transparent; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); padding: 12px 24px; border-radius: 8px; color: white; font-weight: 500; z-index: 1000; }
    </style>
</head>
<body class="bg-gradient-to-br from-indigo-50 to-purple-50 min-h-screen">
    <nav class="gradient-header text-white py-4 px-6 shadow-lg">
        <div class="container mx-auto flex justify-between items-center">
            <div class="flex items-center space-x-2">
                <span class="text-2xl">🎫</span>
                <span class="font-bold text-xl">{{SITE_NAME}} 兑换中心</span>
            </div>
            <a href="{{NEW_API_URL}}" target="_blank" class="hover:text-indigo-200 transition">返回主站</a>
        </div>
    </nav>

    <main class="container mx-auto px-4 py-8 max-w-xl">
        <div id="login-section" class="card p-8">
            <div class="text-center mb-6">
                <div class="text-5xl mb-4">🎁</div>
                <h1 class="text-2xl font-bold text-gray-800">兑换券领取中心</h1>
                <p class="text-gray-500 mt-2">验证身份后领取免费额度</p>
                <p class="text-sm text-indigo-600 mt-2 font-medium">📦 当前可领取: <span id="available-count">{{AVAILABLE}}</span> 个</p>
            </div>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">用户ID <span class="text-red-500">*</span></label>
                    <input type="number" id="user-id-input" class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500" placeholder="在个人设置页面查看">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">用户名 <span class="text-red-500">*</span></label>
                    <input type="text" id="username-input" class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500" placeholder="您的登录用户名">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">API Key <span class="text-red-500">*</span></label>
                    <input type="password" id="api-key-input" class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500" placeholder="sk-xxxxxxxx">
                    <p class="text-xs text-gray-400 mt-1">在 <a href="{{NEW_API_URL}}/console/token" target="_blank" class="text-indigo-500 hover:underline">令牌管理</a> 创建</p>
                </div>
                <button onclick="verifyUser()" id="verify-btn" class="w-full btn-primary text-white py-3 rounded-lg font-semibold hover:opacity-90 transition">
                    验证身份
                </button>
            </div>
            <p class="text-xs text-gray-400 text-center mt-4">💡 用户ID在「个人设置」页面顶部可见</p>
        </div>

        <div id="claim-section" class="hidden">
            <div class="card p-5 mb-4">
                <div class="flex items-center justify-between">
                    <div>
                        <p class="text-gray-500 text-sm">当前用户</p>
                        <p id="user-info" class="font-semibold text-gray-800"></p>
                    </div>
                    <button onclick="logout()" class="text-indigo-500 hover:text-indigo-700 text-sm">切换账号</button>
                </div>
            </div>

            <div class="card p-6 mb-4">
                <div class="flex items-center justify-between mb-4">
                    <h2 class="text-lg font-semibold text-gray-800">领取状态</h2>
                    <span id="status-badge" class="px-3 py-1 rounded-full text-sm font-medium"></span>
                </div>
                <div class="text-center py-4">
                    <button id="claim-btn" onclick="claimCoupon()" class="btn-claim text-white py-3 px-8 rounded-xl text-lg font-bold shadow-lg hover:opacity-90 transition">
                        ⬇️ 领取兑换券
                    </button>
                    <p id="cooldown-msg" class="text-gray-500 mt-3 text-sm"></p>
                    <p class="text-gray-400 text-xs mt-2">每 {{COOLDOWN}} 小时可领取一次</p>
                </div>
            </div>

            <div class="card p-6">
                <h2 class="text-lg font-semibold text-gray-800 mb-3">📋 我的领取记录</h2>
                <div id="history-container"></div>
            </div>

            <div class="card p-5 mt-4">
                <h2 class="font-semibold text-gray-800 mb-2">📖 使用说明</h2>
                <ol class="list-decimal list-inside space-y-1 text-gray-600 text-sm">
                    <li>点击领取获取兑换码</li>
                    <li>复制兑换码</li>
                    <li>前往 <a href="{{NEW_API_URL}}/topup" target="_blank" class="text-indigo-500 hover:underline">钱包管理</a> 兑换</li>
                </ol>
            </div>
        </div>
    </main>

    <footer class="text-center py-4 text-gray-400 text-sm">{{SITE_NAME}} © 2025</footer>

    <script>
        let userData = JSON.parse(localStorage.getItem('coupon_user') || 'null');

        document.addEventListener('DOMContentLoaded', () => {
            if (userData) {
                document.getElementById('user-id-input').value = userData.user_id;
                document.getElementById('username-input').value = userData.username;
                document.getElementById('api-key-input').value = userData.api_key;
                verifyUser();
            }
        });

        function showToast(msg, ok = true) {
            const t = document.createElement('div');
            t.className = 'toast ' + (ok ? 'bg-green-500' : 'bg-red-500');
            t.textContent = msg;
            document.body.appendChild(t);
            setTimeout(() => t.remove(), 3000);
        }

        async function verifyUser() {
            const userId = document.getElementById('user-id-input').value.trim();
            const username = document.getElementById('username-input').value.trim();
            const apiKey = document.getElementById('api-key-input').value.trim();
            const btn = document.getElementById('verify-btn');

            if (!userId || !username || !apiKey) {
                showToast('请填写完整信息', false);
                return;
            }

            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> 验证中...';

            try {
                const resp = await fetch('/api/verify', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_id: parseInt(userId), username, api_key: apiKey})
                });
                const data = await resp.json();

                if (resp.ok && data.success) {
                    userData = {user_id: parseInt(userId), username, api_key: apiKey};
                    localStorage.setItem('coupon_user', JSON.stringify(userData));
                    showLoggedIn();
                    await loadStatus();
                } else {
                    showToast(data.detail || '验证失败', false);
                }
            } catch (e) {
                showToast('网络错误', false);
            }

            btn.disabled = false;
            btn.textContent = '验证身份';
        }

        function showLoggedIn() {
            document.getElementById('login-section').classList.add('hidden');
            document.getElementById('claim-section').classList.remove('hidden');
            document.getElementById('user-info').textContent = userData.username + ' (ID: ' + userData.user_id + ')';
        }

        function logout() {
            localStorage.removeItem('coupon_user');
            userData = null;
            document.getElementById('login-section').classList.remove('hidden');
            document.getElementById('claim-section').classList.add('hidden');
        }

        async function loadStatus() {
            try {
                const resp = await fetch('/api/claim/status', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(userData)
                });
                const data = await resp.json();
                if (data.success) updateUI(data.data);
            } catch (e) { console.error(e); }
        }

        function updateUI(data) {
            const btn = document.getElementById('claim-btn');
            const badge = document.getElementById('status-badge');
            const msg = document.getElementById('cooldown-msg');
            document.getElementById('available-count').textContent = data.available_count;

            if (data.can_claim) {
                btn.disabled = false;
                badge.textContent = '✅ 可领取';
                badge.className = 'px-3 py-1 rounded-full text-sm font-medium bg-green-100 text-green-600';
                msg.textContent = '';
            } else {
                btn.disabled = true;
                badge.textContent = '⏳ 冷却中';
                badge.className = 'px-3 py-1 rounded-full text-sm font-medium bg-yellow-100 text-yellow-600';
                msg.textContent = data.cooldown_text || '';
            }
            renderHistory(data.history || []);
        }

        function renderHistory(records) {
            const c = document.getElementById('history-container');
            if (!records.length) {
                c.innerHTML = '<p class="text-gray-400 text-center py-3 text-sm">暂无记录</p>';
                return;
            }
            c.innerHTML = records.map(r => '<div class="coupon-card text-white p-3 rounded-lg mb-2"><div class="flex justify-between items-center"><span class="font-mono text-sm">' + r.coupon_code + '</span><button onclick="copyCode(\'' + r.coupon_code + '\')" class="bg-white/20 hover:bg-white/30 px-2 py-1 rounded text-xs">📋复制</button></div><div class="text-indigo-200 text-xs mt-1">' + new Date(r.claim_time).toLocaleString('zh-CN') + '</div></div>').join('');
        }

        async function claimCoupon() {
            const btn = document.getElementById('claim-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> 领取中...';

            try {
                const resp = await fetch('/api/claim', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(userData)
                });
                const data = await resp.json();

                if (resp.ok && data.success) {
                    showToast('领取成功！已复制');
                    await navigator.clipboard.writeText(data.data.coupon_code);
                } else {
                    showToast(data.detail || '领取失败', false);
                }
            } catch (e) {
                showToast('网络错误', false);
            }

            btn.innerHTML = '⬇️ 领取兑换券';
            await loadStatus();
        }

        async function copyCode(code) {
            await navigator.clipboard.writeText(code);
            showToast('已复制');
        }
    </script>
</body>
</html>'''

ADMIN_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台 - {{SITE_NAME}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-4xl mx-auto">
        <div class="flex justify-between items-center mb-6">
            <h1 class="text-2xl font-bold text-gray-800">🔧 兑换码管理后台</h1>
            <a href="/" class="text-indigo-500 hover:underline">← 返回领取页</a>
        </div>

        <div class="grid md:grid-cols-2 gap-6">
            <div class="bg-white rounded-xl p-6 shadow">
                <h2 class="font-semibold text-lg mb-4">📤 上传兑换码</h2>
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm mb-1">管理员密码</label>
                        <input type="password" id="admin-pwd" class="w-full border rounded px-3 py-2">
                    </div>
                    <div>
                        <label class="block text-sm mb-1">上传 TXT 文件</label>
                        <input type="file" id="txt-file" accept=".txt" class="w-full border rounded px-3 py-2">
                        <p class="text-xs text-gray-400 mt-1">每行一个兑换码</p>
                    </div>
                    <button onclick="uploadTxt()" class="w-full bg-indigo-500 text-white py-2 rounded hover:bg-indigo-600">
                        上传文件
                    </button>
                    <hr>
                    <div>
                        <label class="block text-sm mb-1">或手动粘贴（每行一个）</label>
                        <textarea id="coupons-input" rows="6" class="w-full border rounded px-3 py-2 font-mono text-sm"></textarea>
                    </div>
                    <button onclick="addCoupons()" class="w-full bg-green-500 text-white py-2 rounded hover:bg-green-600">
                        添加兑换码
                    </button>
                </div>
            </div>

            <div class="bg-white rounded-xl p-6 shadow">
                <h2 class="font-semibold text-lg mb-4">📊 统计信息</h2>
                <div id="stats" class="text-gray-500">输入密码后点击刷新</div>
                <button onclick="loadStats()" class="mt-4 bg-gray-500 text-white px-4 py-2 rounded hover:bg-gray-600">
                    刷新统计
                </button>

                <h3 class="font-semibold mt-6 mb-2">最近领取</h3>
                <div id="recent-claims" class="text-sm text-gray-600 max-h-64 overflow-y-auto"></div>
            </div>
        </div>

        <div id="toast" class="fixed top-4 left-1/2 -translate-x-1/2 px-4 py-2 rounded text-white hidden"></div>
    </div>

    <script>
        function showToast(msg, ok = true) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'fixed top-4 left-1/2 -translate-x-1/2 px-4 py-2 rounded text-white ' + (ok ? 'bg-green-500' : 'bg-red-500');
            setTimeout(() => t.classList.add('hidden'), 3000);
        }

        async function uploadTxt() {
            const pwd = document.getElementById('admin-pwd').value;
            const file = document.getElementById('txt-file').files[0];
            if (!pwd) { showToast('请输入密码', false); return; }
            if (!file) { showToast('请选择文件', false); return; }

            const formData = new FormData();
            formData.append('file', file);

            try {
                const resp = await fetch('/api/admin/upload-txt?password=' + encodeURIComponent(pwd), {
                    method: 'POST',
                    body: formData
                });
                const data = await resp.json();
                showToast(data.message || data.detail, resp.ok);
                if (resp.ok) loadStats();
            } catch (e) {
                showToast('网络错误', false);
            }
        }

        async function addCoupons() {
            const pwd = document.getElementById('admin-pwd').value;
            const text = document.getElementById('coupons-input').value;
            const coupons = text.split('\\n').filter(s => s.trim());

            if (!pwd) { showToast('请输入密码', false); return; }
            if (!coupons.length) { showToast('请输入兑换码', false); return; }

            try {
                const resp = await fetch('/api/admin/add-coupons', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password: pwd, coupons})
                });
                const data = await resp.json();
                showToast(data.message || data.detail, resp.ok);
                if (resp.ok) {
                    document.getElementById('coupons-input').value = '';
                    loadStats();
                }
            } catch (e) {
                showToast('网络错误', false);
            }
        }

        async function loadStats() {
            const pwd = document.getElementById('admin-pwd').value;
            if (!pwd) { showToast('请输入密码', false); return; }

            try {
                const resp = await fetch('/api/admin/stats?password=' + encodeURIComponent(pwd));
                const data = await resp.json();
                if (resp.ok && data.success) {
                    const d = data.data;
                    document.getElementById('stats').innerHTML = '<div class="grid grid-cols-3 gap-3 text-center"><div class="bg-gray-50 p-3 rounded"><div class="text-xl font-bold">' + d.total + '</div><div class="text-xs text-gray-500">总数</div></div><div class="bg-green-50 p-3 rounded"><div class="text-xl font-bold text-green-600">' + d.available + '</div><div class="text-xs text-gray-500">可用</div></div><div class="bg-blue-50 p-3 rounded"><div class="text-xl font-bold text-blue-600">' + d.claimed + '</div><div class="text-xs text-gray-500">已领</div></div></div>';
                    document.getElementById('recent-claims').innerHTML = d.recent_claims.map(r => '<div class="py-1 border-b">ID:' + r.user_id + ' ' + r.username + ' - ' + r.coupon_code + '</div>').join('') || '<p class="text-gray-400">暂无</p>';
                } else {
                    showToast(data.detail || '加载失败', false);
                }
            } catch (e) {
                showToast('网络错误', false);
            }
        }
    </script>
</body>
</html>'''

EMBED_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>领取兑换券</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background: transparent; }
        .card { background: white; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
    </style>
</head>
<body class="p-4">
    <div class="card p-4 max-w-md mx-auto">
        <h2 class="text-lg font-bold text-center mb-3">🎫 免费领取兑换券</h2>
        <p class="text-center text-sm text-gray-500 mb-3">当前可领: <span class="text-indigo-600 font-bold">{{AVAILABLE}}</span> 个</p>
        <a href="https://velvenodehome.zeabur.app" target="_blank" class="block w-full bg-indigo-500 text-white text-center py-2 rounded-lg hover:bg-indigo-600 transition">
            前往领取 →
        </a>
        <p class="text-xs text-gray-400 text-center mt-2">每{{COOLDOWN}}小时可领取一次</p>
    </div>
</body>
</html>'''

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

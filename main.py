from fastapi import FastAPI, HTTPException, Request, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Float
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from datetime import datetime, timedelta, timezone
import httpx
import random
import os

# ============ 配置 ============
NEW_API_URL = os.getenv("NEW_API_URL", "https://velvenode.zeabur.app")
COUPON_SITE_URL = os.getenv("COUPON_SITE_URL", "https://velvenodehome.zeabur.app")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./coupon.db")
CLAIM_COOLDOWN_HOURS = int(os.getenv("CLAIM_COOLDOWN_HOURS", "8"))
SITE_NAME = os.getenv("SITE_NAME", "velvenode")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# 额度概率配置
QUOTA_WEIGHTS = {
    1: 50, 5: 30, 10: 15, 50: 4, 100: 1,
}

# ============ 数据库 ============
Base = declarative_base()

class CouponPool(Base):
    __tablename__ = "coupon_pool"
    id = Column(Integer, primary_key=True, autoincrement=True)
    coupon_code = Column(String(64), unique=True, nullable=False)
    quota_dollars = Column(Float, default=1.0)
    is_claimed = Column(Boolean, default=False)
    claimed_by_user_id = Column(Integer, nullable=True)
    claimed_by_username = Column(String(255), nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class ClaimRecord(Base):
    __tablename__ = "claim_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, index=True, nullable=False)
    username = Column(String(255), nullable=False)
    coupon_code = Column(String(64), nullable=False)
    quota_dollars = Column(Float, default=1.0)
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

def get_random_coupon(db: Session):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).all()
    if not available:
        return None
    by_quota = {}
    for c in available:
        q = c.quota_dollars
        if q not in by_quota:
            by_quota[q] = []
        by_quota[q].append(c)
    choices, weights = [], []
    for quota, coupons in by_quota.items():
        weight = QUOTA_WEIGHTS.get(int(quota), QUOTA_WEIGHTS.get(1, 50))
        if quota > 100:
            weight = max(1, 100 - int(quota))
        choices.append((quota, coupons))
        weights.append(weight)
    if not choices:
        return None
    selected = random.choices(choices, weights=weights, k=1)[0]
    return random.choice(selected[1])

async def verify_user_identity(user_id: int, username: str, api_key: str) -> bool:
    if not api_key or not api_key.startswith("sk-"):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{NEW_API_URL}/v1/models", headers={"Authorization": f"Bearer {api_key}"})
            return resp.status_code == 200
    except:
        return False

# ============ 用户 API ============
@app.post("/api/verify")
async def verify_user(request: Request):
    body = await request.json()
    user_id, username, api_key = body.get("user_id"), body.get("username", "").strip(), body.get("api_key", "").strip()
    if not user_id or not username or not api_key:
        raise HTTPException(status_code=400, detail="请填写完整信息")
    try:
        user_id = int(user_id)
    except:
        raise HTTPException(status_code=400, detail="用户ID必须是数字")
    if not await verify_user_identity(user_id, username, api_key):
        raise HTTPException(status_code=401, detail="API Key 无效或已过期")
    return {"success": True, "data": {"user_id": user_id, "username": username}}

@app.post("/api/claim/status")
async def get_claim_status(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    user_id, username, api_key = body.get("user_id"), body.get("username", "").strip(), body.get("api_key", "").strip()
    if not user_id or not username or not api_key:
        raise HTTPException(status_code=400, detail="请填写完整信息")
    try:
        user_id = int(user_id)
    except:
        raise HTTPException(status_code=400, detail="用户ID必须是数字")
    if not await verify_user_identity(user_id, username, api_key):
        raise HTTPException(status_code=401, detail="API Key 无效")
    
    now = now_utc()
    last_claim = db.query(ClaimRecord).filter(ClaimRecord.user_id == user_id).order_by(ClaimRecord.claim_time.desc()).first()
    can_claim, cooldown_text = True, None
    if last_claim:
        last_time = last_claim.claim_time.replace(tzinfo=timezone.utc) if last_claim.claim_time.tzinfo is None else last_claim.claim_time
        next_claim_time = last_time + timedelta(hours=CLAIM_COOLDOWN_HOURS)
        if now < next_claim_time:
            can_claim = False
            remaining = next_claim_time - now
            total_seconds = int(remaining.total_seconds())
            h, m, s = total_seconds // 3600, (total_seconds % 3600) // 60, total_seconds % 60
            cooldown_text = f"{h}小时 {m}分钟 {s}秒"
    
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    if available == 0:
        can_claim, cooldown_text = False, "兑换码已领完，请等待补充"
    
    quota_stats = {}
    for q in [1, 5, 10, 50, 100]:
        cnt = db.query(CouponPool).filter(CouponPool.is_claimed == False, CouponPool.quota_dollars == q).count()
        if cnt > 0:
            quota_stats[f"${q}"] = cnt
    
    history = db.query(ClaimRecord).filter(ClaimRecord.user_id == user_id).order_by(ClaimRecord.claim_time.desc()).limit(10).all()
    return {
        "success": True,
        "data": {
            "can_claim": can_claim, "cooldown_text": cooldown_text, "available_count": available,
            "quota_stats": quota_stats,
            "history": [{"coupon_code": r.coupon_code, "quota": r.quota_dollars, "claim_time": r.claim_time.isoformat()} for r in history]
        }
    }

@app.post("/api/claim")
async def claim_coupon(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    user_id, username, api_key = body.get("user_id"), body.get("username", "").strip(), body.get("api_key", "").strip()
    if not user_id or not username or not api_key:
        raise HTTPException(status_code=400, detail="请填写完整信息")
    try:
        user_id = int(user_id)
    except:
        raise HTTPException(status_code=400, detail="用户ID必须是数字")
    if not await verify_user_identity(user_id, username, api_key):
        raise HTTPException(status_code=401, detail="API Key 无效")
    
    now = now_utc()
    last_claim = db.query(ClaimRecord).filter(ClaimRecord.user_id == user_id).order_by(ClaimRecord.claim_time.desc()).first()
    if last_claim:
        last_time = last_claim.claim_time.replace(tzinfo=timezone.utc) if last_claim.claim_time.tzinfo is None else last_claim.claim_time
        next_claim_time = last_time + timedelta(hours=CLAIM_COOLDOWN_HOURS)
        if now < next_claim_time:
            remaining = next_claim_time - now
            h, m = int(remaining.total_seconds()) // 3600, (int(remaining.total_seconds()) % 3600) // 60
            raise HTTPException(status_code=400, detail=f"冷却中，请在 {h}小时 {m}分钟 后再试")
    
    coupon = get_random_coupon(db)
    if not coupon:
        raise HTTPException(status_code=400, detail="兑换码已领完")
    
    coupon.is_claimed, coupon.claimed_by_user_id, coupon.claimed_by_username, coupon.claimed_at = True, user_id, username, now
    record = ClaimRecord(user_id=user_id, username=username, coupon_code=coupon.coupon_code, quota_dollars=coupon.quota_dollars, claim_time=now)
    db.add(record)
    db.commit()
    return {"success": True, "data": {"coupon_code": coupon.coupon_code, "quota": coupon.quota_dollars}}

# ============ 管理员 API ============
@app.post("/api/admin/add-coupons")
async def add_coupons(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    password, coupons, quota = body.get("password", ""), body.get("coupons", []), float(body.get("quota", 1))
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    added = 0
    for code in coupons:
        code = code.strip()
        if code and not db.query(CouponPool).filter(CouponPool.coupon_code == code).first():
            db.add(CouponPool(coupon_code=code, quota_dollars=quota))
            added += 1
    db.commit()
    total = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return {"success": True, "message": f"成功添加 {added} 个 ${quota} 兑换码，当前可用: {total} 个"}

@app.post("/api/admin/upload-txt")
async def upload_txt(password: str = Form(...), quota: float = Form(1), file: UploadFile = File(...), db: Session = Depends(get_db)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    content = await file.read()
    lines = content.decode("utf-8").strip().split("\n")
    added = 0
    for line in lines:
        code = line.strip()
        if code and not db.query(CouponPool).filter(CouponPool.coupon_code == code).first():
            db.add(CouponPool(coupon_code=code, quota_dollars=quota))
            added += 1
    db.commit()
    total = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return {"success": True, "message": f"成功添加 {added} 个 ${quota} 兑换码，当前可用: {total} 个"}

@app.get("/api/admin/stats")
async def get_stats(password: str, db: Session = Depends(get_db)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    total = db.query(CouponPool).count()
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    claimed = db.query(CouponPool).filter(CouponPool.is_claimed == True).count()
    quota_stats = {}
    for q in [1, 5, 10, 50, 100]:
        avail = db.query(CouponPool).filter(CouponPool.is_claimed == False, CouponPool.quota_dollars == q).count()
        used = db.query(CouponPool).filter(CouponPool.is_claimed == True, CouponPool.quota_dollars == q).count()
        if avail > 0 or used > 0:
            quota_stats[f"${q}"] = {"available": avail, "claimed": used}
    recent = db.query(ClaimRecord).order_by(ClaimRecord.claim_time.desc()).limit(50).all()
    return {
        "success": True,
        "data": {
            "total": total, "available": available, "claimed": claimed, "quota_stats": quota_stats,
            "recent_claims": [{"user_id": r.user_id, "username": r.username, "quota": r.quota_dollars, "code": r.coupon_code[:8]+"...", "time": r.claim_time.strftime("%m-%d %H:%M") if r.claim_time else ""} for r in recent]
        }
    }

@app.get("/api/stats/public")
async def get_public_stats(db: Session = Depends(get_db)):
    """公开统计接口，用于嵌入组件"""
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return {"available": available, "cooldown_hours": CLAIM_COOLDOWN_HOURS}

# ============ 页面路由 ============
@app.get("/", response_class=HTMLResponse)
async def index(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return USER_PAGE.replace("{{AVAILABLE}}", str(available)).replace("{{SITE_NAME}}", SITE_NAME).replace("{{NEW_API_URL}}", NEW_API_URL).replace("{{COOLDOWN}}", str(CLAIM_COOLDOWN_HOURS))

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_PAGE.replace("{{SITE_NAME}}", SITE_NAME)

@app.get("/embed", response_class=HTMLResponse)
async def embed_page(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return EMBED_PAGE.replace("{{AVAILABLE}}", str(available)).replace("{{COOLDOWN}}", str(CLAIM_COOLDOWN_HOURS)).replace("{{COUPON_SITE_URL}}", COUPON_SITE_URL)

@app.get("/widget", response_class=HTMLResponse)
async def widget_page(db: Session = Depends(get_db)):
    """小型入口组件，可嵌入主站"""
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return WIDGET_PAGE.replace("{{AVAILABLE}}", str(available)).replace("{{COUPON_SITE_URL}}", COUPON_SITE_URL)

# ============ HTML 模板 - 黑蓝色调 ============
USER_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>兑换券领取 - {{SITE_NAME}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        :root { --bg-primary: #0a0a0f; --bg-secondary: #12121a; --bg-card: #1a1a24; --accent: #3b82f6; --accent-hover: #2563eb; --text-primary: #f0f0f0; --text-secondary: #9ca3af; --border: #2a2a3a; }
        body { background: var(--bg-primary); color: var(--text-primary); }
        .card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 16px; }
        .btn-primary { background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%); }
        .btn-primary:hover { background: linear-gradient(135deg, #2563eb 0%, #1e40af 100%); }
        .btn-claim { background: linear-gradient(135deg, #10b981 0%, #059669 100%); }
        .btn-claim:hover { background: linear-gradient(135deg, #059669 0%, #047857 100%); }
        .btn-claim:disabled { background: #374151; cursor: not-allowed; opacity: 0.6; }
        .input-dark { background: var(--bg-secondary); border: 1px solid var(--border); color: var(--text-primary); }
        .input-dark:focus { border-color: var(--accent); outline: none; box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.3); }
        .coupon-card { background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%); }
        .loading { display: inline-block; width: 18px; height: 18px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: #fff; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); padding: 12px 24px; border-radius: 8px; color: white; font-weight: 500; z-index: 1000; }
        .prize-animation { animation: prize 0.5s ease-out; }
        @keyframes prize { 0% { transform: scale(0.5); opacity: 0; } 50% { transform: scale(1.2); } 100% { transform: scale(1); opacity: 1; } }
        .glow { box-shadow: 0 0 20px rgba(59, 130, 246, 0.3); }
    </style>
</head>
<body class="min-h-screen">
    <nav class="bg-gradient-to-r from-blue-600 to-blue-800 text-white py-4 px-6 shadow-lg">
        <div class="container mx-auto flex justify-between items-center">
            <div class="flex items-center space-x-2">
                <span class="text-2xl">🎫</span>
                <span class="font-bold text-xl">{{SITE_NAME}} 兑换中心</span>
            </div>
            <div class="flex items-center space-x-4">
                <a href="/admin" class="hover:text-blue-200 transition text-sm">管理后台</a>
                <a href="{{NEW_API_URL}}" target="_blank" class="hover:text-blue-200 transition">返回主站 →</a>
            </div>
        </div>
    </nav>

    <main class="container mx-auto px-4 py-8 max-w-xl">
        <div id="login-section" class="card p-8 glow">
            <div class="text-center mb-6">
                <div class="text-5xl mb-4">🎁</div>
                <h1 class="text-2xl font-bold">兑换券领取中心</h1>
                <p class="text-gray-400 mt-2">验证身份后领取免费额度</p>
                <div class="mt-4 inline-flex items-center bg-blue-900/50 text-blue-300 px-4 py-2 rounded-full border border-blue-700">
                    <span class="text-lg mr-2">📦</span>
                    当前可领取: <span id="available-count" class="font-bold text-blue-200 ml-1">{{AVAILABLE}}</span> 个
                </div>
                <p class="text-xs text-gray-500 mt-2">🎰 随机额度: $1~$100，大额低概率</p>
            </div>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">用户ID</label>
                    <input type="number" id="user-id-input" class="w-full px-4 py-3 rounded-lg input-dark" placeholder="个人设置页面查看">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">用户名</label>
                    <input type="text" id="username-input" class="w-full px-4 py-3 rounded-lg input-dark" placeholder="登录用户名">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">API Key</label>
                    <input type="password" id="api-key-input" class="w-full px-4 py-3 rounded-lg input-dark" placeholder="sk-xxx">
                    <p class="text-xs text-gray-500 mt-1">在 <a href="{{NEW_API_URL}}/console/token" target="_blank" class="text-blue-400 hover:text-blue-300">令牌管理</a> 创建</p>
                </div>
                <button onclick="verifyUser()" id="verify-btn" class="w-full btn-primary text-white py-3 rounded-lg font-semibold transition-all hover:shadow-lg">验证身份</button>
            </div>
        </div>

        <div id="claim-section" class="hidden">
            <div class="card p-5 mb-4">
                <div class="flex justify-between items-center">
                    <div>
                        <p class="text-gray-500 text-sm">当前用户</p>
                        <p id="user-info" class="font-semibold"></p>
                    </div>
                    <button onclick="logout()" class="text-blue-400 hover:text-blue-300 text-sm transition">切换账号</button>
                </div>
            </div>

            <div class="card p-6 mb-4 glow">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-lg font-semibold">领取状态</h2>
                    <span id="status-badge" class="px-3 py-1 rounded-full text-sm"></span>
                </div>
                <div id="quota-stats" class="flex flex-wrap gap-2 mb-4 text-sm"></div>
                <div class="text-center py-4">
                    <button id="claim-btn" onclick="claimCoupon()" class="btn-claim text-white py-4 px-10 rounded-xl text-lg font-bold shadow-lg transition-all hover:shadow-xl hover:scale-105">
                        🎰 抽取兑换券
                    </button>
                    <p id="cooldown-msg" class="text-gray-500 mt-3 text-sm"></p>
                </div>
                <div id="prize-display" class="hidden text-center py-4">
                    <div class="prize-animation">
                        <div id="prize-amount" class="text-4xl font-bold text-green-400"></div>
                        <div id="prize-code" class="font-mono text-lg mt-2 bg-gray-800 p-3 rounded-lg border border-gray-700"></div>
                    </div>
                </div>
            </div>

            <div class="card p-6">
                <h2 class="font-semibold mb-3 flex items-center"><span class="mr-2">📋</span>领取记录</h2>
                <div id="history-container"></div>
            </div>
        </div>
    </main>

    <footer class="text-center py-6 text-gray-600 text-sm">
        <p>每 {{COOLDOWN}} 小时可领取一次 | <a href="{{NEW_API_URL}}" class="text-blue-400 hover:text-blue-300">{{SITE_NAME}}</a></p>
    </footer>

    <script>
        let userData = JSON.parse(localStorage.getItem('coupon_user') || 'null');
        document.addEventListener('DOMContentLoaded', () => { if (userData) { fillForm(); verifyUser(); } });

        function fillForm() {
            document.getElementById('user-id-input').value = userData.user_id;
            document.getElementById('username-input').value = userData.username;
            document.getElementById('api-key-input').value = userData.api_key;
        }

        function showToast(msg, ok = true) {
            const t = document.createElement('div');
            t.className = 'toast ' + (ok ? 'bg-green-600' : 'bg-red-600');
            t.textContent = msg;
            document.body.appendChild(t);
            setTimeout(() => t.remove(), 3000);
        }

        async function verifyUser() {
            const userId = document.getElementById('user-id-input').value.trim();
            const username = document.getElementById('username-input').value.trim();
            const apiKey = document.getElementById('api-key-input').value.trim();
            if (!userId || !username || !apiKey) { showToast('请填写完整', false); return; }

            const btn = document.getElementById('verify-btn');
            btn.disabled = true; btn.innerHTML = '<span class="loading"></span>';

            try {
                const resp = await fetch('/api/verify', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_id: parseInt(userId), username, api_key: apiKey})
                });
                const data = await resp.json();
                if (resp.ok) {
                    userData = {user_id: parseInt(userId), username, api_key: apiKey};
                    localStorage.setItem('coupon_user', JSON.stringify(userData));
                    showLoggedIn();
                    loadStatus();
                } else { showToast(data.detail, false); }
            } catch (e) { showToast('网络错误', false); }
            btn.disabled = false; btn.textContent = '验证身份';
        }

        function showLoggedIn() {
            document.getElementById('login-section').classList.add('hidden');
            document.getElementById('claim-section').classList.remove('hidden');
            document.getElementById('user-info').textContent = userData.username + ' (ID:' + userData.user_id + ')';
        }

        function logout() {
            localStorage.removeItem('coupon_user'); userData = null;
            document.getElementById('login-section').classList.remove('hidden');
            document.getElementById('claim-section').classList.add('hidden');
        }

        async function loadStatus() {
            const resp = await fetch('/api/claim/status', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(userData)
            });
            const data = await resp.json();
            if (data.success) updateUI(data.data);
        }

        function updateUI(data) {
            document.getElementById('available-count').textContent = data.available_count;
            const btn = document.getElementById('claim-btn');
            const badge = document.getElementById('status-badge');
            const msg = document.getElementById('cooldown-msg');

            if (data.can_claim) {
                btn.disabled = false;
                badge.textContent = '✅ 可领取';
                badge.className = 'px-3 py-1 rounded-full text-sm bg-green-900/50 text-green-400 border border-green-700';
                msg.textContent = '';
            } else {
                btn.disabled = true;
                badge.textContent = '⏳ 冷却中';
                badge.className = 'px-3 py-1 rounded-full text-sm bg-yellow-900/50 text-yellow-400 border border-yellow-700';
                msg.textContent = data.cooldown_text || '';
            }

            const stats = document.getElementById('quota-stats');
            stats.innerHTML = Object.entries(data.quota_stats || {}).map(([k,v]) => 
                '<span class="bg-blue-900/50 text-blue-300 px-2 py-1 rounded border border-blue-800">' + k + ': ' + v + '个</span>'
            ).join('');

            renderHistory(data.history || []);
        }

        function renderHistory(records) {
            const c = document.getElementById('history-container');
            if (!records.length) { c.innerHTML = '<p class="text-gray-500 text-center py-3 text-sm">暂无领取记录</p>'; return; }
            c.innerHTML = records.map(r => 
                '<div class="coupon-card text-white p-3 rounded-lg mb-2"><div class="flex justify-between items-center"><span class="font-mono text-sm">' + r.coupon_code + '</span><span class="bg-white/20 px-2 py-0.5 rounded text-sm">$' + r.quota + '</span></div><div class="flex justify-between items-center mt-2"><span class="text-xs text-blue-200">' + new Date(r.claim_time).toLocaleString('zh-CN') + '</span><button onclick="copyCode(\'' + r.coupon_code + '\')" class="text-xs bg-white/20 hover:bg-white/30 px-2 py-0.5 rounded transition">复制</button></div></div>'
            ).join('');
        }

        async function claimCoupon() {
            const btn = document.getElementById('claim-btn');
            btn.disabled = true; btn.innerHTML = '<span class="loading"></span> 抽取中...';
            document.getElementById('prize-display').classList.add('hidden');

            try {
                const resp = await fetch('/api/claim', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(userData)
                });
                const data = await resp.json();
                if (resp.ok && data.success) {
                    document.getElementById('prize-amount').textContent = '🎉 $' + data.data.quota;
                    document.getElementById('prize-code').textContent = data.data.coupon_code;
                    document.getElementById('prize-display').classList.remove('hidden');
                    await navigator.clipboard.writeText(data.data.coupon_code);
                    showToast('恭喜！兑换码已复制到剪贴板');
                } else {
                    showToast(data.detail, false);
                }
            } catch (e) { showToast('网络错误', false); }

            btn.innerHTML = '🎰 抽取兑换券';
            loadStatus();
        }

        async function copyCode(code) {
            await navigator.clipboard.writeText(code);
            showToast('已复制到剪贴板');
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
    <style>
        :root { --bg-primary: #0a0a0f; --bg-secondary: #12121a; --bg-card: #1a1a24; --accent: #3b82f6; --text-primary: #f0f0f0; --text-secondary: #9ca3af; --border: #2a2a3a; }
        body { background: var(--bg-primary); color: var(--text-primary); }
        .card { background: var(--bg-card); border: 1px solid var(--border); }
        .input-dark { background: var(--bg-secondary); border: 1px solid var(--border); color: var(--text-primary); }
        .input-dark:focus { border-color: var(--accent); outline: none; }
        .toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); padding: 12px 24px; border-radius: 8px; color: white; font-weight: 500; z-index: 1000; }
    </style>
</head>
<body class="min-h-screen p-4">
    <div class="max-w-6xl mx-auto">
        <div class="flex justify-between items-center mb-6">
            <h1 class="text-2xl font-bold flex items-center"><span class="mr-2">🔧</span>兑换码管理后台</h1>
            <a href="/" class="text-blue-400 hover:text-blue-300 transition">← 返回前台</a>
        </div>

        <div class="grid lg:grid-cols-3 gap-6">
            <div class="lg:col-span-2 space-y-4">
                <div class="card rounded-xl p-6">
                    <h2 class="font-semibold mb-4 flex items-center"><span class="mr-2">🔐</span>管理员密码</h2>
                    <input type="password" id="admin-pwd" class="w-full rounded-lg px-4 py-3 input-dark" placeholder="输入管理员密码">
                </div>

                <div class="card rounded-xl p-6">
                    <h2 class="font-semibold mb-4 flex items-center"><span class="mr-2">📤</span>快速上传（按额度分类）</h2>
                    <div class="grid grid-cols-5 gap-2 mb-4">
                        <button onclick="setQuota(1)" class="quota-btn bg-green-900/50 text-green-400 border border-green-700 py-2 rounded-lg font-bold hover:bg-green-900 transition">$1</button>
                        <button onclick="setQuota(5)" class="quota-btn bg-blue-900/50 text-blue-400 border border-blue-700 py-2 rounded-lg font-bold hover:bg-blue-900 transition">$5</button>
                        <button onclick="setQuota(10)" class="quota-btn bg-purple-900/50 text-purple-400 border border-purple-700 py-2 rounded-lg font-bold hover:bg-purple-900 transition">$10</button>
                        <button onclick="setQuota(50)" class="quota-btn bg-orange-900/50 text-orange-400 border border-orange-700 py-2 rounded-lg font-bold hover:bg-orange-900 transition">$50</button>
                        <button onclick="setQuota(100)" class="quota-btn bg-red-900/50 text-red-400 border border-red-700 py-2 rounded-lg font-bold hover:bg-red-900 transition">$100</button>
                    </div>
                    <div class="flex items-center gap-2 mb-4">
                        <span class="text-gray-400">当前额度:</span>
                        <input type="number" id="quota-input" value="1" class="w-20 rounded-lg px-3 py-2 text-center font-bold input-dark">
                        <span class="text-gray-400">美元</span>
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm text-gray-400 mb-2">上传 TXT 文件（每行一个兑换码）</label>
                        <input type="file" id="txt-file" accept=".txt" class="w-full rounded-lg px-4 py-3 input-dark file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:bg-blue-600 file:text-white file:cursor-pointer">
                    </div>
                    <button onclick="uploadTxt()" class="w-full bg-blue-600 hover:bg-blue-700 text-white py-3 rounded-lg font-semibold transition">上传文件</button>
                    
                    <hr class="my-6 border-gray-700">
                    
                    <div>
                        <label class="block text-sm text-gray-400 mb-2">或手动粘贴（每行一个）</label>
                        <textarea id="coupons-input" rows="5" class="w-full rounded-lg px-4 py-3 font-mono text-sm input-dark" placeholder="粘贴兑换码，每行一个..."></textarea>
                    </div>
                    <button onclick="addCoupons()" class="w-full mt-3 bg-green-600 hover:bg-green-700 text-white py-3 rounded-lg font-semibold transition">添加兑换码</button>
                </div>

                <div class="card rounded-xl p-6">
                    <h2 class="font-semibold mb-4 flex items-center"><span class="mr-2">🎰</span>概率说明</h2>
                    <div class="grid grid-cols-5 gap-2 text-center text-sm">
                        <div class="bg-green-900/30 p-2 rounded-lg border border-green-800"><div class="text-green-400 font-bold">$1</div><div class="text-gray-500">50%</div></div>
                        <div class="bg-blue-900/30 p-2 rounded-lg border border-blue-800"><div class="text-blue-400 font-bold">$5</div><div class="text-gray-500">30%</div></div>
                        <div class="bg-purple-900/30 p-2 rounded-lg border border-purple-800"><div class="text-purple-400 font-bold">$10</div><div class="text-gray-500">15%</div></div>
                        <div class="bg-orange-900/30 p-2 rounded-lg border border-orange-800"><div class="text-orange-400 font-bold">$50</div><div class="text-gray-500">4%</div></div>
                        <div class="bg-red-900/30 p-2 rounded-lg border border-red-800"><div class="text-red-400 font-bold">$100</div><div class="text-gray-500">1%</div></div>
                    </div>
                    <p class="text-gray-500 text-xs mt-3 text-center">用户抽取时按此概率随机分配对应额度的兑换码</p>
                </div>
            </div>

            <div class="space-y-4">
                <div class="card rounded-xl p-6">
                    <div class="flex justify-between items-center mb-4">
                        <h2 class="font-semibold flex items-center"><span class="mr-2">📊</span>统计数据</h2>
                        <button onclick="loadStats()" class="text-blue-400 hover:text-blue-300 text-sm transition">刷新</button>
                    </div>
                    <div id="stats" class="text-gray-500">输入密码后点击刷新加载</div>
                </div>

                <div class="card rounded-xl p-6">
                    <h2 class="font-semibold mb-4 flex items-center"><span class="mr-2">📋</span>最近领取</h2>
                    <div id="recent-claims" class="text-sm max-h-96 overflow-y-auto space-y-2"></div>
                </div>
            </div>
        </div>

        <div id="toast" class="toast hidden"></div>
    </div>

    <script>
        function setQuota(q) {
            document.getElementById('quota-input').value = q;
            document.querySelectorAll('.quota-btn').forEach(b => b.classList.remove('ring-2', 'ring-blue-500'));
            event.target.classList.add('ring-2', 'ring-blue-500');
        }

        function showToast(msg, ok = true) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'toast ' + (ok ? 'bg-green-600' : 'bg-red-600');
            setTimeout(() => t.classList.add('hidden'), 3000);
        }

        async function uploadTxt() {
            const pwd = document.getElementById('admin-pwd').value;
            const quota = document.getElementById('quota-input').value;
            const file = document.getElementById('txt-file').files[0];
            if (!pwd) { showToast('请输入密码', false); return; }
            if (!file) { showToast('请选择文件', false); return; }

            const formData = new FormData();
            formData.append('password', pwd);
            formData.append('quota', quota);
            formData.append('file', file);

            const resp = await fetch('/api/admin/upload-txt', { method: 'POST', body: formData });
            const data = await resp.json();
            showToast(data.message || data.detail, resp.ok);
            if (resp.ok) { loadStats(); document.getElementById('txt-file').value = ''; }
        }

        async function addCoupons() {
            const pwd = document.getElementById('admin-pwd').value;
            const quota = parseFloat(document.getElementById('quota-input').value);
            const text = document.getElementById('coupons-input').value;
            const coupons = text.split('\\n').filter(s => s.trim());

            if (!pwd) { showToast('请输入密码', false); return; }
            if (!coupons.length) { showToast('请输入兑换码', false); return; }

            const resp = await fetch('/api/admin/add-coupons', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({password: pwd, quota, coupons})
            });
            const data = await resp.json();
            showToast(data.message || data.detail, resp.ok);
            if (resp.ok) { loadStats(); document.getElementById('coupons-input').value = ''; }
        }

        async function loadStats() {
            const pwd = document.getElementById('admin-pwd').value;
            if (!pwd) { showToast('请输入密码', false); return; }

            const resp = await fetch('/api/admin/stats?password=' + encodeURIComponent(pwd));
            const data = await resp.json();
            
            if (resp.ok && data.success) {
                const d = data.data;
                let statsHtml = '<div class="grid grid-cols-3 gap-2 text-center mb-4">' +
                    '<div class="bg-gray-800 p-3 rounded-lg"><div class="text-2xl font-bold">' + d.total + '</div><div class="text-xs text-gray-500">总数</div></div>' +
                    '<div class="bg-green-900/30 p-3 rounded-lg border border-green-800"><div class="text-2xl font-bold text-green-400">' + d.available + '</div><div class="text-xs text-gray-500">可用</div></div>' +
                    '<div class="bg-blue-900/30 p-3 rounded-lg border border-blue-800"><div class="text-2xl font-bold text-blue-400">' + d.claimed + '</div><div class="text-xs text-gray-500">已领</div></div>' +
                    '</div>';
                
                statsHtml += '<div class="space-y-2">';
                for (const [k, v] of Object.entries(d.quota_stats || {})) {
                    statsHtml += '<div class="flex justify-between items-center text-sm bg-gray-800/50 p-2 rounded"><span class="font-medium">' + k + '</span><span class="text-green-400">' + v.available + ' 可用</span><span class="text-gray-500">' + v.claimed + ' 已领</span></div>';
                }
                statsHtml += '</div>';
                
                document.getElementById('stats').innerHTML = statsHtml;
                document.getElementById('recent-claims').innerHTML = d.recent_claims.map(r => 
                    '<div class="bg-gray-800/50 p-2 rounded text-gray-400"><span class="text-blue-400">ID:' + r.user_id + '</span> <span class="text-gray-300">' + r.username + '</span> <span class="text-green-400 font-medium">$' + r.quota + '</span> <span class="text-gray-600 text-xs">' + r.time + '</span></div>'
                ).join('') || '<p class="text-gray-600 text-center">暂无记录</p>';
            } else {
                showToast(data.detail, false);
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
    <script src="https://cdn.tailwindcss.com"></script>
    <style>body{background:transparent;margin:0;padding:8px}</style>
</head>
<body>
<div class="bg-gradient-to-r from-blue-600 to-blue-800 text-white p-4 rounded-xl text-center shadow-lg">
    <div class="text-xl font-bold mb-1">🎫 免费领取兑换券</div>
    <div class="text-sm opacity-90 mb-2">可领: <b>{{AVAILABLE}}</b> 个 | 随机 $1~$100 | 每{{COOLDOWN}}小时一次</div>
    <a href="{{COUPON_SITE_URL}}" target="_blank" class="inline-block bg-white text-blue-600 px-5 py-2 rounded-full font-bold hover:bg-blue-50 transition shadow">立即领取 →</a>
</div>
</body>
</html>'''

WIDGET_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: transparent; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
        .widget {
            background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%);
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 16px;
            color: white;
            max-width: 280px;
        }
        .widget-header { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
        .widget-icon { font-size: 24px; }
        .widget-title { font-weight: 600; font-size: 14px; }
        .widget-stats { display: flex; justify-content: space-between; margin-bottom: 12px; font-size: 12px; color: #94a3b8; }
        .widget-count { color: #60a5fa; font-weight: 700; font-size: 18px; }
        .widget-btn {
            display: block;
            width: 100%;
            background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
            color: white;
            text-align: center;
            padding: 10px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            font-size: 14px;
            transition: all 0.2s;
        }
        .widget-btn:hover { background: linear-gradient(135deg, #2563eb 0%, #1e40af 100%); transform: translateY(-1px); }
    </style>
</head>
<body>
<div class="widget">
    <div class="widget-header">
        <span class="widget-icon">🎫</span>
        <span class="widget-title">兑换券领取中心</span>
    </div>
    <div class="widget-stats">
        <span>当前可领</span>
        <span class="widget-count">{{AVAILABLE}} 个</span>
    </div>
    <a href="{{COUPON_SITE_URL}}" target="_blank" class="widget-btn">🎁 免费领取 →</a>
</div>
</body>
</html>'''

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

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
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./coupon.db")
CLAIM_COOLDOWN_HOURS = int(os.getenv("CLAIM_COOLDOWN_HOURS", "8"))
SITE_NAME = os.getenv("SITE_NAME", "velvenode")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# 额度概率配置（额度: 概率权重）
QUOTA_WEIGHTS = {
    1: 50,    # 1$ - 50%
    5: 30,    # 5$ - 30%
    10: 15,   # 10$ - 15%
    50: 4,    # 50$ - 4%
    100: 1,   # 100$ - 1%
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
    """根据概率随机选择一个兑换码"""
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).all()
    if not available:
        return None
    
    # 按额度分组
    by_quota = {}
    for c in available:
        q = c.quota_dollars
        if q not in by_quota:
            by_quota[q] = []
        by_quota[q].append(c)
    
    # 计算权重
    choices = []
    weights = []
    for quota, coupons in by_quota.items():
        weight = QUOTA_WEIGHTS.get(int(quota), QUOTA_WEIGHTS.get(1, 50))
        # 额度越大权重越小
        if quota > 100:
            weight = max(1, 100 - int(quota))
        choices.append((quota, coupons))
        weights.append(weight)
    
    if not choices:
        return None
    
    # 随机选择额度
    selected = random.choices(choices, weights=weights, k=1)[0]
    quota, coupons = selected
    
    # 从该额度中随机选一个
    return random.choice(coupons)

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
    user_id = body.get("user_id")
    username = body.get("username", "").strip()
    api_key = body.get("api_key", "").strip()
    
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
    user_id = body.get("user_id")
    username = body.get("username", "").strip()
    api_key = body.get("api_key", "").strip()
    
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
    
    can_claim = True
    cooldown_text = None
    
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
        can_claim = False
        cooldown_text = "兑换码已领完，请等待补充"
    
    # 统计各额度数量
    quota_stats = {}
    for q in [1, 5, 10, 50, 100]:
        cnt = db.query(CouponPool).filter(CouponPool.is_claimed == False, CouponPool.quota_dollars == q).count()
        if cnt > 0:
            quota_stats[f"${q}"] = cnt
    
    history = db.query(ClaimRecord).filter(ClaimRecord.user_id == user_id).order_by(ClaimRecord.claim_time.desc()).limit(10).all()
    
    return {
        "success": True,
        "data": {
            "can_claim": can_claim,
            "cooldown_text": cooldown_text,
            "available_count": available,
            "quota_stats": quota_stats,
            "history": [{"coupon_code": r.coupon_code, "quota": r.quota_dollars, "claim_time": r.claim_time.isoformat()} for r in history]
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
    
    coupon.is_claimed = True
    coupon.claimed_by_user_id = user_id
    coupon.claimed_by_username = username
    coupon.claimed_at = now
    
    record = ClaimRecord(user_id=user_id, username=username, coupon_code=coupon.coupon_code, quota_dollars=coupon.quota_dollars, claim_time=now)
    db.add(record)
    db.commit()
    
    return {"success": True, "data": {"coupon_code": coupon.coupon_code, "quota": coupon.quota_dollars}}

# ============ 管理员 API ============
@app.post("/api/admin/add-coupons")
async def add_coupons(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    password = body.get("password", "")
    coupons = body.get("coupons", [])
    quota = float(body.get("quota", 1))
    
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    added = 0
    for code in coupons:
        code = code.strip()
        if not code:
            continue
        if not db.query(CouponPool).filter(CouponPool.coupon_code == code).first():
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
    
    # 各额度统计
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
            "total": total, "available": available, "claimed": claimed,
            "quota_stats": quota_stats,
            "recent_claims": [{"user_id": r.user_id, "username": r.username, "quota": r.quota_dollars, "code": r.coupon_code[:8]+"...", "time": r.claim_time.strftime("%m-%d %H:%M") if r.claim_time else ""} for r in recent]
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

@app.get("/embed", response_class=HTMLResponse)
async def embed_page(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return EMBED_PAGE.replace("{{AVAILABLE}}", str(available)).replace("{{COOLDOWN}}", str(CLAIM_COOLDOWN_HOURS))

# ============ HTML ============
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
        .prize-animation { animation: prize 0.5s ease-out; }
        @keyframes prize { 0% { transform: scale(0.5); opacity: 0; } 50% { transform: scale(1.2); } 100% { transform: scale(1); opacity: 1; } }
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
                <div class="mt-3 inline-block bg-indigo-100 text-indigo-700 px-4 py-2 rounded-full">
                    📦 当前可领取: <span id="available-count" class="font-bold">{{AVAILABLE}}</span> 个
                </div>
                <p class="text-xs text-gray-400 mt-2">🎰 随机额度: $1~$100，大额低概率</p>
            </div>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">用户ID</label>
                    <input type="number" id="user-id-input" class="w-full px-4 py-2 border rounded-lg focus:ring-2 focus:ring-indigo-500" placeholder="个人设置页面查看">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">用户名</label>
                    <input type="text" id="username-input" class="w-full px-4 py-2 border rounded-lg focus:ring-2 focus:ring-indigo-500" placeholder="登录用户名">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">API Key</label>
                    <input type="password" id="api-key-input" class="w-full px-4 py-2 border rounded-lg focus:ring-2 focus:ring-indigo-500" placeholder="sk-xxx">
                    <p class="text-xs text-gray-400 mt-1">在 <a href="{{NEW_API_URL}}/console/token" target="_blank" class="text-indigo-500">令牌管理</a> 创建</p>
                </div>
                <button onclick="verifyUser()" id="verify-btn" class="w-full btn-primary text-white py-3 rounded-lg font-semibold">验证身份</button>
            </div>
        </div>

        <div id="claim-section" class="hidden">
            <div class="card p-5 mb-4">
                <div class="flex justify-between items-center">
                    <div>
                        <p class="text-gray-500 text-sm">当前用户</p>
                        <p id="user-info" class="font-semibold text-gray-800"></p>
                    </div>
                    <button onclick="logout()" class="text-indigo-500 text-sm">切换</button>
                </div>
            </div>

            <div class="card p-6 mb-4">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-lg font-semibold">领取状态</h2>
                    <span id="status-badge" class="px-3 py-1 rounded-full text-sm"></span>
                </div>
                <div id="quota-stats" class="flex flex-wrap gap-2 mb-4 text-sm"></div>
                <div class="text-center py-4">
                    <button id="claim-btn" onclick="claimCoupon()" class="btn-claim text-white py-3 px-8 rounded-xl text-lg font-bold shadow-lg">
                        🎰 抽取兑换券
                    </button>
                    <p id="cooldown-msg" class="text-gray-500 mt-3 text-sm"></p>
                </div>
                <div id="prize-display" class="hidden text-center py-4">
                    <div class="prize-animation">
                        <div id="prize-amount" class="text-4xl font-bold text-green-500"></div>
                        <div id="prize-code" class="font-mono text-lg mt-2 bg-gray-100 p-3 rounded"></div>
                    </div>
                </div>
            </div>

            <div class="card p-6">
                <h2 class="font-semibold mb-3">📋 领取记录</h2>
                <div id="history-container"></div>
            </div>
        </div>
    </main>

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
            t.className = 'toast ' + (ok ? 'bg-green-500' : 'bg-red-500');
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
                badge.className = 'px-3 py-1 rounded-full text-sm bg-green-100 text-green-600';
                msg.textContent = '';
            } else {
                btn.disabled = true;
                badge.textContent = '⏳ 冷却中';
                badge.className = 'px-3 py-1 rounded-full text-sm bg-yellow-100 text-yellow-600';
                msg.textContent = data.cooldown_text || '';
            }

            // 额度统计
            const stats = document.getElementById('quota-stats');
            stats.innerHTML = Object.entries(data.quota_stats || {}).map(([k,v]) => 
                '<span class="bg-indigo-100 text-indigo-700 px-2 py-1 rounded">' + k + ': ' + v + '个</span>'
            ).join('');

            renderHistory(data.history || []);
        }

        function renderHistory(records) {
            const c = document.getElementById('history-container');
            if (!records.length) { c.innerHTML = '<p class="text-gray-400 text-center py-3 text-sm">暂无</p>'; return; }
            c.innerHTML = records.map(r => 
                '<div class="coupon-card text-white p-3 rounded-lg mb-2"><div class="flex justify-between"><span class="font-mono">' + r.coupon_code + '</span><span class="bg-white/20 px-2 rounded">$' + r.quota + '</span></div><div class="flex justify-between mt-1"><span class="text-xs text-indigo-200">' + new Date(r.claim_time).toLocaleString('zh-CN') + '</span><button onclick="copyCode(\'' + r.coupon_code + '\')" class="text-xs bg-white/20 px-2 rounded">复制</button></div></div>'
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
                    // 显示中奖动画
                    document.getElementById('prize-amount').textContent = '🎉 $' + data.data.quota;
                    document.getElementById('prize-code').textContent = data.data.coupon_code;
                    document.getElementById('prize-display').classList.remove('hidden');
                    await navigator.clipboard.writeText(data.data.coupon_code);
                    showToast('已复制兑换码！');
                } else {
                    showToast(data.detail, false);
                }
            } catch (e) { showToast('网络错误', false); }

            btn.innerHTML = '🎰 抽取兑换券';
            loadStatus();
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
<body class="bg-gray-100 min-h-screen p-4">
    <div class="max-w-6xl mx-auto">
        <div class="flex justify-between items-center mb-6">
            <h1 class="text-2xl font-bold">🔧 兑换码管理后台</h1>
            <a href="/" class="text-indigo-500">← 返回</a>
        </div>

        <div class="grid lg:grid-cols-3 gap-6">
            <!-- 上传区 -->
            <div class="lg:col-span-2 space-y-4">
                <div class="bg-white rounded-xl p-6 shadow">
                    <h2 class="font-semibold mb-4">🔐 管理员密码</h2>
                    <input type="password" id="admin-pwd" class="w-full border rounded px-3 py-2" placeholder="输入密码">
                </div>

                <div class="bg-white rounded-xl p-6 shadow">
                    <h2 class="font-semibold mb-4">📤 快速上传（按额度分类）</h2>
                    <div class="grid grid-cols-5 gap-2 mb-4">
                        <button onclick="setQuota(1)" class="quota-btn bg-green-100 text-green-700 py-2 rounded font-bold hover:bg-green-200">$1</button>
                        <button onclick="setQuota(5)" class="quota-btn bg-blue-100 text-blue-700 py-2 rounded font-bold hover:bg-blue-200">$5</button>
                        <button onclick="setQuota(10)" class="quota-btn bg-purple-100 text-purple-700 py-2 rounded font-bold hover:bg-purple-200">$10</button>
                        <button onclick="setQuota(50)" class="quota-btn bg-orange-100 text-orange-700 py-2 rounded font-bold hover:bg-orange-200">$50</button>
                        <button onclick="setQuota(100)" class="quota-btn bg-red-100 text-red-700 py-2 rounded font-bold hover:bg-red-200">$100</button>
                    </div>
                    <div class="flex items-center gap-2 mb-4">
                        <span>当前额度:</span>
                        <input type="number" id="quota-input" value="1" class="w-20 border rounded px-2 py-1 text-center font-bold">
                        <span>美元</span>
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm mb-1">上传 TXT 文件</label>
                        <input type="file" id="txt-file" accept=".txt" class="w-full border rounded px-3 py-2">
                    </div>
                    <button onclick="uploadTxt()" class="w-full bg-indigo-500 text-white py-2 rounded hover:bg-indigo-600">上传文件</button>
                    
                    <hr class="my-4">
                    
                    <div>
                        <label class="block text-sm mb-1">或手动粘贴（每行一个）</label>
                        <textarea id="coupons-input" rows="5" class="w-full border rounded px-3 py-2 font-mono text-sm"></textarea>
                    </div>
                    <button onclick="addCoupons()" class="w-full mt-2 bg-green-500 text-white py-2 rounded hover:bg-green-600">添加兑换码</button>
                </div>

                <div class="bg-white rounded-xl p-6 shadow">
                    <h2 class="font-semibold mb-4">🎰 概率说明</h2>
                    <div class="text-sm text-gray-600 space-y-1">
                        <p>$1: 50% | $5: 30% | $10: 15% | $50: 4% | $100: 1%</p>
                        <p class="text-gray-400">用户抽取时按此概率随机分配</p>
                    </div>
                </div>
            </div>

            <!-- 统计区 -->
            <div class="space-y-4">
                <div class="bg-white rounded-xl p-6 shadow">
                    <div class="flex justify-between items-center mb-4">
                        <h2 class="font-semibold">📊 统计</h2>
                        <button onclick="loadStats()" class="text-indigo-500 text-sm">刷新</button>
                    </div>
                    <div id="stats" class="text-gray-500">点击刷新加载</div>
                </div>

                <div class="bg-white rounded-xl p-6 shadow">
                    <h2 class="font-semibold mb-4">📋 最近领取</h2>
                    <div id="recent-claims" class="text-sm max-h-96 overflow-y-auto"></div>
                </div>
            </div>
        </div>

        <div id="toast" class="fixed top-4 left-1/2 -translate-x-1/2 px-4 py-2 rounded text-white hidden z-50"></div>
    </div>

    <script>
        function setQuota(q) {
            document.getElementById('quota-input').value = q;
            document.querySelectorAll('.quota-btn').forEach(b => b.classList.remove('ring-2', 'ring-indigo-500'));
            event.target.classList.add('ring-2', 'ring-indigo-500');
        }

        function showToast(msg, ok = true) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'fixed top-4 left-1/2 -translate-x-1/2 px-4 py-2 rounded text-white z-50 ' + (ok ? 'bg-green-500' : 'bg-red-500');
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
                    '<div class="bg-gray-50 p-2 rounded"><div class="text-xl font-bold">' + d.total + '</div><div class="text-xs text-gray-500">总数</div></div>' +
                    '<div class="bg-green-50 p-2 rounded"><div class="text-xl font-bold text-green-600">' + d.available + '</div><div class="text-xs text-gray-500">可用</div></div>' +
                    '<div class="bg-blue-50 p-2 rounded"><div class="text-xl font-bold text-blue-600">' + d.claimed + '</div><div class="text-xs text-gray-500">已领</div></div>' +
                    '</div>';
                
                // 各额度统计
                statsHtml += '<div class="space-y-1 text-sm">';
                for (const [k, v] of Object.entries(d.quota_stats || {})) {
                    statsHtml += '<div class="flex justify-between"><span>' + k + '</span><span class="text-green-600">' + v.available + '可用</span><span class="text-gray-400">' + v.claimed + '已领</span></div>';
                }
                statsHtml += '</div>';
                
                document.getElementById('stats').innerHTML = statsHtml;
                document.getElementById('recent-claims').innerHTML = d.recent_claims.map(r => 
                    '<div class="py-1 border-b text-gray-600"><span class="text-indigo-600">ID:' + r.user_id + '</span> ' + r.username + ' <span class="text-green-600">$' + r.quota + '</span> <span class="text-gray-400">' + r.time + '</span></div>'
                ).join('') || '<p class="text-gray-400">暂无</p>';
            } else {
                showToast(data.detail, false);
            }
        }
    </script>
</body>
</html>'''

EMBED_PAGE = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<script src="https://cdn.tailwindcss.com"></script>
<style>body{background:transparent}</style></head>
<body class="p-2">
<div class="bg-gradient-to-r from-indigo-500 to-purple-500 text-white p-4 rounded-xl text-center">
    <div class="text-2xl mb-1">🎫 免费领取兑换券</div>
    <div class="text-sm opacity-90">可领: <b>{{AVAILABLE}}</b>个 | 随机$1~$100</div>
    <a href="https://velvenodehome.zeabur.app" target="_blank" class="inline-block mt-2 bg-white text-indigo-600 px-4 py-1 rounded-full font-bold hover:bg-indigo-100">立即领取 →</a>
</div>
</body></html>'''

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

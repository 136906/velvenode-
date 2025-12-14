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

QUOTA_WEIGHTS = {1: 50, 5: 30, 10: 15, 50: 4, 100: 1}

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
        weight = QUOTA_WEIGHTS.get(int(quota), 50)
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

# ============ API ============
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
    return {"success": True, "data": {"can_claim": can_claim, "cooldown_text": cooldown_text, "available_count": available, "quota_stats": quota_stats, "history": [{"coupon_code": r.coupon_code, "quota": r.quota_dollars, "claim_time": r.claim_time.isoformat()} for r in history]}}

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

@app.post("/api/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    if body.get("password") != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    return {"success": True}

@app.post("/api/admin/add-coupons")
async def add_coupons(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    if body.get("password") != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    coupons, quota = body.get("coupons", []), float(body.get("quota", 1))
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
    return {"success": True, "data": {"total": total, "available": available, "claimed": claimed, "quota_stats": quota_stats, "recent_claims": [{"user_id": r.user_id, "username": r.username, "quota": r.quota_dollars, "code": r.coupon_code[:8]+"...", "time": r.claim_time.strftime("%m-%d %H:%M") if r.claim_time else ""} for r in recent]}}

@app.get("/api/stats/public")
async def get_public_stats(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return {"available": available, "cooldown_hours": CLAIM_COOLDOWN_HOURS}

# ============ 页面 ============
@app.get("/", response_class=HTMLResponse)
async def index(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return HOME_PAGE.replace("{{AVAILABLE}}", str(available)).replace("{{SITE_NAME}}", SITE_NAME).replace("{{NEW_API_URL}}", NEW_API_URL).replace("{{COOLDOWN}}", str(CLAIM_COOLDOWN_HOURS)).replace("{{COUPON_SITE_URL}}", COUPON_SITE_URL)

@app.get("/claim", response_class=HTMLResponse)
async def claim_page(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return CLAIM_PAGE.replace("{{AVAILABLE}}", str(available)).replace("{{SITE_NAME}}", SITE_NAME).replace("{{NEW_API_URL}}", NEW_API_URL).replace("{{COOLDOWN}}", str(CLAIM_COOLDOWN_HOURS))

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_PAGE.replace("{{SITE_NAME}}", SITE_NAME)

@app.get("/widget", response_class=HTMLResponse)
async def widget_page(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return WIDGET_PAGE.replace("{{AVAILABLE}}", str(available)).replace("{{COUPON_SITE_URL}}", COUPON_SITE_URL)

# ============ 首页 - 完整版 ============
HOME_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{SITE_NAME}} - API服务</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        :root{--bg:#0a0a0f;--card:#13131a;--border:#1e1e2a;--accent:#3b82f6;--text:#e0e0e0;--muted:#6b7280}
        body{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif}
        .card{background:var(--card);border:1px solid var(--border);border-radius:12px}
        .btn{padding:10px 20px;border-radius:8px;font-weight:500;transition:all .2s;display:inline-flex;align-items:center;gap:6px}
        .btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:#2563eb}
        .btn-outline{border:1px solid var(--border);color:var(--text)}.btn-outline:hover{background:var(--card);border-color:var(--accent)}
        .glow{box-shadow:0 0 40px rgba(59,130,246,0.15)}
        .code-box{background:#0d0d12;border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-family:monospace;position:relative}
        .copy-btn{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:var(--accent);color:#fff;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px}
    </style>
</head>
<body class="min-h-screen">
    <!-- 导航 -->
    <nav class="border-b border-gray-800 py-4 px-6">
        <div class="max-w-6xl mx-auto flex justify-between items-center">
            <div class="flex items-center gap-2">
                <span class="text-2xl">⚡</span>
                <span class="font-bold text-xl">{{SITE_NAME}}</span>
            </div>
            <div class="flex items-center gap-4">
                <a href="#api" class="text-gray-400 hover:text-white transition">API接入</a>
                <a href="#coupon" class="text-gray-400 hover:text-white transition">领券中心</a>
                <a href="{{NEW_API_URL}}" target="_blank" class="btn btn-primary text-sm">控制台 →</a>
            </div>
        </div>
    </nav>

    <!-- Hero -->
    <section class="py-20 px-6">
        <div class="max-w-4xl mx-auto text-center">
            <h1 class="text-4xl md:text-5xl font-bold mb-4 bg-gradient-to-r from-blue-400 to-cyan-400 bg-clip-text text-transparent">
                统一的大模型API网关
            </h1>
            <p class="text-xl text-gray-400 mb-8">更低的价格，更稳定的服务，只需替换API地址即可使用</p>
            <div class="code-box max-w-xl mx-auto text-left mb-8">
                <span class="text-gray-500">API地址:</span>
                <span class="text-blue-400 ml-2" id="api-url">{{NEW_API_URL}}/v1</span>
                <button class="copy-btn" onclick="copyText('{{NEW_API_URL}}/v1')">复制</button>
            </div>
            <div class="flex justify-center gap-4 flex-wrap">
                <a href="{{NEW_API_URL}}" target="_blank" class="btn btn-primary">🚀 获取API Key</a>
                <a href="/claim" class="btn btn-outline">🎫 领取兑换券</a>
            </div>
        </div>
    </section>

    <!-- API接入教程 -->
    <section id="api" class="py-16 px-6 border-t border-gray-800">
        <div class="max-w-4xl mx-auto">
            <h2 class="text-2xl font-bold mb-8 flex items-center gap-2">📖 API接入教程</h2>
            
            <div class="grid md:grid-cols-2 gap-6">
                <div class="card p-6">
                    <h3 class="font-semibold text-lg mb-4 text-blue-400">1️⃣ 获取API Key</h3>
                    <ol class="space-y-2 text-gray-400 text-sm">
                        <li>1. 访问 <a href="{{NEW_API_URL}}" target="_blank" class="text-blue-400 hover:underline">{{SITE_NAME}}控制台</a></li>
                        <li>2. 注册/登录账号</li>
                        <li>3. 进入「令牌管理」创建API Key</li>
                        <li>4. 复制生成的 sk-xxx 密钥</li>
                    </ol>
                </div>
                
                <div class="card p-6">
                    <h3 class="font-semibold text-lg mb-4 text-green-400">2️⃣ 配置API地址</h3>
                    <div class="space-y-3 text-sm">
                        <div class="code-box text-xs">
                            <div class="text-gray-500 mb-1"># API Base URL</div>
                            <div class="text-green-400">{{NEW_API_URL}}/v1</div>
                        </div>
                        <p class="text-gray-400">将此地址替换到你的应用中即可</p>
                    </div>
                </div>
                
                <div class="card p-6">
                    <h3 class="font-semibold text-lg mb-4 text-purple-400">3️⃣ ChatGPT-Next-Web</h3>
                    <ol class="space-y-2 text-gray-400 text-sm">
                        <li>1. 设置 → 自定义接口</li>
                        <li>2. 接口地址: <code class="text-purple-400">{{NEW_API_URL}}</code></li>
                        <li>3. API Key: 填入你的密钥</li>
                        <li>4. 保存即可使用</li>
                    </ol>
                </div>
                
                <div class="card p-6">
                    <h3 class="font-semibold text-lg mb-4 text-orange-400">4️⃣ Python调用示例</h3>
                    <div class="code-box text-xs overflow-x-auto">
                        <pre class="text-gray-300">from openai import OpenAI

client = OpenAI(
    api_key="sk-xxx",
    base_url="{{NEW_API_URL}}/v1"
)

resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role":"user","content":"Hi"}]
)</pre>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <!-- 兑换券入口 -->
    <section id="coupon" class="py-16 px-6 border-t border-gray-800">
        <div class="max-w-4xl mx-auto">
            <h2 class="text-2xl font-bold mb-8 flex items-center gap-2">🎫 兑换券领取</h2>
            
            <div class="card p-8 glow">
                <div class="flex flex-col md:flex-row items-center justify-between gap-6">
                    <div>
                        <h3 class="text-xl font-bold mb-2">免费领取API额度</h3>
                        <p class="text-gray-400 mb-2">每 {{COOLDOWN}} 小时可领取一次，随机获得 $1~$100 额度</p>
                        <div class="flex items-center gap-4 text-sm">
                            <span class="bg-green-900/50 text-green-400 px-3 py-1 rounded-full border border-green-700">
                                📦 当前可领: <b>{{AVAILABLE}}</b> 个
                            </span>
                            <span class="text-gray-500">🎰 大额低概率</span>
                        </div>
                    </div>
                    <a href="/claim" class="btn btn-primary text-lg px-8 py-3">
                        🎁 立即领取 →
                    </a>
                </div>
            </div>
        </div>
    </section>

    <!-- 使用须知 -->
    <section class="py-16 px-6 border-t border-gray-800">
        <div class="max-w-4xl mx-auto">
            <h2 class="text-2xl font-bold mb-8 flex items-center gap-2">📋 使用须知</h2>
            <div class="grid md:grid-cols-3 gap-6">
                <div class="card p-6">
                    <h3 class="font-semibold mb-3 text-blue-400">✅ 允许使用</h3>
                    <ul class="text-gray-400 text-sm space-y-1">
                        <li>• 个人学习研究</li>
                        <li>• 小型项目开发</li>
                        <li>• 合理频率调用</li>
                    </ul>
                </div>
                <div class="card p-6">
                    <h3 class="font-semibold mb-3 text-red-400">❌ 禁止行为</h3>
                    <ul class="text-gray-400 text-sm space-y-1">
                        <li>• 商业盈利用途</li>
                        <li>• 高频滥用接口</li>
                        <li>• 违法违规内容</li>
                    </ul>
                </div>
                <div class="card p-6">
                    <h3 class="font-semibold mb-3 text-yellow-400">⚠️ 注意事项</h3>
                    <ul class="text-gray-400 text-sm space-y-1">
                        <li>• 请勿分享API Key</li>
                        <li>• 违规将被封禁</li>
                        <li>• 额度用完需充值</li>
                    </ul>
                </div>
            </div>
        </div>
    </section>

    <!-- Footer -->
    <footer class="border-t border-gray-800 py-8 px-6 text-center text-gray-500 text-sm">
        <p>{{SITE_NAME}} © 2025 | <a href="{{NEW_API_URL}}" class="text-blue-400 hover:underline">控制台</a> | <a href="/claim" class="text-blue-400 hover:underline">领券中心</a></p>
    </footer>

    <script>
        function copyText(text) {
            navigator.clipboard.writeText(text);
            const btn = event.target;
            btn.textContent = '已复制';
            setTimeout(() => btn.textContent = '复制', 1500);
        }
    </script>
</body>
</html>'''

# ============ 兑换券领取页 ============
CLAIM_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>兑换券领取 - {{SITE_NAME}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        :root{--bg:#0a0a0f;--card:#13131a;--border:#1e1e2a;--accent:#3b82f6}
        body{background:var(--bg);color:#e0e0e0}
        .card{background:var(--card);border:1px solid var(--border);border-radius:16px}
        .input-dark{background:#0d0d12;border:1px solid var(--border);color:#e0e0e0;border-radius:8px;padding:12px 16px;width:100%}
        .input-dark:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 2px rgba(59,130,246,0.2)}
        .btn-primary{background:linear-gradient(135deg,#3b82f6,#1d4ed8);color:#fff;padding:12px 24px;border-radius:8px;font-weight:600;border:none;cursor:pointer;width:100%}
        .btn-primary:hover{background:linear-gradient(135deg,#2563eb,#1e40af)}
        .btn-primary:disabled{background:#374151;cursor:not-allowed}
        .btn-claim{background:linear-gradient(135deg,#10b981,#059669);color:#fff;padding:16px 32px;border-radius:12px;font-weight:700;font-size:18px;border:none;cursor:pointer}
        .btn-claim:hover{transform:scale(1.02)}
        .btn-claim:disabled{background:#374151;cursor:not-allowed;transform:none}
        .loading{display:inline-block;width:18px;height:18px;border:2px solid rgba(255,255,255,0.3);border-radius:50%;border-top-color:#fff;animation:spin 1s linear infinite}
        @keyframes spin{to{transform:rotate(360deg)}}
        .toast{position:fixed;top:20px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;color:#fff;font-weight:500;z-index:1000;animation:fadeIn .3s}
        @keyframes fadeIn{from{opacity:0;transform:translateX(-50%) translateY(-10px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
        .prize{animation:prize .5s ease-out}
        @keyframes prize{0%{transform:scale(0.5);opacity:0}50%{transform:scale(1.1)}100%{transform:scale(1);opacity:1}}
        .coupon{background:linear-gradient(135deg,#3b82f6,#1d4ed8);border-radius:8px;padding:12px;margin-bottom:8px}
    </style>
</head>
<body class="min-h-screen">
    <nav class="bg-gradient-to-r from-blue-600 to-blue-800 py-4 px-6">
        <div class="max-w-4xl mx-auto flex justify-between items-center">
            <a href="/" class="flex items-center gap-2 text-white">
                <span class="text-xl">🎫</span>
                <span class="font-bold">{{SITE_NAME}} 兑换中心</span>
            </a>
            <a href="{{NEW_API_URL}}" target="_blank" class="text-blue-200 hover:text-white text-sm">返回主站 →</a>
        </div>
    </nav>

    <main class="max-w-md mx-auto px-4 py-8">
        <!-- 登录区 -->
        <div id="login-section" class="card p-8">
            <div class="text-center mb-6">
                <div class="text-5xl mb-4">🎁</div>
                <h1 class="text-2xl font-bold">兑换券领取中心</h1>
                <p class="text-gray-400 mt-2">验证身份后领取免费额度</p>
                <div class="mt-4 inline-flex items-center bg-blue-900/30 text-blue-300 px-4 py-2 rounded-full border border-blue-800">
                    📦 当前可领: <span id="available-count" class="font-bold ml-1">{{AVAILABLE}}</span> 个
                </div>
                <p class="text-xs text-gray-500 mt-2">🎰 随机 $1~$100，大额低概率</p>
            </div>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">用户ID</label>
                    <input type="number" id="user-id" class="input-dark" placeholder="在个人设置页面查看">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">用户名</label>
                    <input type="text" id="username" class="input-dark" placeholder="登录用户名">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">API Key</label>
                    <input type="password" id="api-key" class="input-dark" placeholder="sk-xxx">
                    <p class="text-xs text-gray-500 mt-1">在 <a href="{{NEW_API_URL}}/console/token" target="_blank" class="text-blue-400">令牌管理</a> 创建</p>
                </div>
                <button id="verify-btn" class="btn-primary">验证身份</button>
            </div>
        </div>

        <!-- 领取区 -->
        <div id="claim-section" class="hidden">
            <div class="card p-4 mb-4">
                <div class="flex justify-between items-center">
                    <div>
                        <p class="text-gray-500 text-sm">当前用户</p>
                        <p id="user-info" class="font-semibold"></p>
                    </div>
                    <button id="logout-btn" class="text-blue-400 text-sm hover:underline">切换</button>
                </div>
            </div>

            <div class="card p-6 mb-4">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="font-semibold">领取状态</h2>
                    <span id="status-badge" class="px-3 py-1 rounded-full text-sm"></span>
                </div>
                <div id="quota-stats" class="flex flex-wrap gap-2 mb-4"></div>
                <div class="text-center py-4">
                    <button id="claim-btn" class="btn-claim">🎰 抽取兑换券</button>
                    <p id="cooldown-msg" class="text-gray-500 mt-3 text-sm"></p>
                </div>
                <div id="prize-display" class="hidden text-center py-4">
                    <div class="prize">
                        <div id="prize-amount" class="text-4xl font-bold text-green-400"></div>
                        <div id="prize-code" class="font-mono text-lg mt-2 bg-gray-800 p-3 rounded-lg border border-gray-700"></div>
                        <button id="copy-prize-btn" class="mt-2 text-blue-400 text-sm hover:underline">📋 复制兑换码</button>
                    </div>
                </div>
            </div>

            <div class="card p-6">
                <h2 class="font-semibold mb-3">📋 领取记录</h2>
                <div id="history"></div>
            </div>
        </div>
    </main>

    <footer class="text-center py-6 text-gray-600 text-sm">
        每 {{COOLDOWN}} 小时可领取一次 | <a href="/" class="text-blue-400">返回首页</a>
    </footer>

    <script>
        let userData = null;
        
        // 初始化
        document.addEventListener('DOMContentLoaded', function() {
            const saved = localStorage.getItem('coupon_user');
            if (saved) {
                try {
                    userData = JSON.parse(saved);
                    document.getElementById('user-id').value = userData.user_id || '';
                    document.getElementById('username').value = userData.username || '';
                    document.getElementById('api-key').value = userData.api_key || '';
                } catch(e) {}
            }
            
            // 绑定事件
            document.getElementById('verify-btn').addEventListener('click', verifyUser);
            document.getElementById('logout-btn').addEventListener('click', logout);
            document.getElementById('claim-btn').addEventListener('click', claimCoupon);
            document.getElementById('copy-prize-btn').addEventListener('click', copyPrize);
        });

        function showToast(msg, ok) {
            const t = document.createElement('div');
            t.className = 'toast ' + (ok ? 'bg-green-600' : 'bg-red-600');
            t.textContent = msg;
            document.body.appendChild(t);
            setTimeout(function() { t.remove(); }, 3000);
        }

        async function verifyUser() {
            const userId = document.getElementById('user-id').value.trim();
            const username = document.getElementById('username').value.trim();
            const apiKey = document.getElementById('api-key').value.trim();
            
            if (!userId || !username || !apiKey) {
                showToast('请填写完整信息', false);
                return;
            }

            const btn = document.getElementById('verify-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> 验证中...';

            try {
                const resp = await fetch('/api/verify', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_id: parseInt(userId), username: username, api_key: apiKey})
                });
                const data = await resp.json();
                
                if (resp.ok && data.success) {
                    userData = {user_id: parseInt(userId), username: username, api_key: apiKey};
                    localStorage.setItem('coupon_user', JSON.stringify(userData));
                    showLoggedIn();
                    loadStatus();
                    showToast('验证成功', true);
                } else {
                    showToast(data.detail || '验证失败', false);
                }
            } catch (e) {
                console.error(e);
                showToast('网络错误，请重试', false);
            }
            
            btn.disabled = false;
            btn.textContent = '验证身份';
        }

        function showLoggedIn() {
            document.getElementById('login-section').classList.add('hidden');
            document.getElementById('claim-section').classList.remove('hidden');
            document.getElementById('user-info').textContent = userData.username + ' (ID:' + userData.user_id + ')';
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
                if (data.success) {
                    updateUI(data.data);
                }
            } catch(e) {
                console.error(e);
            }
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

            // 额度统计
            const stats = document.getElementById('quota-stats');
            let statsHtml = '';
            for (const [k, v] of Object.entries(data.quota_stats || {})) {
                statsHtml += '<span class="bg-blue-900/30 text-blue-300 px-2 py-1 rounded text-sm border border-blue-800">' + k + ': ' + v + '个</span>';
            }
            stats.innerHTML = statsHtml;

            // 历史记录
            const history = document.getElementById('history');
            if (!data.history || data.history.length === 0) {
                history.innerHTML = '<p class="text-gray-500 text-center text-sm">暂无记录</p>';
            } else {
                let html = '';
                for (const r of data.history) {
                    html += '<div class="coupon text-white"><div class="flex justify-between"><span class="font-mono text-sm">' + r.coupon_code + '</span><span class="bg-white/20 px-2 rounded text-sm">$' + r.quota + '</span></div><div class="text-xs text-blue-200 mt-1">' + new Date(r.claim_time).toLocaleString('zh-CN') + '</div></div>';
                }
                history.innerHTML = html;
            }
        }

        async function claimCoupon() {
            const btn = document.getElementById('claim-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> 抽取中...';
            document.getElementById('prize-display').classList.add('hidden');

            try {
                const resp = await fetch('/api/claim', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(userData)
                });
                const data = await resp.json();
                
                if (resp.ok && data.success) {
                    document.getElementById('prize-amount').textContent = '🎉 $' + data.data.quota;
                    document.getElementById('prize-code').textContent = data.data.coupon_code;
                    document.getElementById('prize-display').classList.remove('hidden');
                    
                    try {
                        await navigator.clipboard.writeText(data.data.coupon_code);
                        showToast('恭喜！兑换码已复制', true);
                    } catch(e) {
                        showToast('恭喜获得 $' + data.data.quota, true);
                    }
                } else {
                    showToast(data.detail || '领取失败', false);
                }
            } catch (e) {
                console.error(e);
                showToast('网络错误', false);
            }

            btn.innerHTML = '🎰 抽取兑换券';
            loadStatus();
        }

        async function copyPrize() {
            const code = document.getElementById('prize-code').textContent;
            try {
                await navigator.clipboard.writeText(code);
                showToast('已复制', true);
            } catch(e) {
                showToast('复制失败', false);
            }
        }
    </script>
</body>
</html>'''

# ============ 管理后台 - 需要密码验证 ============
ADMIN_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台 - {{SITE_NAME}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        :root{--bg:#0a0a0f;--card:#13131a;--border:#1e1e2a;--accent:#3b82f6}
        body{background:var(--bg);color:#e0e0e0}
        .card{background:var(--card);border:1px solid var(--border);border-radius:12px}
        .input-dark{background:#0d0d12;border:1px solid var(--border);color:#e0e0e0;border-radius:8px;padding:12px 16px;width:100%}
        .input-dark:focus{border-color:var(--accent);outline:none}
        .btn{padding:12px 24px;border-radius:8px;font-weight:600;border:none;cursor:pointer;width:100%}
        .btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:#2563eb}
        .btn-green{background:#10b981;color:#fff}.btn-green:hover{background:#059669}
        .toast{position:fixed;top:20px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;color:#fff;z-index:1000}
    </style>
</head>
<body class="min-h-screen">
    <!-- 登录遮罩 -->
    <div id="login-overlay" class="fixed inset-0 bg-black/80 flex items-center justify-center z-50">
        <div class="card p-8 w-full max-w-sm mx-4">
            <div class="text-center mb-6">
                <div class="text-4xl mb-2">🔐</div>
                <h1 class="text-xl font-bold">管理后台</h1>
                <p class="text-gray-500 text-sm">请输入管理员密码</p>
            </div>
            <input type="password" id="login-pwd" class="input-dark mb-4" placeholder="管理员密码" onkeypress="if(event.key==='Enter')adminLogin()">
            <button onclick="adminLogin()" class="btn btn-primary">登录</button>
            <a href="/" class="block text-center text-gray-500 text-sm mt-4 hover:text-blue-400">← 返回首页</a>
        </div>
    </div>

    <!-- 管理界面 -->
    <div id="admin-content" class="hidden">
        <nav class="border-b border-gray-800 py-4 px-6">
            <div class="max-w-6xl mx-auto flex justify-between items-center">
                <h1 class="font-bold text-xl flex items-center gap-2">🔧 管理后台</h1>
                <div class="flex items-center gap-4">
                    <a href="/" class="text-gray-400 hover:text-white text-sm">← 返回首页</a>
                    <button onclick="adminLogout()" class="text-red-400 hover:text-red-300 text-sm">退出登录</button>
                </div>
            </div>
        </nav>

        <main class="max-w-6xl mx-auto px-4 py-8">
            <div class="grid lg:grid-cols-3 gap-6">
                <div class="lg:col-span-2 space-y-6">
                    <!-- 上传区 -->
                    <div class="card p-6">
                        <h2 class="font-semibold mb-4">📤 添加兑换码</h2>
                        <div class="grid grid-cols-5 gap-2 mb-4">
                            <button onclick="setQuota(1)" class="quota-btn bg-green-900/50 text-green-400 border border-green-700 py-2 rounded font-bold hover:bg-green-900">$1</button>
                            <button onclick="setQuota(5)" class="quota-btn bg-blue-900/50 text-blue-400 border border-blue-700 py-2 rounded font-bold hover:bg-blue-900">$5</button>
                            <button onclick="setQuota(10)" class="quota-btn bg-purple-900/50 text-purple-400 border border-purple-700 py-2 rounded font-bold hover:bg-purple-900">$10</button>
                            <button onclick="setQuota(50)" class="quota-btn bg-orange-900/50 text-orange-400 border border-orange-700 py-2 rounded font-bold hover:bg-orange-900">$50</button>
                            <button onclick="setQuota(100)" class="quota-btn bg-red-900/50 text-red-400 border border-red-700 py-2 rounded font-bold hover:bg-red-900">$100</button>
                        </div>
                        <div class="flex items-center gap-2 mb-4">
                            <span class="text-gray-400">当前额度:</span>
                            <input type="number" id="quota-input" value="1" class="w-20 input-dark text-center font-bold">
                            <span class="text-gray-400">美元</span>
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm text-gray-400 mb-2">上传TXT文件</label>
                            <input type="file" id="txt-file" accept=".txt" class="input-dark">
                        </div>
                        <button onclick="uploadTxt()" class="btn btn-primary mb-4">上传文件</button>
                        <hr class="border-gray-700 my-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-2">或手动粘贴（每行一个）</label>
                            <textarea id="coupons-input" rows="4" class="input-dark font-mono text-sm" placeholder="粘贴兑换码..."></textarea>
                        </div>
                        <button onclick="addCoupons()" class="btn btn-green mt-3">添加兑换码</button>
                    </div>

                    <!-- 概率说明 -->
                    <div class="card p-6">
                        <h2 class="font-semibold mb-4">🎰 概率说明</h2>
                        <div class="grid grid-cols-5 gap-2 text-center text-sm">
                            <div class="bg-green-900/30 p-3 rounded border border-green-800"><div class="text-green-400 font-bold">$1</div><div class="text-gray-500">50%</div></div>
                            <div class="bg-blue-900/30 p-3 rounded border border-blue-800"><div class="text-blue-400 font-bold">$5</div><div class="text-gray-500">30%</div></div>
                            <div class="bg-purple-900/30 p-3 rounded border border-purple-800"><div class="text-purple-400 font-bold">$10</div><div class="text-gray-500">15%</div></div>
                            <div class="bg-orange-900/30 p-3 rounded border border-orange-800"><div class="text-orange-400 font-bold">$50</div><div class="text-gray-500">4%</div></div>
                            <div class="bg-red-900/30 p-3 rounded border border-red-800"><div class="text-red-400 font-bold">$100</div><div class="text-gray-500">1%</div></div>
                        </div>
                    </div>
                </div>

                <div class="space-y-6">
                    <!-- 统计 -->
                    <div class="card p-6">
                        <div class="flex justify-between items-center mb-4">
                            <h2 class="font-semibold">📊 统计</h2>
                            <button onclick="loadStats()" class="text-blue-400 text-sm hover:underline">刷新</button>
                        </div>
                        <div id="stats">加载中...</div>
                    </div>

                    <!-- 最近领取 -->
                    <div class="card p-6">
                        <h2 class="font-semibold mb-4">📋 最近领取</h2>
                        <div id="recent-claims" class="max-h-80 overflow-y-auto space-y-2 text-sm"></div>
                    </div>
                </div>
            </div>
        </main>
    </div>

    <div id="toast" class="toast hidden"></div>

    <script>
        let adminPwd = '';

        document.addEventListener('DOMContentLoaded', function() {
            const saved = sessionStorage.getItem('admin_pwd');
            if (saved) {
                adminPwd = saved;
                checkLogin();
            }
        });

        function showToast(msg, ok) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'toast ' + (ok ? 'bg-green-600' : 'bg-red-600');
            setTimeout(function() { t.classList.add('hidden'); }, 3000);
        }

        async function adminLogin() {
            const pwd = document.getElementById('login-pwd').value;
            if (!pwd) { showToast('请输入密码', false); return; }

            try {
                const resp = await fetch('/api/admin/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password: pwd})
                });
                if (resp.ok) {
                    adminPwd = pwd;
                    sessionStorage.setItem('admin_pwd', pwd);
                    document.getElementById('login-overlay').classList.add('hidden');
                    document.getElementById('admin-content').classList.remove('hidden');
                    loadStats();
                } else {
                    showToast('密码错误', false);
                }
            } catch(e) {
                showToast('网络错误', false);
            }
        }

        async function checkLogin() {
            try {
                const resp = await fetch('/api/admin/stats?password=' + encodeURIComponent(adminPwd));
                if (resp.ok) {
                    document.getElementById('login-overlay').classList.add('hidden');
                    document.getElementById('admin-content').classList.remove('hidden');
                    loadStats();
                } else {
                    sessionStorage.removeItem('admin_pwd');
                    adminPwd = '';
                }
            } catch(e) {}
        }

        function adminLogout() {
            sessionStorage.removeItem('admin_pwd');
            adminPwd = '';
            location.reload();
        }

        function setQuota(q) {
            document.getElementById('quota-input').value = q;
            document.querySelectorAll('.quota-btn').forEach(function(b) { b.classList.remove('ring-2', 'ring-blue-500'); });
            event.target.classList.add('ring-2', 'ring-blue-500');
        }

        async function uploadTxt() {
            const quota = document.getElementById('quota-input').value;
            const file = document.getElementById('txt-file').files[0];
            if (!file) { showToast('请选择文件', false); return; }

            const formData = new FormData();
            formData.append('password', adminPwd);
            formData.append('quota', quota);
            formData.append('file', file);

            try {
                const resp = await fetch('/api/admin/upload-txt', { method: 'POST', body: formData });
                const data = await resp.json();
                showToast(data.message || data.detail, resp.ok);
                if (resp.ok) { loadStats(); document.getElementById('txt-file').value = ''; }
            } catch(e) { showToast('上传失败', false); }
        }

        async function addCoupons() {
            const quota = parseFloat(document.getElementById('quota-input').value);
            const text = document.getElementById('coupons-input').value;
            const coupons = text.split('\n').filter(function(s) { return s.trim(); });

            if (!coupons.length) { showToast('请输入兑换码', false); return; }

            try {
                const resp = await fetch('/api/admin/add-coupons', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password: adminPwd, quota: quota, coupons: coupons})
                });
                const data = await resp.json();
                showToast(data.message || data.detail, resp.ok);
                if (resp.ok) { loadStats(); document.getElementById('coupons-input').value = ''; }
            } catch(e) { showToast('添加失败', false); }
        }

        async function loadStats() {
            try {
                const resp = await fetch('/api/admin/stats?password=' + encodeURIComponent(adminPwd));
                const data = await resp.json();
                
                if (resp.ok && data.success) {
                    const d = data.data;
                    let html = '<div class="grid grid-cols-3 gap-2 text-center mb-4">' +
                        '<div class="bg-gray-800 p-3 rounded"><div class="text-xl font-bold">' + d.total + '</div><div class="text-xs text-gray-500">总数</div></div>' +
                        '<div class="bg-green-900/30 p-3 rounded border border-green-800"><div class="text-xl font-bold text-green-400">' + d.available + '</div><div class="text-xs text-gray-500">可用</div></div>' +
                        '<div class="bg-blue-900/30 p-3 rounded border border-blue-800"><div class="text-xl font-bold text-blue-400">' + d.claimed + '</div><div class="text-xs text-gray-500">已领</div></div>' +
                        '</div><div class="space-y-1">';
                    
                    for (const [k, v] of Object.entries(d.quota_stats || {})) {
                        html += '<div class="flex justify-between text-sm bg-gray-800/50 p-2 rounded"><span>' + k + '</span><span class="text-green-400">' + v.available + '</span><span class="text-gray-500">' + v.claimed + '</span></div>';
                    }
                    html += '</div>';
                    document.getElementById('stats').innerHTML = html;

                    let claimsHtml = '';
                    for (const r of d.recent_claims) {
                        claimsHtml += '<div class="bg-gray-800/50 p-2 rounded text-gray-400"><span class="text-blue-400">ID:' + r.user_id + '</span> ' + r.username + ' <span class="text-green-400">$' + r.quota + '</span> <span class="text-gray-600">' + r.time + '</span></div>';
                    }
                    document.getElementById('recent-claims').innerHTML = claimsHtml || '<p class="text-gray-600">暂无</p>';
                }
            } catch(e) { console.error(e); }
        }
    </script>
</body>
</html>'''

# ============ Widget ============
WIDGET_PAGE = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:transparent;font-family:system-ui,sans-serif}
.w{background:linear-gradient(135deg,#1e3a5f,#0f172a);border:1px solid #334155;border-radius:12px;padding:16px;color:#fff;max-width:280px}
.h{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.i{font-size:24px}.t{font-weight:600;font-size:14px}
.s{display:flex;justify-content:space-between;margin-bottom:12px;font-size:12px;color:#94a3b8}
.c{color:#60a5fa;font-weight:700;font-size:18px}
.b{display:block;width:100%;background:linear-gradient(135deg,#3b82f6,#1d4ed8);color:#fff;text-align:center;padding:10px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px}
.b:hover{background:linear-gradient(135deg,#2563eb,#1e40af)}
</style></head><body>
<div class="w"><div class="h"><span class="i">🎫</span><span class="t">兑换券领取中心</span></div>
<div class="s"><span>当前可领</span><span class="c">{{AVAILABLE}} 个</span></div>
<a href="{{COUPON_SITE_URL}}/claim" target="_blank" class="b">🎁 免费领取 →</a></div>
</body></html>'''

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

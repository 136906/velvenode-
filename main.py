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
        # 预设概率
        if quota in QUOTA_WEIGHTS:
            weight = QUOTA_WEIGHTS[quota]
        else:
            # 自定义额度：额度越大概率越低
            # 公式：weight = max(1, 100 / quota)
            weight = max(1, int(100 / quota))
        
        choices.append((quota, coupons))
        weights.append(weight)
    
    if not choices:
        return None
    
    selected = random.choices(choices, weights=weights, k=1)[0]
    return random.choice(selected[1])

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
            h = total_seconds // 3600
            m = (total_seconds % 3600) // 60
            s = total_seconds % 60
            cooldown_text = f"{h}小时 {m}分钟 {s}秒"
    
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    if available == 0:
        can_claim = False
        cooldown_text = "兑换码已领完，请等待补充"
    
    history = db.query(ClaimRecord).filter(ClaimRecord.user_id == user_id).order_by(ClaimRecord.claim_time.desc()).limit(10).all()
    
    return {
        "success": True,
        "data": {
            "can_claim": can_claim,
            "cooldown_text": cooldown_text,
            "available_count": available,
            "history": [
                {
                    "coupon_code": r.coupon_code,
                    "quota": r.quota_dollars,
                    "claim_time": r.claim_time.isoformat()
                } for r in history
            ]
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
            h = int(remaining.total_seconds()) // 3600
            m = (int(remaining.total_seconds()) % 3600) // 60
            raise HTTPException(status_code=400, detail=f"冷却中，请在 {h}小时 {m}分钟 后再试")
    
    coupon = get_random_coupon(db)
    if not coupon:
        raise HTTPException(status_code=400, detail="兑换码已领完")
    
    coupon.is_claimed = True
    coupon.claimed_by_user_id = user_id
    coupon.claimed_by_username = username
    coupon.claimed_at = now
    
    record = ClaimRecord(
        user_id=user_id,
        username=username,
        coupon_code=coupon.coupon_code,
        quota_dollars=coupon.quota_dollars,
        claim_time=now
    )
    db.add(record)
    db.commit()
    
    return {"success": True, "data": {"coupon_code": coupon.coupon_code, "quota": coupon.quota_dollars}}

# ============ 管理员 API ============
@app.post("/api/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    if body.get("password") != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    return {"success": True}

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
    return {"success": True, "message": f"成功添加 {added} 个兑换码，当前可用: {total} 个"}

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
    return {"success": True, "message": f"成功添加 {added} 个兑换码，当前可用: {total} 个"}

@app.get("/api/admin/stats")
async def get_stats(password: str, db: Session = Depends(get_db)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    total = db.query(CouponPool).count()
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    claimed = db.query(CouponPool).filter(CouponPool.is_claimed == True).count()
    
    # 获取所有不同额度
    from sqlalchemy import distinct
    all_quotas = db.query(distinct(CouponPool.quota_dollars)).all()
    all_quotas = sorted([q[0] for q in all_quotas])
    
    quota_stats = {}
    for q in all_quotas:
        avail = db.query(CouponPool).filter(CouponPool.is_claimed == False, CouponPool.quota_dollars == q).count()
        used = db.query(CouponPool).filter(CouponPool.is_claimed == True, CouponPool.quota_dollars == q).count()
        if avail > 0 or used > 0:
            quota_stats[f"${q}"] = {"available": avail, "claimed": used}
    
    recent = db.query(ClaimRecord).order_by(ClaimRecord.claim_time.desc()).limit(50).all()
    
    return {
        "success": True,
        "data": {
            "total": total,
            "available": available,
            "claimed": claimed,
            "quota_stats": quota_stats,
            "recent_claims": [
                {
                    "user_id": r.user_id,
                    "username": r.username,
                    "quota": r.quota_dollars,
                    "code": r.coupon_code[:8] + "...",
                    "time": r.claim_time.strftime("%m-%d %H:%M") if r.claim_time else ""
                } for r in recent
            ]
        }
    }

@app.get("/api/stats/public")
async def get_public_stats(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return {"available": available, "cooldown_hours": CLAIM_COOLDOWN_HOURS}

# ============ 页面路由 ============
@app.get("/", response_class=HTMLResponse)
async def index(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    html = HOME_PAGE
    html = html.replace("{{AVAILABLE}}", str(available))
    html = html.replace("{{SITE_NAME}}", SITE_NAME)
    html = html.replace("{{NEW_API_URL}}", NEW_API_URL)
    html = html.replace("{{COOLDOWN}}", str(CLAIM_COOLDOWN_HOURS))
    html = html.replace("{{COUPON_SITE_URL}}", COUPON_SITE_URL)
    return html

@app.get("/claim", response_class=HTMLResponse)
async def claim_page(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    html = CLAIM_PAGE
    html = html.replace("{{AVAILABLE}}", str(available))
    html = html.replace("{{SITE_NAME}}", SITE_NAME)
    html = html.replace("{{NEW_API_URL}}", NEW_API_URL)
    html = html.replace("{{COOLDOWN}}", str(CLAIM_COOLDOWN_HOURS))
    return html

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_PAGE.replace("{{SITE_NAME}}", SITE_NAME)

@app.get("/widget", response_class=HTMLResponse)
async def widget_page(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    html = WIDGET_PAGE
    html = html.replace("{{AVAILABLE}}", str(available))
    html = html.replace("{{COUPON_SITE_URL}}", COUPON_SITE_URL)
    return html

# ============ 首页 HTML ============
HOME_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{SITE_NAME}} - 统一的大模型API网关</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        :root{--bg:#0a0a0f;--card:#12121a;--border:#1f1f2e;--accent:#3b82f6}
        body{background:var(--bg);color:#e0e0e0;font-family:system-ui,sans-serif;padding-top:20px}
        .card{background:var(--card);border:1px solid var(--border);border-radius:12px}
        .btn{padding:10px 20px;border-radius:8px;font-weight:500;transition:all .2s;text-decoration:none;display:inline-flex;align-items:center;gap:6px;cursor:pointer;border:none}
        .btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:#2563eb}
        .btn-secondary{background:#1f1f2e;color:#e0e0e0;border:1px solid #2a2a3a}.btn-secondary:hover{background:#2a2a3a}
        .code-box{background:#0d0d12;border:1px solid var(--border);border-radius:8px;padding:14px 18px;font-family:ui-monospace,monospace}
        .glow{box-shadow:0 0 40px rgba(59,130,246,0.15)}
        .endpoint-container{height:24px;overflow:hidden;display:inline-block}
        .endpoint-slider{animation:slideEndpoints 12s infinite}
        .endpoint-item{height:24px;line-height:24px}
        @keyframes slideEndpoints{0%,16%{transform:translateY(0)}20%,36%{transform:translateY(-24px)}40%,56%{transform:translateY(-48px)}60%,76%{transform:translateY(-72px)}80%,96%{transform:translateY(-96px)}100%{transform:translateY(0)}}
    </style>
</head>
<body class="min-h-screen">
    <section class="py-16 px-6">
        <div class="max-w-3xl mx-auto text-center">
            <h1 class="text-4xl md:text-5xl font-bold mb-4 bg-gradient-to-r from-blue-400 to-cyan-400 bg-clip-text text-transparent">统一的大模型API网关</h1>
            <p class="text-lg text-gray-400 mb-10">更低的价格，更稳定的服务，只需替换API地址即可使用</p>
            
            <div class="code-box max-w-2xl mx-auto mb-8">
                <div class="flex items-center justify-between flex-wrap gap-4">
                    <div class="flex items-center gap-2 text-sm">
                        <span class="text-gray-500">API地址:</span>
                        <span class="text-blue-400">{{NEW_API_URL}}</span>
                        <div class="endpoint-container">
                            <div class="endpoint-slider">
                                <div class="endpoint-item text-cyan-400">/v1/chat/completions</div>
                                <div class="endpoint-item text-cyan-400">/v1/models</div>
                                <div class="endpoint-item text-cyan-400">/v1/embeddings</div>
                                <div class="endpoint-item text-cyan-400">/v1/images/generations</div>
                                <div class="endpoint-item text-cyan-400">/v1/audio/transcriptions</div>
                            </div>
                        </div>
                    </div>
                    <button onclick="copyAPI()" id="copy-btn" class="bg-blue-600 hover:bg-blue-700 text-white text-sm px-4 py-1.5 rounded transition">复制</button>
                </div>
            </div>
            
            <div class="flex justify-center gap-4 flex-wrap">
                <a href="{{NEW_API_URL}}/console/token" target="_blank" class="btn btn-primary text-base">🔑 获取API Key</a>
                <a href="/claim" target="_top" class="btn btn-secondary text-base">🎫 领取兑换券</a>
            </div>
        </div>
    </section>

    <section id="api" class="py-16 px-6 border-t border-gray-800">
        <div class="max-w-4xl mx-auto">
            <h2 class="text-2xl font-bold mb-8 flex items-center gap-2"><span>📖</span> API接入教程</h2>
            <div class="grid md:grid-cols-2 gap-6">
                <div class="card p-6">
                    <h3 class="font-semibold text-lg mb-4 text-blue-400">1️⃣ 获取API Key</h3>
                    <ol class="space-y-2 text-gray-400 text-sm">
                        <li>1. 访问 <a href="{{NEW_API_URL}}/console" target="_blank" class="text-blue-400 hover:underline">{{SITE_NAME}}控制台</a></li>
                        <li>2. 注册/登录账号</li>
                        <li>3. 进入「<a href="{{NEW_API_URL}}/console/token" target="_blank" class="text-blue-400 hover:underline">令牌管理</a>」创建API Key</li>
                        <li>4. 复制生成的 sk-xxx 密钥</li>
                    </ol>
                </div>
                <div class="card p-6">
                    <h3 class="font-semibold text-lg mb-4 text-green-400">2️⃣ 配置API地址</h3>
                    <div class="code-box text-sm mb-3">
                        <div class="text-gray-500"># API Base URL</div>
                        <div class="text-green-400">{{NEW_API_URL}}</div>
                    </div>
                    <p class="text-gray-400 text-sm">将此地址替换到你的应用中即可</p>
                </div>
                <div class="card p-6">
                    <h3 class="font-semibold text-lg mb-4 text-purple-400">3️⃣ ChatGPT-Next-Web</h3>
                    <ol class="space-y-2 text-gray-400 text-sm">
                        <li>1. 设置 → 自定义接口</li>
                        <li>2. 接口地址: <code class="text-purple-400 bg-purple-900/30 px-1 rounded">{{NEW_API_URL}}</code></li>
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

    <section id="coupon" class="py-16 px-6 border-t border-gray-800">
        <div class="max-w-4xl mx-auto">
            <h2 class="text-2xl font-bold mb-8 flex items-center gap-2"><span>🎫</span> 兑换券领取</h2>
            <div class="card p-8 glow">
                <div class="flex flex-col md:flex-row items-center justify-between gap-6">
                    <div>
                        <h3 class="text-xl font-bold mb-2">免费领取API额度</h3>
                        <p class="text-gray-400 mb-3">每 <span id="cd-hours">{{COOLDOWN}}</span> 小时可领取一次，随机获得对应额度的兑换码</p>
                        <span class="inline-block bg-green-900/40 text-green-400 px-4 py-1.5 rounded-full border border-green-800 text-sm">📦 当前可领: <b id="avail-cnt">{{AVAILABLE}}</b> 个</span>
                    </div>
                    <a href="/claim" target="_top" class="btn btn-primary text-lg px-8 py-3">🎁 立即领取 →</a>
                </div>
            </div>
        </div>
    </section>

    <section class="py-16 px-6 border-t border-gray-800">
        <div class="max-w-4xl mx-auto">
            <h2 class="text-2xl font-bold mb-8 flex items-center gap-2"><span>📋</span> 使用须知</h2>
            <div class="grid md:grid-cols-3 gap-6">
                <div class="card p-6">
                    <h3 class="font-semibold mb-3 text-blue-400">✅ 允许使用</h3>
                    <ul class="text-gray-400 text-sm space-y-1"><li>• 个人学习研究</li><li>• 小型项目开发</li><li>• 合理频率调用</li></ul>
                </div>
                <div class="card p-6">
                    <h3 class="font-semibold mb-3 text-red-400">❌ 禁止行为</h3>
                    <ul class="text-gray-400 text-sm space-y-1"><li>• 商业盈利用途</li><li>• 高频滥用接口</li><li>• 违法违规内容</li></ul>
                </div>
                <div class="card p-6">
                    <h3 class="font-semibold mb-3 text-yellow-400">⚠️ 注意事项</h3>
                    <ul class="text-gray-400 text-sm space-y-1"><li>• 请勿分享API Key</li><li>• 违规将被封禁</li><li>• 额度用完需充值</li></ul>
                </div>
            </div>
        </div>
    </section>

    <footer class="border-t border-gray-800 py-8 px-6 text-center text-gray-500 text-sm">
        <p>{{SITE_NAME}} © 2025 | <a href="{{NEW_API_URL}}/console" target="_blank" class="text-blue-400 hover:underline">控制台</a> | <a href="{{NEW_API_URL}}/pricing" target="_blank" class="text-blue-400 hover:underline">模型广场</a> | <a href="/claim" target="_top" class="text-blue-400 hover:underline">领券中心</a></p>
    </footer>

    <script>
        function copyAPI(){
            navigator.clipboard.writeText('{{NEW_API_URL}}');
            var btn=document.getElementById('copy-btn');
            btn.textContent='已复制';btn.classList.remove('bg-blue-600');btn.classList.add('bg-green-600');
            setTimeout(function(){btn.textContent='复制';btn.classList.remove('bg-green-600');btn.classList.add('bg-blue-600');},1500);
        }
        // 动态获取统计
        (function(){
            var xhr = new XMLHttpRequest();
            xhr.open('GET', '/api/stats/public', true);
            xhr.onreadystatechange = function(){
                if(xhr.readyState === 4 && xhr.status === 200){
                    try{
                        var d = JSON.parse(xhr.responseText);
                        document.getElementById('avail-cnt').textContent = d.available;
                        document.getElementById('cd-hours').textContent = d.cooldown_hours;
                    }catch(e){}
                }
            };
            xhr.send();
        })();
    </script>
</body>
</html>'''


CLAIM_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>兑换券领取 - {{SITE_NAME}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        :root{--bg:#0a0a0f;--card:#12121a;--border:#1f1f2e;--accent:#3b82f6}
        body{background:var(--bg);color:#e0e0e0;font-family:system-ui,sans-serif;padding-top:20px}
        .card{background:var(--card);border:1px solid var(--border);border-radius:16px}
        .ipt{background:#0d0d12;border:1px solid var(--border);color:#e0e0e0;border-radius:8px;padding:12px 16px;width:100%;font-size:14px}
        .ipt:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 2px rgba(59,130,246,0.2)}
        .btn-p{background:linear-gradient(135deg,#3b82f6,#1d4ed8);color:#fff;padding:14px;border-radius:8px;font-weight:600;border:none;cursor:pointer;width:100%;font-size:15px}
        .btn-p:hover{opacity:0.9}.btn-p:disabled{background:#374151;cursor:not-allowed}
        .btn-c{background:linear-gradient(135deg,#10b981,#059669);color:#fff;padding:16px 40px;border-radius:12px;font-weight:700;font-size:18px;border:none;cursor:pointer}
        .btn-c:hover{transform:scale(1.02)}.btn-c:disabled{background:#374151;cursor:not-allowed;transform:none}
        .ld{display:inline-block;width:18px;height:18px;border:2px solid rgba(255,255,255,0.3);border-radius:50%;border-top-color:#fff;animation:spin 1s linear infinite}
        @keyframes spin{to{transform:rotate(360deg)}}
        .toast{position:fixed;top:80px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;color:#fff;font-weight:500;z-index:9999;animation:fadeIn .3s}
        @keyframes fadeIn{from{opacity:0;transform:translateX(-50%) translateY(-10px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
        .prize{animation:pop .5s ease-out}
        @keyframes pop{0%{transform:scale(0.5);opacity:0}50%{transform:scale(1.1)}100%{transform:scale(1);opacity:1}}
        .cpn{background:linear-gradient(135deg,#3b82f6,#1d4ed8);border-radius:8px;padding:12px;margin-bottom:8px}
        .amount-big{font-size:48px;font-weight:800;background:linear-gradient(135deg,#fbbf24,#f59e0b);-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-shadow:0 0 30px rgba(251,191,36,0.5)}
    </style>
</head>
<body class="min-h-screen">
    <main class="max-w-md mx-auto px-4 py-8">
        <div id="sec-login" class="card p-8">
            <div class="text-center mb-6">
                <div class="text-5xl mb-4">🎁</div>
                <h1 class="text-2xl font-bold">兑换券领取中心</h1>
                <p class="text-gray-400 mt-2">验证身份后领取免费额度</p>
                <div class="mt-4 inline-flex items-center bg-blue-900/30 text-blue-300 px-4 py-2 rounded-full border border-blue-800">📦 当前可领: <span id="cnt" class="font-bold ml-1">{{AVAILABLE}}</span> 个</div>
            </div>
            <div class="space-y-4">
                <div><label class="block text-sm text-gray-400 mb-1">用户ID</label><input type="number" id="uid" class="ipt" placeholder="在个人设置页面查看"></div>
                <div><label class="block text-sm text-gray-400 mb-1">用户名</label><input type="text" id="uname" class="ipt" placeholder="登录用户名"></div>
                <div><label class="block text-sm text-gray-400 mb-1">API Key</label><input type="password" id="ukey" class="ipt" placeholder="sk-xxx"><p class="text-xs text-gray-500 mt-1">在 <a href="{{NEW_API_URL}}/console/token" target="_blank" class="text-blue-400">令牌管理</a> 创建</p></div>
                <button type="button" class="btn-p" onclick="doVerify()">验证身份</button>
            </div>
        </div>

        <div id="sec-claim" style="display:none">
            <div class="card p-4 mb-4">
                <div class="flex justify-between items-center">
                    <div><p class="text-gray-500 text-sm">当前用户</p><p id="uinfo" class="font-semibold"></p></div>
                    <button type="button" class="text-blue-400 text-sm hover:underline" onclick="doLogout()">切换账号</button>
                </div>
            </div>
            <div class="card p-6 mb-4">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="font-semibold">领取状态</h2>
                    <span id="badge" class="px-3 py-1 rounded-full text-sm"></span>
                </div>
                <div class="text-center py-4">
                    <button type="button" id="claimBtn" class="btn-c" onclick="doClaim()">🎰 抽取兑换券</button>
                    <p id="cdMsg" class="text-gray-500 mt-3 text-sm"></p>
                </div>
                <div id="prizeBox" style="display:none" class="text-center py-6">
                    <div class="prize">
                        <div class="text-gray-400 mb-2">🎉 恭喜获得</div>
                        <div id="prizeAmount" class="amount-big mb-4"></div>
                        <div class="text-gray-400 text-sm mb-2">兑换码:</div>
                        <div id="prizeCode" class="font-mono text-lg bg-gray-800 p-3 rounded-lg border border-gray-700 mb-3"></div>
                        <button type="button" class="text-blue-400 text-sm hover:underline" onclick="copyPrize()">📋 复制兑换码</button>
                    </div>
                </div>
            </div>
            <div class="card p-6">
                <h2 class="font-semibold mb-3">📋 领取记录</h2>
                <div id="hist"></div>
            </div>
        </div>
    </main>

    <footer class="text-center py-6 text-gray-600 text-sm">
        每 <span id="cd-hours">{{COOLDOWN}}</span> 小时可领取一次 | <a href="/" target="_top" class="text-blue-400 hover:underline">返回首页</a> | <a href="{{NEW_API_URL}}/console/topup" target="_blank" class="text-blue-400 hover:underline">钱包充值</a>
    </footer>

    <script>
    var ud = null;
    var cdHours = {{COOLDOWN}};

    (function(){
        var s = localStorage.getItem('coupon_user');
        if(s){ try{ ud = JSON.parse(s); document.getElementById('uid').value = ud.user_id||''; document.getElementById('uname').value = ud.username||''; document.getElementById('ukey').value = ud.api_key||''; }catch(e){} }
        // 获取动态配置
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/stats/public', true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4 && xhr.status === 200){
                try{
                    var d = JSON.parse(xhr.responseText);
                    document.getElementById('cnt').textContent = d.available;
                    document.getElementById('cd-hours').textContent = d.cooldown_hours;
                    cdHours = d.cooldown_hours;
                }catch(e){}
            }
        };
        xhr.send();
    })();

    function toast(msg, ok){
        var t = document.createElement('div');
        t.className = 'toast ' + (ok ? 'bg-green-600' : 'bg-red-600');
        t.textContent = msg;
        document.body.appendChild(t);
        setTimeout(function(){ t.remove(); }, 3000);
    }

    function doVerify(){
        var uid = document.getElementById('uid').value.trim();
        var uname = document.getElementById('uname').value.trim();
        var ukey = document.getElementById('ukey').value.trim();
        if(!uid || !uname || !ukey){ toast('请填写完整信息', false); return; }

        var btn = event.target;
        btn.disabled = true; btn.innerHTML = '<span class="ld"></span> 验证中...';

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/verify', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                btn.disabled = false; btn.textContent = '验证身份';
                if(xhr.status === 200){
                    try{
                        var res = JSON.parse(xhr.responseText);
                        if(res.success){
                            ud = {user_id: parseInt(uid), username: uname, api_key: ukey};
                            localStorage.setItem('coupon_user', JSON.stringify(ud));
                            showLogged(); loadStatus(); toast('验证成功', true);
                        }
                    }catch(e){ toast('解析错误', false); }
                } else {
                    try{ var res = JSON.parse(xhr.responseText); toast(res.detail || '验证失败', false); }catch(e){ toast('验证失败', false); }
                }
            }
        };
        xhr.onerror = function(){ btn.disabled = false; btn.textContent = '验证身份'; toast('网络错误', false); };
        xhr.send(JSON.stringify({user_id: parseInt(uid), username: uname, api_key: ukey}));
    }

    function showLogged(){
        document.getElementById('sec-login').style.display = 'none';
        document.getElementById('sec-claim').style.display = 'block';
        document.getElementById('uinfo').textContent = ud.username + ' (ID:' + ud.user_id + ')';
    }

    function doLogout(){
        localStorage.removeItem('coupon_user'); ud = null;
        document.getElementById('sec-login').style.display = 'block';
        document.getElementById('sec-claim').style.display = 'none';
    }

    function loadStatus(){
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/claim/status', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4 && xhr.status === 200){
                try{ var res = JSON.parse(xhr.responseText); if(res.success) updateUI(res.data); }catch(e){}
            }
        };
        xhr.send(JSON.stringify(ud));
    }

    function updateUI(d){
        document.getElementById('cnt').textContent = d.available_count;
        var btn = document.getElementById('claimBtn');
        var badge = document.getElementById('badge');
        var msg = document.getElementById('cdMsg');
        if(d.can_claim){
            btn.disabled = false;
            badge.textContent = '✅ 可领取'; badge.className = 'px-3 py-1 rounded-full text-sm bg-green-900/50 text-green-400 border border-green-700';
            msg.textContent = '';
        } else {
            btn.disabled = true;
            badge.textContent = '⏳ 冷却中'; badge.className = 'px-3 py-1 rounded-full text-sm bg-yellow-900/50 text-yellow-400 border border-yellow-700';
            msg.textContent = d.cooldown_text || '';
        }
        var h = document.getElementById('hist');
        if(!d.history || d.history.length === 0){ h.innerHTML = '<p class="text-gray-500 text-center text-sm">暂无记录</p>'; }
        else {
            var html = '';
            for(var i=0; i<d.history.length; i++){
                var r = d.history[i];
                html += '<div class="cpn text-white"><div class="flex justify-between"><span class="font-mono text-sm">'+r.coupon_code+'</span><span class="bg-white/20 px-2 py-0.5 rounded text-sm">$'+r.quota+'</span></div><div class="text-xs text-blue-200 mt-1">'+new Date(r.claim_time).toLocaleString('zh-CN')+'</div></div>';
            }
            h.innerHTML = html;
        }
    }

    function doClaim(){
        var btn = document.getElementById('claimBtn');
        btn.disabled = true; btn.innerHTML = '<span class="ld"></span> 抽取中...';
        document.getElementById('prizeBox').style.display = 'none';

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/claim', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                btn.innerHTML = '🎰 抽取兑换券';
                if(xhr.status === 200){
                    try{
                        var res = JSON.parse(xhr.responseText);
                        if(res.success){
                            var quota = res.data.quota;
                            document.getElementById('prizeAmount').textContent = '$' + quota;
                            document.getElementById('prizeCode').textContent = res.data.coupon_code;
                            document.getElementById('prizeBox').style.display = 'block';
                            try{ navigator.clipboard.writeText(res.data.coupon_code); toast('恭喜获得 $' + quota + '！兑换码已复制', true); }catch(e){ toast('恭喜获得 $' + quota + '！', true); }
                        }
                    }catch(e){ toast('解析错误', false); }
                } else {
                    try{ var res = JSON.parse(xhr.responseText); toast(res.detail || '领取失败', false); }catch(e){ toast('领取失败', false); }
                }
                loadStatus();
            }
        };
        xhr.onerror = function(){ btn.innerHTML = '🎰 抽取兑换券'; toast('网络错误', false); loadStatus(); };
        xhr.send(JSON.stringify(ud));
    }

    function copyPrize(){
        var code = document.getElementById('prizeCode').textContent;
        try{ navigator.clipboard.writeText(code); toast('已复制', true); }catch(e){ toast('复制失败', false); }
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
        body{background:#0a0a0f;color:#e0e0e0;font-family:system-ui,sans-serif}
        .card{background:#12121a;border:1px solid #1f1f2e;border-radius:12px}
        .ipt{background:#0d0d12;border:1px solid #1f1f2e;color:#e0e0e0;border-radius:8px;padding:12px 16px;width:100%}
        .ipt:focus{border-color:#3b82f6;outline:none}
        .btn{padding:12px 24px;border-radius:8px;font-weight:600;border:none;cursor:pointer;width:100%}
        .btn-blue{background:#3b82f6;color:#fff}
        .btn-blue:hover{background:#2563eb}
        .btn-green{background:#10b981;color:#fff}
        .btn-green:hover{background:#059669}
        #overlay{position:fixed;inset:0;background:rgba(0,0,0,0.95);display:flex;align-items:center;justify-content:center;z-index:100}
        #toast{position:fixed;top:80px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;color:#fff;z-index:200;display:none}
    </style>
</head>
<body class="min-h-screen">
    <div id="overlay">
        <div class="card p-8 w-full max-w-sm mx-4">
            <div class="text-center mb-6">
                <div class="text-4xl mb-2">🔐</div>
                <h1 class="text-xl font-bold">管理后台</h1>
                <p class="text-gray-500 text-sm">请输入管理员密码</p>
            </div>
            <input type="password" id="loginPwd" class="ipt mb-4" placeholder="管理员密码">
            <button type="button" class="btn btn-blue" onclick="doLogin()">登录</button>
            <a href="/" target="_top" class="block text-center text-gray-500 text-sm mt-4 hover:text-blue-400">← 返回首页</a>
            <p id="loginErr" class="text-red-500 text-center text-sm mt-2" style="display:none"></p>
        </div>
    </div>

    <div id="adminMain" style="display:none">
        <div class="border-b border-gray-800 py-4 px-6">
            <div class="max-w-6xl mx-auto flex justify-between items-center">
                <h1 class="font-bold text-xl">🔧 管理后台</h1>
                <div class="flex items-center gap-4">
                    <a href="/" target="_top" class="text-gray-400 hover:text-white text-sm">← 首页</a>
                    <button type="button" class="text-red-400 text-sm" onclick="doLogout()">退出</button>
                </div>
            </div>
        </div>

        <main class="max-w-6xl mx-auto px-4 py-8">
            <div class="grid lg:grid-cols-3 gap-6">
                <div class="lg:col-span-2 space-y-6">
                    <div class="card p-6">
                        <h2 class="font-semibold mb-4">📤 添加兑换码</h2>
                        <div class="grid grid-cols-5 gap-2 mb-4">
                            <button type="button" onclick="setQ(1)" class="bg-green-900/50 text-green-400 border border-green-700 py-2 rounded font-bold">$1</button>
                            <button type="button" onclick="setQ(5)" class="bg-blue-900/50 text-blue-400 border border-blue-700 py-2 rounded font-bold">$5</button>
                            <button type="button" onclick="setQ(10)" class="bg-purple-900/50 text-purple-400 border border-purple-700 py-2 rounded font-bold">$10</button>
                            <button type="button" onclick="setQ(50)" class="bg-orange-900/50 text-orange-400 border border-orange-700 py-2 rounded font-bold">$50</button>
                            <button type="button" onclick="setQ(100)" class="bg-red-900/50 text-red-400 border border-red-700 py-2 rounded font-bold">$100</button>
                        </div>
                        <div class="flex items-center gap-2 mb-4">
                            <span class="text-gray-400">额度:</span>
                            <input type="number" id="quotaVal" value="1" min="0.01" step="0.01" class="w-24 ipt text-center font-bold">
                            <span class="text-gray-400">美元（支持自定义）</span>
                        </div>
                        <div class="mb-4"><label class="block text-sm text-gray-400 mb-2">上传TXT文件</label><input type="file" id="txtFile" accept=".txt" class="ipt"></div>
                        <button type="button" class="btn btn-blue mb-4" onclick="doUpload()">上传文件</button>
                        <hr class="border-gray-700 my-4">
                        <div><label class="block text-sm text-gray-400 mb-2">或手动粘贴</label><textarea id="codesText" rows="4" class="ipt font-mono text-sm" placeholder="每行一个"></textarea></div>
                        <button type="button" class="btn btn-green mt-3" onclick="doAdd()">添加兑换码</button>
                    </div>
                    <div class="card p-6">
                        <h2 class="font-semibold mb-4">🎰 概率说明</h2>
                        <div class="grid grid-cols-5 gap-2 text-center text-sm mb-4">
                            <div class="bg-green-900/30 p-3 rounded border border-green-800"><div class="text-green-400 font-bold">$1</div><div class="text-gray-500">50%</div></div>
                            <div class="bg-blue-900/30 p-3 rounded border border-blue-800"><div class="text-blue-400 font-bold">$5</div><div class="text-gray-500">30%</div></div>
                            <div class="bg-purple-900/30 p-3 rounded border border-purple-800"><div class="text-purple-400 font-bold">$10</div><div class="text-gray-500">15%</div></div>
                            <div class="bg-orange-900/30 p-3 rounded border border-orange-800"><div class="text-orange-400 font-bold">$50</div><div class="text-gray-500">4%</div></div>
                            <div class="bg-red-900/30 p-3 rounded border border-red-800"><div class="text-red-400 font-bold">$100</div><div class="text-gray-500">1%</div></div>
                        </div>
                        <p class="text-gray-500 text-xs">自定义额度：额度越大概率越低（自动计算）</p>
                    </div>
                </div>
                <div class="space-y-6">
                    <div class="card p-6"><div class="flex justify-between items-center mb-4"><h2 class="font-semibold">📊 统计</h2><button type="button" class="text-blue-400 text-sm" onclick="loadStats()">刷新</button></div><div id="statsBox">加载中...</div></div>
                    <div class="card p-6"><h2 class="font-semibold mb-4">📋 最近领取</h2><div id="recentBox" class="max-h-80 overflow-y-auto space-y-2 text-sm"></div></div>
                </div>
            </div>
        </main>
    </div>
    <div id="toast"></div>

    <script>
    var pwd = '';
    (function(){
        var s = sessionStorage.getItem('admin_pwd');
        if(s){ pwd = s; verifyPwd(); }
        document.getElementById('loginPwd').onkeydown = function(e){ if(e.key==='Enter') doLogin(); };
    })();

    function toast(msg, ok){
        var t = document.getElementById('toast');
        t.textContent = msg; t.style.display = 'block'; t.style.background = ok ? '#10b981' : '#ef4444';
        setTimeout(function(){ t.style.display = 'none'; }, 3000);
    }

    function doLogin(){
        var p = document.getElementById('loginPwd').value;
        var err = document.getElementById('loginErr');
        if(!p){ err.textContent = '请输入密码'; err.style.display = 'block'; return; }
        err.style.display = 'none';

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/login', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                if(xhr.status === 200){
                    pwd = p; sessionStorage.setItem('admin_pwd', p);
                    document.getElementById('overlay').style.display = 'none';
                    document.getElementById('adminMain').style.display = 'block';
                    loadStats();
                } else {
                    err.textContent = '密码错误'; err.style.display = 'block';
                }
            }
        };
        xhr.onerror = function(){ err.textContent = '网络错误'; err.style.display = 'block'; };
        xhr.send(JSON.stringify({password: p}));
    }

    function verifyPwd(){
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/admin/stats?password=' + encodeURIComponent(pwd), true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                if(xhr.status === 200){
                    document.getElementById('overlay').style.display = 'none';
                    document.getElementById('adminMain').style.display = 'block';
                    loadStats();
                } else { sessionStorage.removeItem('admin_pwd'); pwd = ''; }
            }
        };
        xhr.send();
    }

    function doLogout(){ sessionStorage.removeItem('admin_pwd'); pwd = ''; location.reload(); }
    function setQ(q){ document.getElementById('quotaVal').value = q; }

    function doUpload(){
        var q = document.getElementById('quotaVal').value;
        var f = document.getElementById('txtFile').files[0];
        if(!f){ toast('请选择文件', false); return; }
        var fd = new FormData(); fd.append('password', pwd); fd.append('quota', q); fd.append('file', f);
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/upload-txt', true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{ var res = JSON.parse(xhr.responseText); toast(res.message || res.detail, xhr.status === 200); if(xhr.status === 200){ loadStats(); document.getElementById('txtFile').value = ''; } }catch(e){ toast('失败', false); }
            }
        };
        xhr.send(fd);
    }

    function doAdd(){
        var q = parseFloat(document.getElementById('quotaVal').value);
        var txt = document.getElementById('codesText').value;
        var arr = txt.split('\\n').filter(function(s){ return s.trim(); });
        if(!arr.length){ toast('请输入兑换码', false); return; }
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/add-coupons', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{ var res = JSON.parse(xhr.responseText); toast(res.message || res.detail, xhr.status === 200); if(xhr.status === 200){ loadStats(); document.getElementById('codesText').value = ''; } }catch(e){ toast('失败', false); }
            }
        };
        xhr.send(JSON.stringify({password: pwd, quota: q, coupons: arr}));
    }

    function loadStats(){
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/admin/stats?password=' + encodeURIComponent(pwd), true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4 && xhr.status === 200){
                try{
                    var res = JSON.parse(xhr.responseText);
                    if(res.success){
                        var d = res.data;
                        var h = '<div class="grid grid-cols-3 gap-2 text-center mb-4"><div class="bg-gray-800 p-3 rounded"><div class="text-xl font-bold">'+d.total+'</div><div class="text-xs text-gray-500">总数</div></div><div class="bg-green-900/30 p-3 rounded border border-green-800"><div class="text-xl font-bold text-green-400">'+d.available+'</div><div class="text-xs text-gray-500">可用</div></div><div class="bg-blue-900/30 p-3 rounded border border-blue-800"><div class="text-xl font-bold text-blue-400">'+d.claimed+'</div><div class="text-xs text-gray-500">已领</div></div></div><div class="space-y-1">';
                        for(var k in d.quota_stats){ var v = d.quota_stats[k]; h += '<div class="flex justify-between text-sm bg-gray-800/50 p-2 rounded"><span>'+k+'</span><span class="text-green-400">'+v.available+'</span><span class="text-gray-500">'+v.claimed+'</span></div>'; }
                        h += '</div>';
                        document.getElementById('statsBox').innerHTML = h;
                        var rh = '';
                        for(var i=0; i<d.recent_claims.length; i++){ var c = d.recent_claims[i]; rh += '<div class="bg-gray-800/50 p-2 rounded text-gray-400"><span class="text-blue-400">ID:'+c.user_id+'</span> '+c.username+' <span class="text-green-400">$'+c.quota+'</span> <span class="text-gray-600">'+c.time+'</span></div>'; }
                        document.getElementById('recentBox').innerHTML = rh || '<p class="text-gray-600">暂无</p>';
                    }
                }catch(e){}
            }
        };
        xhr.send();
    }
    </script>
</body>
</html>'''

# ============ 管理后台 HTML ============
ADMIN_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台 - {{SITE_NAME}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:#0a0a0f;color:#e0e0e0;font-family:system-ui,sans-serif}
        .card{background:#12121a;border:1px solid #1f1f2e;border-radius:12px}
        .ipt{background:#0d0d12;border:1px solid #1f1f2e;color:#e0e0e0;border-radius:8px;padding:12px 16px;width:100%}
        .ipt:focus{border-color:#3b82f6;outline:none}
        .btn{padding:12px 24px;border-radius:8px;font-weight:600;border:none;cursor:pointer;width:100%}
        .btn-blue{background:#3b82f6;color:#fff}
        .btn-blue:hover{background:#2563eb}
        .btn-green{background:#10b981;color:#fff}
        .btn-green:hover{background:#059669}
        #overlay{position:fixed;inset:0;background:rgba(0,0,0,0.95);display:flex;align-items:center;justify-content:center;z-index:100}
        #toast{position:fixed;top:20px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;color:#fff;z-index:200;display:none}
    </style>
</head>
<body class="min-h-screen">
    <!-- 登录遮罩 -->
    <div id="overlay">
        <div class="card p-8 w-full max-w-sm mx-4">
            <div class="text-center mb-6">
                <div class="text-4xl mb-2">🔐</div>
                <h1 class="text-xl font-bold">管理后台</h1>
                <p class="text-gray-500 text-sm">请输入管理员密码</p>
            </div>
            <input type="password" id="loginPwd" class="ipt mb-4" placeholder="管理员密码">
            <button type="button" class="btn btn-blue" onclick="doLogin()">登录</button>
            <a href="/" class="block text-center text-gray-500 text-sm mt-4 hover:text-blue-400">← 返回首页</a>
            <p id="loginError" class="text-red-500 text-center text-sm mt-2" style="display:none"></p>
        </div>
    </div>

    <!-- 管理界面 -->
    <div id="adminMain" style="display:none">
        <nav class="border-b border-gray-800 py-4 px-6">
            <div class="max-w-6xl mx-auto flex justify-between items-center">
                <h1 class="font-bold text-xl">🔧 管理后台</h1>
                <div class="flex items-center gap-4">
                    <a href="/" class="text-gray-400 hover:text-white text-sm">← 首页</a>
                    <button type="button" class="text-red-400 text-sm" onclick="doLogout()">退出</button>
                </div>
            </div>
        </nav>

        <main class="max-w-6xl mx-auto px-4 py-8">
            <div class="grid lg:grid-cols-3 gap-6">
                <div class="lg:col-span-2 space-y-6">
                    <div class="card p-6">
                        <h2 class="font-semibold mb-4">📤 添加兑换码</h2>
                        <div class="grid grid-cols-5 gap-2 mb-4">
                            <button type="button" onclick="setQuota(1)" class="bg-green-900/50 text-green-400 border border-green-700 py-2 rounded font-bold hover:opacity-80">$1</button>
                            <button type="button" onclick="setQuota(5)" class="bg-blue-900/50 text-blue-400 border border-blue-700 py-2 rounded font-bold hover:opacity-80">$5</button>
                            <button type="button" onclick="setQuota(10)" class="bg-purple-900/50 text-purple-400 border border-purple-700 py-2 rounded font-bold hover:opacity-80">$10</button>
                            <button type="button" onclick="setQuota(50)" class="bg-orange-900/50 text-orange-400 border border-orange-700 py-2 rounded font-bold hover:opacity-80">$50</button>
                            <button type="button" onclick="setQuota(100)" class="bg-red-900/50 text-red-400 border border-red-700 py-2 rounded font-bold hover:opacity-80">$100</button>
                        </div>
                        <div class="flex items-center gap-2 mb-4">
                            <span class="text-gray-400">额度:</span>
                            <input type="number" id="quotaVal" value="1" class="w-20 ipt text-center font-bold">
                            <span class="text-gray-400">美元</span>
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm text-gray-400 mb-2">上传TXT文件</label>
                            <input type="file" id="txtFile" accept=".txt" class="ipt">
                        </div>
                        <button type="button" class="btn btn-blue mb-4" onclick="doUpload()">上传文件</button>
                        <hr class="border-gray-700 my-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-2">或手动粘贴（每行一个）</label>
                            <textarea id="codesText" rows="4" class="ipt font-mono text-sm" placeholder="每行一个兑换码"></textarea>
                        </div>
                        <button type="button" class="btn btn-green mt-3" onclick="doAddCodes()">添加兑换码</button>
                    </div>

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
                    <div class="card p-6">
                        <div class="flex justify-between items-center mb-4">
                            <h2 class="font-semibold">📊 统计</h2>
                            <button type="button" class="text-blue-400 text-sm" onclick="loadStats()">刷新</button>
                        </div>
                        <div id="statsBox">加载中...</div>
                    </div>
                    <div class="card p-6">
                        <h2 class="font-semibold mb-4">📋 最近领取</h2>
                        <div id="recentBox" class="max-h-80 overflow-y-auto space-y-2 text-sm"></div>
                    </div>
                </div>
            </div>
        </main>
    </div>

    <div id="toast"></div>

    <script>
    var adminPwd = '';

    // 页面加载时检查session
    (function(){
        var saved = sessionStorage.getItem('admin_pwd');
        if(saved){
            adminPwd = saved;
            verifyAndShow();
        }
        // 回车登录
        document.getElementById('loginPwd').addEventListener('keydown', function(e){
            if(e.key === 'Enter') doLogin();
        });
    })();

    function toast(msg, ok){
        var t = document.getElementById('toast');
        t.textContent = msg;
        t.style.display = 'block';
        t.style.background = ok ? '#10b981' : '#ef4444';
        setTimeout(function(){ t.style.display = 'none'; }, 3000);
    }

    function doLogin(){
        var pwd = document.getElementById('loginPwd').value;
        var errEl = document.getElementById('loginError');
        
        if(!pwd){
            errEl.textContent = '请输入密码';
            errEl.style.display = 'block';
            return;
        }
        
        errEl.style.display = 'none';
        
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/login', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                if(xhr.status === 200){
                    adminPwd = pwd;
                    sessionStorage.setItem('admin_pwd', pwd);
                    document.getElementById('overlay').style.display = 'none';
                    document.getElementById('adminMain').style.display = 'block';
                    loadStats();
                } else {
                    errEl.textContent = '密码错误';
                    errEl.style.display = 'block';
                }
            }
        };
        xhr.onerror = function(){
            errEl.textContent = '网络错误';
            errEl.style.display = 'block';
        };
        xhr.send(JSON.stringify({password: pwd}));
    }

    function verifyAndShow(){
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/admin/stats?password=' + encodeURIComponent(adminPwd), true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                if(xhr.status === 200){
                    document.getElementById('overlay').style.display = 'none';
                    document.getElementById('adminMain').style.display = 'block';
                    loadStats();
                } else {
                    sessionStorage.removeItem('admin_pwd');
                    adminPwd = '';
                }
            }
        };
        xhr.send();
    }

    function doLogout(){
        sessionStorage.removeItem('admin_pwd');
        adminPwd = '';
        location.reload();
    }

    function setQuota(q){
        document.getElementById('quotaVal').value = q;
    }

    function doUpload(){
        var q = document.getElementById('quotaVal').value;
        var f = document.getElementById('txtFile').files[0];
        if(!f){ toast('请选择文件', false); return; }

        var fd = new FormData();
        fd.append('password', adminPwd);
        fd.append('quota', q);
        fd.append('file', f);

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/upload-txt', true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{
                    var res = JSON.parse(xhr.responseText);
                    toast(res.message || res.detail, xhr.status === 200);
                    if(xhr.status === 200){ loadStats(); document.getElementById('txtFile').value = ''; }
                }catch(e){ toast('请求失败', false); }
            }
        };
        xhr.send(fd);
    }

    function doAddCodes(){
        var q = parseFloat(document.getElementById('quotaVal').value);
        var txt = document.getElementById('codesText').value;
        var arr = txt.split('\\n').filter(function(s){ return s.trim(); });
        if(!arr.length){ toast('请输入兑换码', false); return; }

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/add-coupons', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{
                    var res = JSON.parse(xhr.responseText);
                    toast(res.message || res.detail, xhr.status === 200);
                    if(xhr.status === 200){ loadStats(); document.getElementById('codesText').value = ''; }
                }catch(e){ toast('请求失败', false); }
            }
        };
        xhr.send(JSON.stringify({password: adminPwd, quota: q, coupons: arr}));
    }

    function loadStats(){
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/admin/stats?password=' + encodeURIComponent(adminPwd), true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4 && xhr.status === 200){
                try{
                    var res = JSON.parse(xhr.responseText);
                    if(res.success){
                        var d = res.data;
                        var h = '<div class="grid grid-cols-3 gap-2 text-center mb-4">';
                        h += '<div class="bg-gray-800 p-3 rounded"><div class="text-xl font-bold">'+d.total+'</div><div class="text-xs text-gray-500">总数</div></div>';
                        h += '<div class="bg-green-900/30 p-3 rounded border border-green-800"><div class="text-xl font-bold text-green-400">'+d.available+'</div><div class="text-xs text-gray-500">可用</div></div>';
                        h += '<div class="bg-blue-900/30 p-3 rounded border border-blue-800"><div class="text-xl font-bold text-blue-400">'+d.claimed+'</div><div class="text-xs text-gray-500">已领</div></div>';
                        h += '</div><div class="space-y-1">';
                        for(var k in d.quota_stats){
                            var v = d.quota_stats[k];
                            h += '<div class="flex justify-between text-sm bg-gray-800/50 p-2 rounded"><span>'+k+'</span><span class="text-green-400">'+v.available+'</span><span class="text-gray-500">'+v.claimed+'</span></div>';
                        }
                        h += '</div>';
                        document.getElementById('statsBox').innerHTML = h;

                        var rh = '';
                        for(var i=0; i<d.recent_claims.length; i++){
                            var c = d.recent_claims[i];
                            rh += '<div class="bg-gray-800/50 p-2 rounded text-gray-400"><span class="text-blue-400">ID:'+c.user_id+'</span> '+c.username+' <span class="text-green-400">$'+c.quota+'</span> <span class="text-gray-600">'+c.time+'</span></div>';
                        }
                        document.getElementById('recentBox').innerHTML = rh || '<p class="text-gray-600">暂无</p>';
                    }
                }catch(e){ console.error(e); }
            }
        };
        xhr.send();
    }
    </script>
</body>
</html>'''

# ============ Widget HTML ============
WIDGET_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: transparent; font-family: system-ui, -apple-system, sans-serif; }
        .widget {
            background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%);
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 16px;
            color: #fff;
            max-width: 280px;
        }
        .widget-header {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 12px;
        }
        .widget-icon { font-size: 24px; }
        .widget-title { font-weight: 600; font-size: 14px; }
        .widget-stats {
            display: flex;
            justify-content: space-between;
            margin-bottom: 12px;
            font-size: 12px;
            color: #94a3b8;
        }
        .widget-count {
            color: #60a5fa;
            font-weight: 700;
            font-size: 18px;
        }
        .widget-btn {
            display: block;
            width: 100%;
            background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
            color: #fff;
            text-align: center;
            padding: 10px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            font-size: 14px;
            transition: all 0.2s;
        }
        .widget-btn:hover {
            opacity: 0.9;
            transform: translateY(-1px);
        }
    </style>
</head>
<body>
    <div class="widget">
        <div class="widget-header">
            <span class="widget-icon">🎫</span>
            <span class="widget-title">兑换券领取</span>
        </div>
        <div class="widget-stats">
            <span>当前可领</span>
            <span class="widget-count">{{AVAILABLE}} 个</span>
        </div>
        <a href="{{COUPON_SITE_URL}}/claim" target="_blank" class="widget-btn">
            🎁 免费领取 →
        </a>
    </div>
</body>
</html>'''

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

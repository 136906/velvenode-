from fastapi import FastAPI, HTTPException, Request, Depends, UploadFile, File, Form, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Float, Text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy import text
from datetime import datetime, timedelta, timezone
import httpx
import random
import os
import json
import hashlib
import secrets

# ============ 配置 ============
NEW_API_URL = os.getenv("NEW_API_URL", "https://velvenode.top")
COUPON_SITE_URL = os.getenv("COUPON_SITE_URL", "https://coupon.velvenode.top")
SITE_NAME = os.getenv("SITE_NAME", "velvenode")

ADMIN_ACCESS_TOKEN = os.getenv("ADMIN_ACCESS_TOKEN", "")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "1")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

TIMEZONE_OFFSET_HOURS = int(os.getenv("TIMEZONE_OFFSET_HOURS", "8"))
APP_TIMEZONE = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR}/coupon.db")

BIG_PRIZE_THRESHOLD = float(os.getenv("BIG_PRIZE_THRESHOLD", "50"))

DEFAULT_COOLDOWN_MINUTES = 480
DEFAULT_CLAIM_TIMES = 1
DEFAULT_QUOTA_WEIGHTS = {"1": 50, "5": 30, "10": 15, "50": 4, "100": 1}
DEFAULT_QUOTA_STOCK = {"1": 100, "5": 50, "10": 20, "50": 5, "100": 1}
DEFAULT_CLAIM_MODE = "B"
DEFAULT_QUOTA_RATE = 500000

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
    source = Column(String(32), default="manual")

class ClaimRecord(Base):
    __tablename__ = "claim_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, index=True, nullable=False)
    username = Column(String(255), nullable=False)
    coupon_code = Column(String(64), nullable=False)
    quota_dollars = Column(Float, default=1.0)
    claim_time = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    cooldown_expires_at = Column(DateTime, nullable=True)
    auto_redeemed = Column(Boolean, default=False)

class SystemConfig(Base):
    __tablename__ = "system_config"
    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(64), unique=True, nullable=False)
    config_value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class UserSession(Base):
    """用户会话表 - 用于存储已验证的用户"""
    __tablename__ = "user_sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_token = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, nullable=False)
    username = Column(String(255), nullable=False)
    main_site_session = Column(String(512), nullable=True)  # 存储主站 session 用于充值
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

def auto_migrate():
    with engine.connect() as conn:
        try:
            result = conn.execute(text("PRAGMA table_info(claim_records)"))
            columns = [row[1] for row in result]
            if 'cooldown_expires_at' not in columns:
                conn.execute(text("ALTER TABLE claim_records ADD COLUMN cooldown_expires_at DATETIME"))
                conn.commit()
            if 'auto_redeemed' not in columns:
                conn.execute(text("ALTER TABLE claim_records ADD COLUMN auto_redeemed BOOLEAN DEFAULT 0"))
                conn.commit()
            result2 = conn.execute(text("PRAGMA table_info(coupon_pool)"))
            columns2 = [row[1] for row in result2]
            if 'source' not in columns2:
                conn.execute(text("ALTER TABLE coupon_pool ADD COLUMN source VARCHAR(32) DEFAULT 'manual'"))
                conn.commit()
        except Exception as e:
            print(f"迁移检查: {e}")

auto_migrate()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def now_utc():
    return datetime.now(timezone.utc)

def ensure_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def format_local_time(dt):
    if dt is None:
        return ""
    dt_utc = ensure_utc(dt)
    dt_local = dt_utc.astimezone(APP_TIMEZONE)
    return dt_local.strftime("%Y-%m-%d %H:%M:%S")

def get_config(db: Session, key: str, default=None):
    config = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    return config.config_value if config else default

def set_config(db: Session, key: str, value: str):
    config = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if config:
        config.config_value = value
        config.updated_at = now_utc()
    else:
        config = SystemConfig(config_key=key, config_value=value)
        db.add(config)
    db.commit()

def get_cooldown_minutes(db): 
    val = get_config(db, "cooldown_minutes")
    return int(val) if val else DEFAULT_COOLDOWN_MINUTES

def get_claim_times(db): 
    val = get_config(db, "claim_times")
    return max(1, int(val)) if val else DEFAULT_CLAIM_TIMES

def get_quota_weights(db):
    val = get_config(db, "quota_weights")
    return json.loads(val) if val else DEFAULT_QUOTA_WEIGHTS.copy()

def get_quota_stock(db):
    val = get_config(db, "quota_stock")
    return json.loads(val) if val else DEFAULT_QUOTA_STOCK.copy()

def set_quota_stock(db, stock: dict):
    set_config(db, "quota_stock", json.dumps(stock))

def get_claim_mode(db):
    val = get_config(db, "claim_mode")
    return val if val in ["A", "B"] else DEFAULT_CLAIM_MODE

def get_quota_rate(db):
    val = get_config(db, "quota_rate")
    return int(val) if val else DEFAULT_QUOTA_RATE

def get_probability_mode(db):
    val = get_config(db, "probability_mode")
    return val if val in ["weight_only", "weight_stock"] else "weight_stock"

def init_default_config(db: Session):
    if not get_config(db, "cooldown_minutes"):
        set_config(db, "cooldown_minutes", str(DEFAULT_COOLDOWN_MINUTES))
    if not get_config(db, "claim_times"):
        set_config(db, "claim_times", str(DEFAULT_CLAIM_TIMES))
    if not get_config(db, "quota_weights"):
        set_config(db, "quota_weights", json.dumps(DEFAULT_QUOTA_WEIGHTS))
    if not get_config(db, "quota_stock"):
        set_config(db, "quota_stock", json.dumps(DEFAULT_QUOTA_STOCK))
    if not get_config(db, "claim_mode"):
        set_config(db, "claim_mode", DEFAULT_CLAIM_MODE)
    if not get_config(db, "quota_rate"):
        set_config(db, "quota_rate", str(DEFAULT_QUOTA_RATE))
    if not get_config(db, "probability_mode"):
        set_config(db, "probability_mode", "weight_stock")

with SessionLocal() as db:
    init_default_config(db)

app = FastAPI(title="兑换券系统")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ============ 会话管理 ============
def create_session(db: Session, user_id: int, username: str, main_session: str = None) -> str:
    """创建本站会话"""
    # 清理该用户的旧会话
    db.query(UserSession).filter(UserSession.user_id == user_id).delete()
    
    token = secrets.token_hex(32)
    expires = now_utc() + timedelta(days=7)
    
    session = UserSession(
        session_token=token,
        user_id=user_id,
        username=username,
        main_site_session=main_session,
        expires_at=expires
    )
    db.add(session)
    db.commit()
    return token

def get_session(db: Session, token: str) -> UserSession | None:
    """获取有效会话"""
    if not token:
        return None
    session = db.query(UserSession).filter(
        UserSession.session_token == token,
        UserSession.expires_at > now_utc()
    ).first()
    return session

def delete_session(db: Session, token: str):
    """删除会话"""
    db.query(UserSession).filter(UserSession.session_token == token).delete()
    db.commit()

# ============ 用户验证 ============
async def verify_user_by_main_session(session_cookie: str) -> dict | None:
    """通过主站的 session cookie 验证用户"""
    if not session_cookie:
        return None
    
    try:
        import base64
        from urllib.parse import unquote
        
        # 打印原始值用于调试
        print(f"[AUTH] 原始 session 长度: {len(session_cookie)}")
        print(f"[AUTH] 原始 session 前100字符: {session_cookie[:100]}")
        
        # 尝试 URL 解码
        session_cookie = unquote(session_cookie)
        print(f"[AUTH] URL解码后前100字符: {session_cookie[:100]}")
        
        # Gorilla session 格式: base64(timestamp)|base64(gob_data)|signature
        parts = session_cookie.split("|")
        print(f"[AUTH] 分割后 parts 数量: {len(parts)}")
        
        if len(parts) < 2:
            print("[AUTH] Session 格式错误，缺少分隔符")
            return None
        
        # 解码第二部分（gob 编码的数据）
        data_part = parts[1]
        decoded = None
        for suffix in ['', '=', '==', '===']:
            try:
                decoded = base64.urlsafe_b64decode(data_part + suffix)
                break
            except:
                continue
        
        if not decoded:
            print("[AUTH] Base64 解码失败")
            return None
        
        # 查找 id 字段
        idx = decoded.find(b'id')
        if idx == -1:
            print("[AUTH] 未找到 id 字段")
            return None
        
        # 在 id 后面查找 \x04\x02\x00 模式（gob int 编码标记）
        search_area = decoded[idx:idx+30]
        marker_idx = search_area.find(b'\x04\x02\x00')
        
        if marker_idx == -1:
            print("[AUTH] 未找到数值标记")
            return None
        
        # 标记后面的字节除以2就是真实 user_id
        raw_value = search_area[marker_idx + 3]
        user_id = raw_value // 2
        
        print(f"[AUTH] 解析成功 - 原始值: {raw_value}, user_id: {user_id}")
        
        if user_id <= 0:
            print("[AUTH] user_id 无效")
            return None
        
        # 用管理员令牌获取用户完整信息
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{NEW_API_URL}/api/user/{user_id}",
                headers={
                    "Authorization": f"Bearer {ADMIN_ACCESS_TOKEN}",
                    "New-Api-User": str(ADMIN_USER_ID)
                }
            )
            
            print(f"[AUTH] API 响应状态: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    user_data = data.get("data", {})
                    return {
                        "user_id": user_data.get("id"),
                        "username": user_data.get("username"),
                        "display_name": user_data.get("display_name", user_data.get("username"))
                    }
        
        return None
        
    except Exception as e:
        print(f"[AUTH] 异常: {e}")
        import traceback
        traceback.print_exc()
        return None

async def create_redemption_code_via_api(quota_dollars: float, db: Session) -> str | None:
    if not ADMIN_ACCESS_TOKEN:
        print("错误: ADMIN_ACCESS_TOKEN 未配置")
        return None
    quota_rate = get_quota_rate(db)
    quota = int(quota_dollars * quota_rate)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{NEW_API_URL}/api/redemption/",
                headers={
                    "Authorization": f"Bearer {ADMIN_ACCESS_TOKEN}",
                    "New-Api-User": ADMIN_USER_ID,
                    "Content-Type": "application/json"
                },
                json={
                    "name": f"抽奖${quota_dollars}",
                    "count": 1,
                    "quota": quota,
                    "expired_time": 0
                }
            )
            if response.status_code != 200:
                print(f"创建兑换码失败: HTTP {response.status_code} - {response.text}")
                return None
            data = response.json()
            if not data.get("success"):
                print(f"创建兑换码失败: {data}")
                return None
            codes = data.get("data", [])
            return codes[0] if codes else None
    except Exception as e:
        print(f"创建兑换码异常: {e}")
        return None

async def topup_user_by_session(main_session: str, redemption_code: str) -> bool:
    """使用主站 session 为用户充值"""
    if not main_session:
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{NEW_API_URL}/api/user/topup",
                cookies={"session": main_session},
                headers={"Content-Type": "application/json"},
                json={"key": redemption_code}
            )
            if response.status_code != 200:
                print(f"充值失败: HTTP {response.status_code}")
                return False
            result = response.json()
            if not result.get("success"):
                print(f"充值失败: {result}")
                return False
            return True
    except Exception as e:
        print(f"充值异常: {e}")
        return False

def get_stock_key(quota_stock: dict, quota: float) -> str:
    q_str = str(quota)
    if q_str in quota_stock:
        return q_str
    if quota == int(quota):
        int_str = str(int(quota))
        if int_str in quota_stock:
            return int_str
    return q_str

def get_total_available_stock(db: Session) -> int:
    claim_mode = get_claim_mode(db)
    quota_stock = get_quota_stock(db)
    
    if claim_mode == "A":
        local_count = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
        virtual_total = sum(max(0, int(v)) for v in quota_stock.values())
        return max(local_count, virtual_total)
    else:
        return sum(max(0, int(v)) for v in quota_stock.values())

def draw_random_quota(db: Session) -> float | None:
    claim_mode = get_claim_mode(db)
    probability_mode = get_probability_mode(db)
    quota_stock = get_quota_stock(db)
    quota_weights = get_quota_weights(db)
    
    available = []
    
    for q_str, weight in quota_weights.items():
        quota = float(q_str)
        stock_key = get_stock_key(quota_stock, quota)
        virtual_stock = int(quota_stock.get(stock_key, 0))
        
        if claim_mode == "A":
            local_count = db.query(CouponPool).filter(
                CouponPool.is_claimed == False,
                CouponPool.quota_dollars == quota
            ).count()
            effective_stock = max(local_count, virtual_stock)
        else:
            effective_stock = virtual_stock
        
        if effective_stock > 0:
            if probability_mode == "weight_only":
                actual_weight = float(weight)
            else:
                actual_weight = float(weight) * effective_stock
            
            available.append({
                "quota": quota,
                "actual_weight": actual_weight
            })
    
    if not available:
        return None
    
    quotas = [item["quota"] for item in available]
    weights = [item["actual_weight"] for item in available]
    
    return random.choices(quotas, weights=weights, k=1)[0]

def deduct_virtual_stock(db: Session, quota: float) -> bool:
    quota_stock = get_quota_stock(db)
    stock_key = get_stock_key(quota_stock, quota)
    
    current = int(quota_stock.get(stock_key, 0))
    if current > 0:
        quota_stock[stock_key] = current - 1
        set_quota_stock(db, quota_stock)
        return True
    return False

def get_local_coupon(db: Session, quota: float):
    return db.query(CouponPool).filter(
        CouponPool.is_claimed == False,
        CouponPool.quota_dollars == quota
    ).first()

def get_big_prizes(db: Session) -> list:
    quota_stock = get_quota_stock(db)
    quota_weights = get_quota_weights(db)
    claim_mode = get_claim_mode(db)
    
    big_prizes = []
    
    for q_str in quota_weights.keys():
        quota = float(q_str)
        if quota >= BIG_PRIZE_THRESHOLD:
            stock_key = get_stock_key(quota_stock, quota)
            virtual_stock = int(quota_stock.get(stock_key, 0))
            
            if claim_mode == "A":
                local_count = db.query(CouponPool).filter(
                    CouponPool.is_claimed == False,
                    CouponPool.quota_dollars == quota
                ).count()
                total = max(local_count, virtual_stock)
            else:
                total = virtual_stock
            
            if total > 0:
                big_prizes.append({"quota": quota, "count": total})
    
    big_prizes.sort(key=lambda x: x["quota"], reverse=True)
    return big_prizes

def format_cooldown(minutes: int) -> str:
    if minutes >= 60:
        h = minutes // 60
        m = minutes % 60
        return f"{h}小时{m}分钟" if m > 0 else f"{h}小时"
    return f"{minutes}分钟"

def calculate_user_cooldown_status(db: Session, user_id: int, now: datetime):
    cooldown_minutes = get_cooldown_minutes(db)
    claim_times = get_claim_times(db)
    max_lookback = now - timedelta(minutes=cooldown_minutes * 2)
    recent_claims = db.query(ClaimRecord).filter(
        ClaimRecord.user_id == user_id,
        ClaimRecord.claim_time >= max_lookback
    ).order_by(ClaimRecord.claim_time.desc()).all()
    active_claims = []
    for claim in recent_claims:
        claim_time = ensure_utc(claim.claim_time)
        config_expires = claim_time + timedelta(minutes=cooldown_minutes)
        stored_expires = ensure_utc(claim.cooldown_expires_at) if claim.cooldown_expires_at else None
        actual_expires = min(config_expires, stored_expires) if stored_expires else config_expires
        if now < actual_expires:
            active_claims.append({'claim': claim, 'expires_at': actual_expires})
    claims_in_period = len(active_claims)
    remaining_claims = max(0, claim_times - claims_in_period)
    can_claim = True
    cooldown_seconds = 0
    if claims_in_period >= claim_times and active_claims:
        earliest_expiry = min(c['expires_at'] for c in active_claims)
        if now < earliest_expiry:
            can_claim = False
            cooldown_seconds = int((earliest_expiry - now).total_seconds())
    return can_claim, remaining_claims, cooldown_seconds, recent_claims

# ============ 认证 API ============
@app.get("/api/auth/check")
async def check_auth(request: Request, db: Session = Depends(get_db)):
    """检查用户登录状态"""
    from fastapi.responses import JSONResponse
    
    main_session = request.cookies.get("session")
    local_token = request.cookies.get("coupon_session")
    
    # 先验证主站 session
    main_user = None
    if main_session:
        main_user = await verify_user_by_main_session(main_session)
    
    # 检查本站 session
    local_session = get_session(db, local_token) if local_token else None
    
    # 如果主站已登录
    if main_user:
        # 如果本站 session 不存在，或者用户变了，重新创建
        if not local_session or local_session.user_id != main_user["user_id"]:
            # 删除旧 session
            if local_token:
                delete_session(db, local_token)
            # 创建新 session
            token = create_session(db, main_user["user_id"], main_user["username"], main_session)
            response = JSONResponse(content={
                "success": True,
                "logged_in": True,
                "data": main_user
            })
            response.set_cookie(
                key="coupon_session",
                value=token,
                max_age=7*24*3600,
                httponly=True,
                samesite="lax",
                path="/"
            )
            return response
        else:
            # 用户没变，直接返回
            return {
                "success": True,
                "logged_in": True,
                "data": {
                    "user_id": local_session.user_id,
                    "username": local_session.username
                }
            }
    
    # 主站未登录，检查本站 session
    if local_session:
        return {
            "success": True,
            "logged_in": True,
            "data": {
                "user_id": local_session.user_id,
                "username": local_session.username
            }
        }
    
    return {"success": False, "logged_in": False, "message": "未登录"}

@app.get("/api/auth/login")
async def auth_login(request: Request):
    """重定向到主站登录"""
    # 记录来源页面
    redirect_url = f"{COUPON_SITE_URL}/api/auth/callback"
    # 跳转到主站控制台，用户登录后手动返回
    return RedirectResponse(
        url=f"{NEW_API_URL}/console?redirect={redirect_url}",
        status_code=302
    )

@app.get("/api/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    """认证回调 - 用户从主站返回后"""
    main_session = request.cookies.get("session")
    
    if main_session:
        user_info = await verify_user_by_main_session(main_session)
        if user_info:
            token = create_session(db, user_info["user_id"], user_info["username"], main_session)
            response = RedirectResponse(url="/claim", status_code=302)
            response.set_cookie(
                key="coupon_session",
                value=token,
                max_age=7*24*3600,
                httponly=True,
                samesite="lax"
            )
            return response
    
    # 认证失败，返回领取页
    return RedirectResponse(url="/claim?error=auth_failed", status_code=302)

@app.post("/api/auth/logout")
async def auth_logout(request: Request, db: Session = Depends(get_db)):
    """登出"""
    token = request.cookies.get("coupon_session")
    if token:
        delete_session(db, token)
    
    response = {"success": True}
    return response

# ============ 领取 API ============
@app.get("/api/claim/status")
async def get_claim_status(request: Request, db: Session = Depends(get_db)):
    """获取领取状态"""
    # 检查本站会话
    local_token = request.cookies.get("coupon_session")
    session = get_session(db, local_token) if local_token else None
    
    # 也尝试主站 session
    if not session:
        main_session = request.cookies.get("session")
        if main_session:
            user_info = await verify_user_by_main_session(main_session)
            if user_info:
                token = create_session(db, user_info["user_id"], user_info["username"], main_session)
                session = get_session(db, token)
    
    if not session:
        raise HTTPException(status_code=401, detail="请先登录")
    
    user_id = session.user_id
    username = session.username
    claim_times = get_claim_times(db)
    claim_mode = get_claim_mode(db)
    now = now_utc()
    can_claim, remaining_claims, cooldown_seconds, _ = calculate_user_cooldown_status(db, user_id, now)
    
    cooldown_text = None
    if not can_claim and cooldown_seconds > 0:
        h = cooldown_seconds // 3600
        m = (cooldown_seconds % 3600) // 60
        s = cooldown_seconds % 60
        cooldown_text = f"{h}小时 {m}分钟 {s}秒" if h > 0 else f"{m}分钟 {s}秒"
    
    total_stock = get_total_available_stock(db)
    if total_stock <= 0:
        can_claim = False
        cooldown_text = "兑换码已领完，请等待补充"
    
    big_prizes = get_big_prizes(db)
    
    history = db.query(ClaimRecord).filter(ClaimRecord.user_id == user_id).order_by(ClaimRecord.claim_time.desc()).limit(10).all()
    
    return {
        "success": True,
        "data": {
            "user_id": user_id,
            "username": username,
            "can_claim": can_claim,
            "cooldown_text": cooldown_text,
            "available_count": total_stock,
            "remaining_claims": remaining_claims,
            "claim_times": claim_times,
            "claim_mode": claim_mode,
            "big_prizes": big_prizes,
            "history": [
                {
                    "coupon_code": r.coupon_code,
                    "quota": r.quota_dollars,
                    "claim_time": r.claim_time.isoformat() if r.claim_time else "",
                    "auto_redeemed": getattr(r, 'auto_redeemed', False)
                } for r in history
            ]
        }
    }

@app.post("/api/claim")
async def claim_coupon(request: Request, db: Session = Depends(get_db)):
    """领取兑换券"""
    local_token = request.cookies.get("coupon_session")
    session = get_session(db, local_token) if local_token else None
    
    if not session:
        main_session = request.cookies.get("session")
        if main_session:
            user_info = await verify_user_by_main_session(main_session)
            if user_info:
                token = create_session(db, user_info["user_id"], user_info["username"], main_session)
                session = get_session(db, token)
    
    if not session:
        raise HTTPException(status_code=401, detail="请先登录")
    
    user_id = session.user_id
    username = session.username
    main_session = session.main_site_session
    cooldown_minutes = get_cooldown_minutes(db)
    now = now_utc()
    
    can_claim, remaining_claims, cooldown_seconds, _ = calculate_user_cooldown_status(db, user_id, now)
    
    if not can_claim:
        total_min = cooldown_seconds // 60
        if total_min >= 60:
            raise HTTPException(status_code=400, detail=f"冷却中，请在 {total_min//60}小时 {total_min%60}分钟 后再试")
        else:
            raise HTTPException(status_code=400, detail=f"冷却中，请在 {total_min}分钟 后再试")
    
    total_stock = get_total_available_stock(db)
    if total_stock <= 0:
        raise HTTPException(status_code=400, detail="兑换码已领完，请等待补充")
    
    claim_mode = get_claim_mode(db)
    quota = draw_random_quota(db)
    if quota is None:
        raise HTTPException(status_code=400, detail="没有可用的兑换码")
    
    coupon_code = None
    auto_redeemed = False
    
    if claim_mode == "A":
        local_coupon = get_local_coupon(db, quota)
        if local_coupon:
            coupon_code = local_coupon.coupon_code
            local_coupon.is_claimed = True
            local_coupon.claimed_by_user_id = user_id
            local_coupon.claimed_by_username = username
            local_coupon.claimed_at = now
        else:
            coupon_code = await create_redemption_code_via_api(quota, db)
            if coupon_code:
                deduct_virtual_stock(db, quota)
                new_coupon = CouponPool(
                    coupon_code=coupon_code,
                    quota_dollars=quota,
                    is_claimed=True,
                    claimed_by_user_id=user_id,
                    claimed_by_username=username,
                    claimed_at=now,
                    source="api"
                )
                db.add(new_coupon)
    else:
        coupon_code = await create_redemption_code_via_api(quota, db)
        if coupon_code:
            deduct_virtual_stock(db, quota)
            new_coupon = CouponPool(
                coupon_code=coupon_code,
                quota_dollars=quota,
                is_claimed=True,
                claimed_by_user_id=user_id,
                claimed_by_username=username,
                claimed_at=now,
                source="api"
            )
            db.add(new_coupon)
            
            # B模式自动充值
            if main_session and await topup_user_by_session(main_session, coupon_code):
                auto_redeemed = True
    
    if not coupon_code:
        raise HTTPException(status_code=500, detail="兑换码生成失败，请稍后重试")
    
    cooldown_expires = now + timedelta(minutes=cooldown_minutes)
    record = ClaimRecord(
        user_id=user_id,
        username=username,
        coupon_code=coupon_code,
        quota_dollars=quota,
        claim_time=now,
        cooldown_expires_at=cooldown_expires,
        auto_redeemed=auto_redeemed
    )
    db.add(record)
    db.commit()
    
    return {
        "success": True,
        "data": {
            "coupon_code": coupon_code,
            "quota": quota,
            "remaining_claims": remaining_claims - 1,
            "auto_redeemed": auto_redeemed,
            "claim_mode": claim_mode
        }
    }

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
    if body.get("password") != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    coupons = body.get("coupons", [])
    quota = float(body.get("quota", 1))
    added = 0
    for code in coupons:
        code = code.strip()
        if code and not db.query(CouponPool).filter(CouponPool.coupon_code == code).first():
            db.add(CouponPool(coupon_code=code, quota_dollars=quota, source="manual"))
            added += 1
    db.commit()
    total = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return {"success": True, "message": f"成功添加 {added} 个兑换码，本地可用: {total} 个"}

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
            db.add(CouponPool(coupon_code=code, quota_dollars=quota, source="manual"))
            added += 1
    db.commit()
    total = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    return {"success": True, "message": f"成功添加 {added} 个兑换码，本地可用: {total} 个"}

@app.get("/api/admin/coupons")
async def get_coupons(password: str, page: int = 1, per_page: int = 20, status: str = "all", search: str = "", db: Session = Depends(get_db)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    query = db.query(CouponPool)
    if status == "available":
        query = query.filter(CouponPool.is_claimed == False)
    elif status == "claimed":
        query = query.filter(CouponPool.is_claimed == True)
    if search:
        query = query.filter(CouponPool.coupon_code.contains(search))
    total = query.count()
    coupons = query.order_by(CouponPool.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return {
        "success": True,
        "data": {
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
            "coupons": [
                {
                    "id": c.id,
                    "code": c.coupon_code,
                    "quota": c.quota_dollars,
                    "is_claimed": c.is_claimed,
                    "claimed_by": c.claimed_by_username,
                    "claimed_at": format_local_time(c.claimed_at) if c.claimed_at else None,
                    "created_at": format_local_time(c.created_at) if c.created_at else None,
                    "source": getattr(c, 'source', 'manual')
                } for c in coupons
            ]
        }
    }

@app.post("/api/admin/delete-coupon")
async def delete_coupon(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    if body.get("password") != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    coupon = db.query(CouponPool).filter(CouponPool.id == body.get("id")).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="兑换码不存在")
    db.delete(coupon)
    db.commit()
    return {"success": True, "message": "删除成功"}

@app.post("/api/admin/delete-coupons-batch")
async def delete_coupons_batch(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    if body.get("password") != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    delete_type = body.get("type", "selected")
    ids = body.get("ids", [])
    if delete_type == "selected":
        deleted = db.query(CouponPool).filter(CouponPool.id.in_(ids)).delete(synchronize_session=False)
    elif delete_type == "all_available":
        deleted = db.query(CouponPool).filter(CouponPool.is_claimed == False).delete(synchronize_session=False)
    elif delete_type == "all_claimed":
        deleted = db.query(CouponPool).filter(CouponPool.is_claimed == True).delete(synchronize_session=False)
    elif delete_type == "all":
        deleted = db.query(CouponPool).delete(synchronize_session=False)
    else:
        deleted = 0
    db.commit()
    return {"success": True, "message": f"成功删除 {deleted} 个兑换码"}

@app.get("/api/admin/stats")
async def get_stats(password: str, db: Session = Depends(get_db)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    total = db.query(CouponPool).count()
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    claimed = db.query(CouponPool).filter(CouponPool.is_claimed == True).count()
    
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
    
    quota_stock = get_quota_stock(db)
    quota_weights = get_quota_weights(db)
    total_virtual_stock = get_total_available_stock(db)
    probability_mode = get_probability_mode(db)
    
    probability_info = []
    total_weighted = 0
    
    for q_str, weight in quota_weights.items():
        stock_key = get_stock_key(quota_stock, float(q_str))
        stock = int(quota_stock.get(stock_key, 0))
        if stock > 0:
            if probability_mode == "weight_only":
                weighted = float(weight)
            else:
                weighted = float(weight) * stock
            total_weighted += weighted
            probability_info.append({
                "quota": q_str, 
                "weight": weight, 
                "stock": stock, 
                "weighted": weighted
            })
    
    for item in probability_info:
        item["probability"] = round(item["weighted"] / total_weighted * 100, 2) if total_weighted > 0 else 0
    
    return {
        "success": True,
        "data": {
            "total": total,
            "available": available,
            "claimed": claimed,
            "total_virtual_stock": total_virtual_stock,
            "quota_stats": quota_stats,
            "cooldown_minutes": get_cooldown_minutes(db),
            "claim_times": get_claim_times(db),
            "quota_weights": quota_weights,
            "quota_stock": quota_stock,
            "probability_info": probability_info,
            "probability_mode": probability_mode,
            "claim_mode": get_claim_mode(db),
            "quota_rate": get_quota_rate(db),
            "timezone_offset": TIMEZONE_OFFSET_HOURS,
            "admin_token_configured": bool(ADMIN_ACCESS_TOKEN),
            "big_prize_threshold": BIG_PRIZE_THRESHOLD,
            "recent_claims": [
                {
                    "user_id": r.user_id,
                    "username": r.username,
                    "quota": r.quota_dollars,
                    "code": r.coupon_code[:8] + "...",
                    "time": format_local_time(r.claim_time),
                    "auto_redeemed": getattr(r, 'auto_redeemed', False)
                } for r in recent
            ]
        }
    }

@app.post("/api/admin/update-config")
async def update_config(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    if body.get("password") != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    updated = []
    
    if "cooldown_minutes" in body:
        set_config(db, "cooldown_minutes", str(int(body["cooldown_minutes"])))
        updated.append("冷却时间")
    if "claim_times" in body:
        set_config(db, "claim_times", str(int(body["claim_times"])))
        updated.append("领取次数")
    if "quota_weights" in body:
        if isinstance(body["quota_weights"], dict):
            set_config(db, "quota_weights", json.dumps(body["quota_weights"]))
            updated.append("概率权重")
    if "quota_stock" in body:
        if isinstance(body["quota_stock"], dict):
            set_config(db, "quota_stock", json.dumps(body["quota_stock"]))
            updated.append("虚拟库存")
    if "claim_mode" in body:
        if body["claim_mode"] in ["A", "B"]:
            set_config(db, "claim_mode", body["claim_mode"])
            updated.append(f"领取模式({body['claim_mode']})")
    if "probability_mode" in body:
        if body["probability_mode"] in ["weight_only", "weight_stock"]:
            set_config(db, "probability_mode", body["probability_mode"])
            updated.append(f"概率模式({body['probability_mode']})")
    if "quota_rate" in body:
        set_config(db, "quota_rate", str(int(body["quota_rate"])))
        updated.append("额度比例")
    
    return {"success": True, "message": f"已更新: {', '.join(updated)}" if updated else "无更新"}

@app.get("/api/stats/public")
async def get_public_stats(db: Session = Depends(get_db)):
    total_stock = get_total_available_stock(db)
    big_prizes = get_big_prizes(db)
    return {
        "available": total_stock,
        "cooldown_minutes": get_cooldown_minutes(db),
        "cooldown_text": format_cooldown(get_cooldown_minutes(db)),
        "claim_times": get_claim_times(db),
        "claim_mode": get_claim_mode(db),
        "probability_mode": get_probability_mode(db),
        "big_prizes": big_prizes
    }

# ============ 页面路由 ============
@app.get("/", response_class=HTMLResponse)
async def index(db: Session = Depends(get_db)):
    html = HOME_PAGE
    total_stock = get_total_available_stock(db)
    html = html.replace("{{AVAILABLE}}", str(total_stock))
    html = html.replace("{{SITE_NAME}}", SITE_NAME)
    html = html.replace("{{NEW_API_URL}}", NEW_API_URL)
    html = html.replace("{{COOLDOWN_TEXT}}", format_cooldown(get_cooldown_minutes(db)))
    html = html.replace("{{CLAIM_TIMES}}", str(get_claim_times(db)))
    html = html.replace("{{COUPON_SITE_URL}}", COUPON_SITE_URL)
    return html

@app.get("/claim", response_class=HTMLResponse)
async def claim_page(request: Request, db: Session = Depends(get_db)):
    html = CLAIM_PAGE
    total_stock = get_total_available_stock(db)
    html = html.replace("{{AVAILABLE}}", str(total_stock))
    html = html.replace("{{SITE_NAME}}", SITE_NAME)
    html = html.replace("{{NEW_API_URL}}", NEW_API_URL)
    html = html.replace("{{COOLDOWN_TEXT}}", format_cooldown(get_cooldown_minutes(db)))
    html = html.replace("{{CLAIM_TIMES}}", str(get_claim_times(db)))
    return html

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_PAGE.replace("{{SITE_NAME}}", SITE_NAME)

# ============ HTML 页面 ============
HOME_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{SITE_NAME}} - 统一的大模型API网关</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        :root{--bg:#0a0a0f;--card:#12121a;--border:#1f1f2e;--accent:#3b82f6}
        body{background:var(--bg);color:#e0e0e0;font-family:system-ui,sans-serif}
        .card{background:var(--card);border:1px solid var(--border);border-radius:12px}
        .btn{padding:10px 20px;border-radius:8px;font-weight:500;transition:all .2s;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;cursor:pointer;border:none;font-size:14px}
        .btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:#2563eb}
        .btn-secondary{background:#1f1f2e;color:#e0e0e0;border:1px solid #2a2a3a}.btn-secondary:hover{background:#2a2a3a}
        .btn-console{background:linear-gradient(135deg,#8b5cf6,#6d28d9);color:#fff}.btn-console:hover{opacity:0.9}
        .code-box{background:#0d0d12;border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-family:ui-monospace,monospace}
        .glow{box-shadow:0 0 40px rgba(59,130,246,0.15)}
    </style>
</head>
<body class="min-h-screen">
    <nav class="border-b border-gray-800 px-4 py-3 sticky top-0 bg-[#0a0a0f]/95 backdrop-blur z-50">
        <div class="max-w-5xl mx-auto flex justify-between items-center">
            <a href="/" class="text-lg font-bold text-white">{{SITE_NAME}}</a>
            <div class="flex items-center gap-2">
                <a href="{{NEW_API_URL}}/console" target="_blank" class="text-gray-400 hover:text-white text-sm px-3 py-1.5">控制台</a>
                <a href="/claim" class="btn btn-primary text-sm px-4 py-1.5">🎫 领券</a>
            </div>
        </div>
    </nav>

    <section class="py-12 md:py-20 px-4">
        <div class="max-w-3xl mx-auto text-center">
            <h1 class="text-3xl md:text-5xl font-bold mb-4 bg-gradient-to-r from-blue-400 to-cyan-400 bg-clip-text text-transparent leading-tight">统一的大模型API网关</h1>
            <p class="text-base md:text-lg text-gray-400 mb-8">更低的价格，更稳定的服务，只需替换API地址即可使用</p>
            
            <div class="code-box max-w-xl mx-auto mb-8">
                <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
                    <div class="flex items-center gap-2 text-sm overflow-hidden">
                        <span class="text-gray-500 shrink-0">API:</span>
                        <span class="text-blue-400 truncate">{{NEW_API_URL}}</span>
                    </div>
                    <button onclick="copyAPI()" id="copy-btn" class="bg-blue-600 hover:bg-blue-700 text-white text-sm px-4 py-1.5 rounded transition shrink-0">复制</button>
                </div>
            </div>
            
            <div class="flex flex-wrap justify-center gap-3">
                <a href="{{NEW_API_URL}}/console/token" target="_blank" class="btn btn-primary">🔑 获取API Key</a>
                <a href="{{NEW_API_URL}}/console" target="_blank" class="btn btn-console">🖥️ 控制台</a>
                <a href="/claim" class="btn btn-secondary">🎫 领取兑换券</a>
            </div>
        </div>
    </section>

    <section class="py-12 px-4 border-t border-gray-800">
        <div class="max-w-4xl mx-auto">
            <h2 class="text-xl md:text-2xl font-bold mb-6 flex items-center gap-2"><span>📖</span> 快速接入</h2>
            <div class="grid md:grid-cols-2 gap-4">
                <div class="card p-5">
                    <h3 class="font-semibold mb-3 text-blue-400">1️⃣ 获取API Key</h3>
                    <ol class="space-y-1.5 text-gray-400 text-sm">
                        <li>1. 访问 <a href="{{NEW_API_URL}}/console" target="_blank" class="text-blue-400 hover:underline">控制台</a> 注册登录</li>
                        <li>2. 进入「令牌管理」创建密钥</li>
                        <li>3. 复制生成的 sk-xxx</li>
                    </ol>
                </div>
                <div class="card p-5">
                    <h3 class="font-semibold mb-3 text-green-400">2️⃣ 配置使用</h3>
                    <div class="code-box text-sm mb-2 py-2">
                        <div class="text-gray-500 text-xs"># Base URL</div>
                        <div class="text-green-400 truncate">{{NEW_API_URL}}</div>
                    </div>
                    <p class="text-gray-400 text-sm">替换到你的应用中即可</p>
                </div>
                <div class="card p-5">
                    <h3 class="font-semibold mb-3 text-purple-400">3️⃣ ChatGPT-Next-Web</h3>
                    <ol class="space-y-1.5 text-gray-400 text-sm">
                        <li>设置 → 自定义接口</li>
                        <li>接口地址: <code class="text-purple-400 bg-purple-900/30 px-1 rounded text-xs">{{NEW_API_URL}}</code></li>
                        <li>填入API Key保存即可</li>
                    </ol>
                </div>
                <div class="card p-5">
                    <h3 class="font-semibold mb-3 text-orange-400">4️⃣ Python示例</h3>
                    <div class="code-box text-xs overflow-x-auto py-2">
                        <pre class="text-gray-300">from openai import OpenAI
client = OpenAI(
    api_key="sk-xxx",
    base_url="{{NEW_API_URL}}/v1"
)</pre>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <section class="py-12 px-4 border-t border-gray-800">
        <div class="max-w-4xl mx-auto">
            <h2 class="text-xl md:text-2xl font-bold mb-6 flex items-center gap-2"><span>🎫</span> 兑换券领取</h2>
            <div class="card p-6 md:p-8 glow">
                <div class="flex flex-col md:flex-row items-start md:items-center justify-between gap-6">
                    <div>
                        <h3 class="text-lg md:text-xl font-bold mb-2">免费领取API额度</h3>
                        <p class="text-gray-400 text-sm mb-3">每 <span id="cd-text">{{COOLDOWN_TEXT}}</span> 可领取 <span id="claim-times">{{CLAIM_TIMES}}</span> 次</p>
                        <span class="inline-block bg-green-900/40 text-green-400 px-3 py-1.5 rounded-full border border-green-800 text-sm">📦 可领: <b id="avail-cnt">{{AVAILABLE}}</b> 个</span>
                        <div id="bigPrizesHome" class="mt-3"></div>
                    </div>
                    <a href="/claim" class="btn btn-primary text-base px-6 py-3 w-full md:w-auto">🎁 立即领取 →</a>
                </div>
            </div>
        </div>
    </section>

    <section class="py-12 px-4 border-t border-gray-800">
        <div class="max-w-4xl mx-auto">
            <h2 class="text-xl md:text-2xl font-bold mb-6 flex items-center gap-2"><span>📋</span> 使用须知</h2>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div class="card p-5">
                    <h3 class="font-semibold mb-2 text-blue-400 text-sm">✅ 允许使用</h3>
                    <ul class="text-gray-400 text-sm space-y-1">
                        <li>• 个人学习研究</li>
                        <li>• 小型项目开发</li>
                        <li>• 合理频率调用</li>
                    </ul>
                </div>
                <div class="card p-5">
                    <h3 class="font-semibold mb-2 text-red-400 text-sm">❌ 禁止行为</h3>
                    <ul class="text-gray-400 text-sm space-y-1">
                        <li>• 商业盈利用途</li>
                        <li>• 高频滥用接口</li>
                        <li>• 违法违规内容</li>
                    </ul>
                </div>
                <div class="card p-5">
                    <h3 class="font-semibold mb-2 text-yellow-400 text-sm">⚠️ 注意事项</h3>
                    <ul class="text-gray-400 text-sm space-y-1">
                        <li>• 请勿分享API Key</li>
                        <li>• 违规将被封禁</li>
                        <li>• 额度用完可领券</li>
                    </ul>
                </div>
            </div>
        </div>
    </section>

    <footer class="border-t border-gray-800 py-6 px-4 text-center text-gray-500 text-sm">
        <p>{{SITE_NAME}} © 2025 | <a href="{{NEW_API_URL}}/console" target="_blank" class="text-blue-400 hover:underline">控制台</a> | <a href="/claim" class="text-blue-400 hover:underline">领券中心</a></p>
    </footer>

    <script>
        function copyAPI(){
            navigator.clipboard.writeText('{{NEW_API_URL}}');
            var btn=document.getElementById('copy-btn');
            btn.textContent='已复制';btn.classList.remove('bg-blue-600');btn.classList.add('bg-green-600');
            setTimeout(function(){btn.textContent='复制';btn.classList.remove('bg-green-600');btn.classList.add('bg-blue-600');},1500);
        }
        fetch('/api/stats/public').then(r=>r.json()).then(d=>{
            document.getElementById('avail-cnt').textContent=d.available;
            document.getElementById('cd-text').textContent=d.cooldown_text;
            document.getElementById('claim-times').textContent=d.claim_times;
            if(d.big_prizes && d.big_prizes.length > 0){
                var html='<div class="flex gap-2 flex-wrap">';
                d.big_prizes.forEach(function(p){
                    html+='<span class="bg-yellow-900/50 text-yellow-400 px-2 py-1 rounded text-xs border border-yellow-700">🏆 $'+p.quota+' x'+p.count+'</span>';
                });
                html+='</div>';
                document.getElementById('bigPrizesHome').innerHTML=html;
            }
        }).catch(()=>{});
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
        body{background:var(--bg);color:#e0e0e0;font-family:system-ui,sans-serif}
        .card{background:var(--card);border:1px solid var(--border);border-radius:12px}
        .btn-p{background:linear-gradient(135deg,#3b82f6,#1d4ed8);color:#fff;padding:12px 24px;border-radius:8px;font-weight:600;border:none;cursor:pointer;font-size:14px;text-decoration:none;display:inline-block;text-align:center}
        .btn-p:hover{opacity:0.9}.btn-p:disabled{background:#374151;cursor:not-allowed}
        .btn-c{background:linear-gradient(135deg,#10b981,#059669);color:#fff;padding:14px 32px;border-radius:10px;font-weight:700;font-size:16px;border:none;cursor:pointer}
        .btn-c:hover{transform:scale(1.02)}.btn-c:disabled{background:#374151;cursor:not-allowed;transform:none}
        .ld{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,0.3);border-radius:50%;border-top-color:#fff;animation:spin 1s linear infinite}
        @keyframes spin{to{transform:rotate(360deg)}}
        .toast{position:fixed;top:70px;left:50%;transform:translateX(-50%);padding:10px 20px;border-radius:8px;color:#fff;font-weight:500;z-index:9999;animation:fadeIn .3s;font-size:14px}
        @keyframes fadeIn{from{opacity:0;transform:translateX(-50%) translateY(-10px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
        .prize{animation:pop .5s ease-out}
        @keyframes pop{0%{transform:scale(0.5);opacity:0}50%{transform:scale(1.1)}100%{transform:scale(1);opacity:1}}
        .cpn{background:linear-gradient(135deg,#3b82f6,#1d4ed8);border-radius:8px;padding:10px 12px;margin-bottom:6px}
        .amount-big{font-size:40px;font-weight:800;background:linear-gradient(135deg,#fbbf24,#f59e0b);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
    </style>
</head>
<body class="min-h-screen">
    <nav class="border-b border-gray-800 px-4 py-3 sticky top-0 bg-[#0a0a0f]/95 backdrop-blur z-50">
        <div class="max-w-4xl mx-auto flex justify-between items-center">
            <a href="/" class="text-lg font-bold text-white">{{SITE_NAME}}</a>
            <div id="navRight" class="flex items-center gap-2">
                <span id="navUser" class="text-gray-400 text-sm hidden"></span>
                <button id="logoutBtn" onclick="doLogout()" class="text-red-400 text-sm hidden hover:underline">退出</button>
                <a href="/" class="text-gray-400 hover:text-white text-sm px-2">首页</a>
            </div>
        </div>
    </nav>

    <main class="max-w-4xl mx-auto px-4 py-6">
        <div id="sec-loading" class="card p-8 text-center">
            <div class="ld mb-4" style="width:32px;height:32px;border-width:3px;margin:0 auto"></div>
            <p class="text-gray-400">正在检查登录状态...</p>
        </div>

        <div id="sec-login" class="card p-6 md:p-8" style="display:none">
            <div class="text-center mb-6">
                <div class="text-5xl mb-4">🎁</div>
                <h1 class="text-xl md:text-2xl font-bold">兑换券领取中心</h1>
                <p class="text-gray-400 mt-2 text-sm">登录后即可领取免费额度</p>
                <div class="mt-4 inline-flex items-center bg-blue-900/30 text-blue-300 px-4 py-2 rounded-full border border-blue-800 text-sm">
                    📦 当前可领: <span id="cnt" class="font-bold ml-1">{{AVAILABLE}}</span> 个
                </div>
            </div>
            
            <div class="max-w-sm mx-auto">
                <div class="p-4 bg-green-900/20 border border-green-800 rounded-lg mb-4">
                    <p class="text-green-400 text-sm mb-2">🚀 一键登录</p>
                    <p class="text-gray-400 text-xs">点击下方按钮，登录主站后返回即可自动识别</p>
                </div>
                <a href="{{NEW_API_URL}}/console" target="_blank" class="btn-p w-full block" id="loginBtn">前往主站登录 →</a>
                <button onclick="checkAuth()" class="mt-3 w-full text-center text-blue-400 text-sm hover:underline">已登录？点击刷新</button>
            </div>
            
            <div id="bigPrizesLogin" class="mt-6 text-center"></div>
        </div>

        <div id="sec-claim" style="display:none">
            <div class="grid md:grid-cols-3 gap-4">
                <div class="md:col-span-2 space-y-4">
                    <div class="card p-4">
                        <div class="flex justify-between items-center">
                            <div>
                                <p class="text-gray-500 text-xs">当前用户</p>
                                <p id="uinfo" class="font-semibold text-sm"></p>
                            </div>
                            <a href="{{NEW_API_URL}}/console" target="_blank" class="text-blue-400 text-sm hover:underline">控制台 →</a>
                        </div>
                    </div>
                    
                    <div class="card p-5">
                        <div class="flex flex-wrap justify-between items-center gap-2 mb-4">
                            <h2 class="font-semibold text-sm">领取状态</h2>
                            <div class="flex items-center gap-2 flex-wrap">
                                <span id="modeBadge" class="px-2 py-0.5 rounded text-xs"></span>
                                <span id="remainBadge" class="px-2 py-0.5 rounded text-xs bg-purple-900/50 text-purple-400 border border-purple-700"></span>
                                <span id="badge" class="px-2 py-0.5 rounded text-xs"></span>
                            </div>
                        </div>
                        <div class="text-center py-4">
                            <button type="button" id="claimBtn" class="btn-c" onclick="doClaim()">🎰 抽取兑换券</button>
                            <p id="cdMsg" class="text-gray-500 mt-3 text-sm"></p>
                        </div>
                        
                        <div id="prizeBox" style="display:none" class="text-center py-6 border-t border-gray-800 mt-4">
                            <div class="prize">
                                <div class="text-gray-400 mb-2 text-sm">🎉 恭喜获得</div>
                                <div id="prizeAmount" class="amount-big mb-3"></div>
                                <div id="autoRedeemMsg" class="text-green-400 text-sm mb-3" style="display:none">✅ 已自动充值到您的账户</div>
                                <div id="manualRedeemBox">
                                    <div class="text-gray-400 text-xs mb-1">兑换码:</div>
                                    <div id="prizeCode" class="font-mono text-sm bg-gray-800 p-2 rounded border border-gray-700 mb-2 break-all"></div>
                                    <button type="button" class="text-blue-400 text-sm hover:underline" onclick="copyPrize()">📋 复制兑换码</button>
                                    <p class="text-xs text-gray-500 mt-2">前往 <a href="{{NEW_API_URL}}/console/topup" target="_blank" class="text-blue-400">钱包充值</a> 兑换</p>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="card p-5">
                        <h2 class="font-semibold mb-3 text-sm">📋 领取记录</h2>
                        <div id="hist" class="max-h-60 overflow-y-auto"></div>
                    </div>
                </div>
                
                <div class="md:col-span-1">
                    <div id="bigPrizeSection" class="card p-5" style="display:none">
                        <h2 class="font-semibold mb-3 flex items-center gap-2 text-sm">🏆 大奖池</h2>
                        <div id="bigPrizeList"></div>
                        <p class="text-xs text-gray-500 mt-3">以上大奖等你来抽！</p>
                    </div>
                    <div id="noBigPrize" class="card p-5 text-center text-gray-500">
                        <div class="text-3xl mb-2">🎰</div>
                        <p class="text-sm">暂无大奖</p>
                    </div>
                    
                    <div class="card p-5 mt-4">
                        <h2 class="font-semibold mb-3 text-sm">📊 统计</h2>
                        <div class="space-y-2 text-sm">
                            <div class="flex justify-between">
                                <span class="text-gray-500">可领取</span>
                                <span id="statAvail" class="text-green-400 font-bold">-</span>
                            </div>
                            <div class="flex justify-between">
                                <span class="text-gray-500">冷却时间</span>
                                <span id="statCd" class="text-gray-300">-</span>
                            </div>
                            <div class="flex justify-between">
                                <span class="text-gray-500">每周期次数</span>
                                <span id="statTimes" class="text-gray-300">-</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </main>

    <footer class="text-center py-6 text-gray-600 text-sm px-4">
        每 <span id="cd-text">{{COOLDOWN_TEXT}}</span> 可领取 <span id="claim-times">{{CLAIM_TIMES}}</span> 次 | <a href="/" class="text-blue-400 hover:underline">返回首页</a>
    </footer>

    <script>
    var userData = null;

    document.addEventListener('DOMContentLoaded', function(){
        checkAuth();
        loadPublicStats();
    });

    function loadPublicStats(){
        fetch('/api/stats/public').then(r=>r.json()).then(d=>{
            document.getElementById('cnt').textContent=d.available;
            document.getElementById('cd-text').textContent=d.cooldown_text;
            document.getElementById('claim-times').textContent=d.claim_times;
            renderBigPrizes(d.big_prizes, 'bigPrizesLogin');
        }).catch(()=>{});
    }

    function checkAuth(){
        document.getElementById('sec-loading').style.display='block';
        document.getElementById('sec-login').style.display='none';
        document.getElementById('sec-claim').style.display='none';
        
        fetch('/api/auth/check',{credentials:'include'})
        .then(r=>r.json())
        .then(d=>{
            document.getElementById('sec-loading').style.display='none';
            if(d.success && d.logged_in){
                userData = d.data;
                showLoggedIn();
                loadStatus();
            }else{
                showLogin();
            }
        })
        .catch(()=>{
            document.getElementById('sec-loading').style.display='none';
            showLogin();
        });
    }

    function showLogin(){
        document.getElementById('sec-login').style.display='block';
        document.getElementById('sec-claim').style.display='none';
        document.getElementById('navUser').classList.add('hidden');
        document.getElementById('logoutBtn').classList.add('hidden');
    }

    function showLoggedIn(){
        document.getElementById('sec-login').style.display='none';
        document.getElementById('sec-claim').style.display='block';
        document.getElementById('uinfo').textContent=userData.username+' (ID:'+userData.user_id+')';
        document.getElementById('navUser').textContent=userData.username;
        document.getElementById('navUser').classList.remove('hidden');
        document.getElementById('logoutBtn').classList.remove('hidden');
    }

    function doLogout(){
        fetch('/api/auth/logout',{method:'POST',credentials:'include'})
        .then(()=>{
            document.cookie = 'coupon_session=;path=/;max-age=0';
            userData = null;
            showLogin();
            toast('已退出登录',true);
        });
    }

    function renderBigPrizes(prizes, containerId){
        var container = document.getElementById(containerId);
        if(!container) return;
        
        if(!prizes || prizes.length === 0){
            if(containerId === 'bigPrizesLogin'){
                container.innerHTML = '';
            }
            return;
        }
        
        var html = '<div class="flex gap-2 flex-wrap justify-center">';
        prizes.forEach(function(p){
            html += '<span class="bg-yellow-900/50 text-yellow-400 px-2 py-1 rounded text-xs border border-yellow-700">🏆 $'+p.quota+' x'+p.count+'</span>';
        });
        html += '</div>';
        container.innerHTML = html;
        
        if(containerId !== 'bigPrizeList'){
            var section = document.getElementById('bigPrizeSection');
            var noPrize = document.getElementById('noBigPrize');
            var list = document.getElementById('bigPrizeList');
            
            if(prizes.length > 0){
                section.style.display = 'block';
                noPrize.style.display = 'none';
                
                var listHtml = '';
                prizes.forEach(function(p){
                    listHtml += '<div class="bg-gradient-to-r from-yellow-900/50 to-orange-900/50 border border-yellow-700 rounded-lg p-2 mb-2 flex justify-between items-center">';
                    listHtml += '<span class="text-yellow-400 font-bold">$' + p.quota + '</span>';
                    listHtml += '<span class="bg-yellow-500 text-black px-2 py-0.5 rounded font-bold text-xs">x' + p.count + '</span>';
                    listHtml += '</div>';
                });
                list.innerHTML = listHtml;
            }else{
                section.style.display = 'none';
                noPrize.style.display = 'block';
            }
        }
    }

    function toast(msg,ok){
        var t=document.createElement('div');
        t.className='toast '+(ok?'bg-green-600':'bg-red-600');
        t.textContent=msg;
        document.body.appendChild(t);
        setTimeout(()=>t.remove(),3000);
    }

    function loadStatus(){
        fetch('/api/claim/status',{credentials:'include'})
        .then(r=>{
            if(r.status===401){
                showLogin();
                return null;
            }
            return r.json();
        })
        .then(res=>{
            if(res && res.success){
                updateUI(res.data);
                renderBigPrizes(res.data.big_prizes, 'bigPrizeList');
            }
        })
        .catch(()=>{});
    }

    function updateUI(d){
        document.getElementById('statAvail').textContent=d.available_count;
        document.getElementById('statCd').textContent=document.getElementById('cd-text').textContent;
        document.getElementById('statTimes').textContent=d.claim_times+'次';
        
        var btn=document.getElementById('claimBtn');
        var badge=document.getElementById('badge');
        var remainBadge=document.getElementById('remainBadge');
        var modeBadge=document.getElementById('modeBadge');
        var msg=document.getElementById('cdMsg');
        
        remainBadge.textContent='剩余 '+d.remaining_claims+'/'+d.claim_times+' 次';
        
        if(d.claim_mode === 'B'){
            modeBadge.textContent='🔄 自动充值';
            modeBadge.className='px-2 py-0.5 rounded text-xs bg-green-900/50 text-green-400 border border-green-700';
        }else{
            modeBadge.textContent='📝 返回兑换码';
            modeBadge.className='px-2 py-0.5 rounded text-xs bg-blue-900/50 text-blue-400 border border-blue-700';
        }
        
        if(d.can_claim){
            btn.disabled=false;
            badge.textContent='✅ 可领取';
            badge.className='px-2 py-0.5 rounded text-xs bg-green-900/50 text-green-400 border border-green-700';
            msg.textContent='';
        }else{
            btn.disabled=true;
            badge.textContent='⏳ 冷却中';
            badge.className='px-2 py-0.5 rounded text-xs bg-yellow-900/50 text-yellow-400 border border-yellow-700';
            msg.textContent=d.cooldown_text||'';
        }
        
        var h=document.getElementById('hist');
        if(!d.history||d.history.length===0){
            h.innerHTML='<p class="text-gray-500 text-center text-sm">暂无记录</p>';
        }else{
            var html='';
            d.history.forEach(r=>{
                var statusText=r.auto_redeemed?'<span class="text-green-400 text-xs ml-1">[已充值]</span>':'';
                html+='<div class="cpn text-white text-sm"><div class="flex justify-between items-center"><span class="font-mono text-xs truncate flex-1 mr-2">'+r.coupon_code+'</span><span class="bg-white/20 px-2 py-0.5 rounded text-xs shrink-0">$'+r.quota+'</span></div><div class="flex justify-between items-center mt-1"><span class="text-xs text-blue-200">'+new Date(r.claim_time).toLocaleString('zh-CN')+'</span>'+statusText+'</div></div>';
            });
            h.innerHTML=html;
        }
    }

    function doClaim(){
        var btn=document.getElementById('claimBtn');
        btn.disabled=true;
        btn.innerHTML='<span class="ld"></span> 抽取中...';
        document.getElementById('prizeBox').style.display='none';
        
        fetch('/api/claim',{
            method:'POST',
            credentials:'include',
            headers:{'Content-Type':'application/json'}
        })
        .then(r=>{
            if(r.status===401){
                showLogin();
                toast('请先登录',false);
                return null;
            }
            return r.json().then(d=>({ok:r.ok,data:d}));
        })
        .then(result=>{
            if(!result) return;
            var {ok,data}=result;
            btn.innerHTML='🎰 抽取兑换券';
            
            if(ok&&data.success){
                var d=data.data;
                document.getElementById('prizeAmount').textContent='$'+d.quota;
                document.getElementById('prizeCode').textContent=d.coupon_code;
                document.getElementById('prizeBox').style.display='block';
                
                if(d.auto_redeemed){
                    document.getElementById('autoRedeemMsg').style.display='block';
                    document.getElementById('manualRedeemBox').style.display='none';
                    toast('恭喜获得 $'+d.quota+'！已自动充值',true);
                }else{
                    document.getElementById('autoRedeemMsg').style.display='none';
                    document.getElementById('manualRedeemBox').style.display='block';
                    navigator.clipboard.writeText(d.coupon_code).catch(()=>{});
                    toast('恭喜获得 $'+d.quota+'！兑换码已复制',true);
                }
            }else{
                toast(data.detail||'领取失败',false);
            }
            loadStatus();
            loadPublicStats();
        })
        .catch(()=>{
            btn.innerHTML='🎰 抽取兑换券';
            toast('网络错误',false);
            loadStatus();
        });
    }

    function copyPrize(){
        var code=document.getElementById('prizeCode').textContent;
        navigator.clipboard.writeText(code).then(()=>toast('已复制',true)).catch(()=>toast('复制失败',false));
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
        .ipt{background:#0d0d12;border:1px solid #1f1f2e;color:#e0e0e0;border-radius:8px;padding:10px 14px;width:100%;font-size:14px}
        .ipt:focus{border-color:#3b82f6;outline:none}
        .btn{padding:8px 16px;border-radius:8px;font-weight:600;border:none;cursor:pointer;font-size:13px}
        .btn-blue{background:#3b82f6;color:#fff}.btn-blue:hover{background:#2563eb}
        .btn-green{background:#10b981;color:#fff}.btn-green:hover{background:#059669}
        .btn-purple{background:#8b5cf6;color:#fff}.btn-purple:hover{background:#7c3aed}
        .btn-red{background:#ef4444;color:#fff}.btn-red:hover{background:#dc2626}
        .btn-gray{background:#374151;color:#fff}.btn-gray:hover{background:#4b5563}
        #overlay{position:fixed;inset:0;background:rgba(0,0,0,0.95);display:flex;align-items:center;justify-content:center;z-index:100}
        #toast{position:fixed;top:20px;left:50%;transform:translateX(-50%);padding:10px 20px;border-radius:8px;color:#fff;z-index:200;display:none;font-size:14px}
        .tab-btn{padding:6px 12px;border-radius:8px;cursor:pointer;transition:all .2s;font-size:13px}
        .tab-btn.active{background:#3b82f6;color:#fff}
        .tab-btn:not(.active){background:#1f1f2e;color:#9ca3af}
        .coupon-row{display:grid;grid-template-columns:1fr 60px 60px 100px 60px;gap:6px;padding:8px;border-bottom:1px solid #1f1f2e;align-items:center;font-size:12px}
        @media(max-width:640px){.coupon-row{grid-template-columns:1fr 50px 50px;}.coupon-row>div:nth-child(4),.coupon-row>div:nth-child(5){display:none}}
        .weight-row{display:flex;align-items:center;gap:6px;padding:8px;background:#1a1a24;border-radius:8px;margin-bottom:6px;flex-wrap:wrap}
        .switch{position:relative;width:44px;height:24px;background:#374151;border-radius:12px;cursor:pointer;transition:background .3s;flex-shrink:0}
        .switch.on{background:#10b981}
        .switch::after{content:'';position:absolute;top:2px;left:2px;width:20px;height:20px;background:#fff;border-radius:50%;transition:left .3s}
        .switch.on::after{left:22px}
        .prob-bar{height:6px;background:#1f1f2e;border-radius:3px;overflow:hidden}
        .prob-fill{height:100%;background:linear-gradient(90deg,#3b82f6,#10b981);border-radius:3px}
    </style>
</head>
<body class="min-h-screen">
    <div id="overlay">
        <div class="card p-6 w-full max-w-sm mx-4">
            <div class="text-center mb-6"><div class="text-4xl mb-2">🔐</div><h1 class="text-xl font-bold">管理后台</h1></div>
            <input type="password" id="loginPwd" class="ipt mb-4" placeholder="管理员密码">
            <button class="btn btn-blue w-full" onclick="doLogin()">登录</button>
            <a href="/" class="block text-center text-gray-500 text-sm mt-4">← 返回首页</a>
            <p id="loginError" class="text-red-500 text-center text-sm mt-2" style="display:none"></p>
        </div>
    </div>

    <div id="adminMain" style="display:none">
        <nav class="border-b border-gray-800 py-3 px-4 sticky top-0 bg-[#0a0a0f]/95 backdrop-blur z-50">
            <div class="max-w-6xl mx-auto flex flex-wrap justify-between items-center gap-2">
                <h1 class="font-bold text-lg">🔧 管理后台</h1>
                <div class="flex items-center gap-2 flex-wrap">
                    <span id="currentModeNav" class="text-xs"></span>
                    <a href="/" class="text-gray-400 hover:text-white text-sm">首页</a>
                    <button class="text-red-400 text-sm" onclick="doLogout()">退出</button>
                </div>
            </div>
        </nav>

        <div class="max-w-6xl mx-auto px-4 py-3">
            <div class="flex gap-2 flex-wrap overflow-x-auto">
                <button class="tab-btn active" onclick="switchTab('overview')">📊 总览</button>
                <button class="tab-btn" onclick="switchTab('coupons')">🎫 兑换码</button>
                <button class="tab-btn" onclick="switchTab('add')">➕ 添加</button>
                <button class="tab-btn" onclick="switchTab('config')">⚙️ 配置</button>
            </div>
        </div>

        <main class="max-w-6xl mx-auto px-4 pb-8">
            <div id="tab-overview" class="tab-content">
                <div class="grid lg:grid-cols-3 gap-4">
                    <div class="lg:col-span-2"><div class="card p-5"><h2 class="font-semibold mb-4 text-sm">📊 统计数据</h2><div id="statsBox">加载中...</div></div></div>
                    <div><div class="card p-5"><h2 class="font-semibold mb-4 text-sm">📋 最近领取</h2><div id="recentBox" class="max-h-80 overflow-y-auto space-y-2 text-xs"></div></div></div>
                </div>
            </div>

            <div id="tab-coupons" class="tab-content" style="display:none">
                <div class="card p-5">
                    <div class="flex flex-wrap justify-between items-center gap-3 mb-4">
                        <h2 class="font-semibold text-sm">🎫 本地兑换码</h2>
                        <div class="flex gap-2 flex-wrap">
                            <select id="couponStatus" class="ipt w-auto text-sm py-2" onchange="loadCoupons(1)">
                                <option value="all">全部</option>
                                <option value="available">可用</option>
                                <option value="claimed">已领</option>
                            </select>
                            <input type="text" id="couponSearch" class="ipt w-32 text-sm py-2" placeholder="搜索..." onkeyup="if(event.key==='Enter')loadCoupons(1)">
                            <button class="btn btn-blue" onclick="loadCoupons(1)">搜索</button>
                        </div>
                    </div>
                    <div class="flex gap-2 mb-3 flex-wrap">
                        <button class="btn btn-gray text-xs" onclick="selectAllCoupons()">全选</button>
                        <button class="btn btn-red text-xs" onclick="deleteSelected()">删除选中</button>
                        <button class="btn btn-red text-xs" onclick="deleteBatch('all_claimed')">删除已领</button>
                    </div>
                    <div class="coupon-row text-gray-500 font-semibold border-b-2 border-gray-700">
                        <div class="flex items-center gap-1"><input type="checkbox" id="selectAllCheck" onchange="toggleSelectAll()"> 兑换码</div>
                        <div>额度</div>
                        <div>状态</div>
                        <div>领取信息</div>
                        <div>操作</div>
                    </div>
                    <div id="couponList"></div>
                    <div id="pagination" class="flex justify-center gap-1 mt-4 flex-wrap"></div>
                </div>
            </div>

            <div id="tab-add" class="tab-content" style="display:none">
                <div class="card p-5">
                    <h2 class="font-semibold mb-4 text-sm">➕ 添加本地兑换码</h2>
                    <div class="grid grid-cols-5 gap-2 mb-4">
                        <button onclick="setQuota(1)" class="bg-green-900/50 text-green-400 border border-green-700 py-2 rounded font-bold text-sm hover:opacity-80">$1</button>
                        <button onclick="setQuota(2)" class="bg-blue-900/50 text-blue-400 border border-blue-700 py-2 rounded font-bold text-sm hover:opacity-80">$2</button>
                        <button onclick="setQuota(5)" class="bg-purple-900/50 text-purple-400 border border-purple-700 py-2 rounded font-bold text-sm hover:opacity-80">$5</button>
                        <button onclick="setQuota(10)" class="bg-orange-900/50 text-orange-400 border border-orange-700 py-2 rounded font-bold text-sm hover:opacity-80">$10</button>
                        <button onclick="setQuota(20)" class="bg-red-900/50 text-red-400 border border-red-700 py-2 rounded font-bold text-sm hover:opacity-80">$20</button>
                    </div>
                    <div class="flex items-center gap-2 mb-4">
                        <span class="text-gray-400 text-sm">额度:</span>
                        <input type="number" id="quotaVal" value="1" step="0.01" min="0.01" class="w-20 ipt text-center font-bold text-sm">
                        <span class="text-gray-400 text-sm">美元</span>
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm text-gray-400 mb-2">上传TXT文件</label>
                        <input type="file" id="txtFile" accept=".txt" class="ipt text-sm">
                    </div>
                    <button class="btn btn-blue w-full mb-4" onclick="doUpload()">上传文件</button>
                    <hr class="border-gray-700 my-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-2">或手动粘贴（每行一个）</label>
                        <textarea id="codesText" rows="5" class="ipt font-mono text-sm" placeholder="每行一个兑换码"></textarea>
                    </div>
                    <button class="btn btn-green w-full mt-3" onclick="doAddCodes()">添加兑换码</button>
                </div>
            </div>

            <div id="tab-config" class="tab-content" style="display:none">
                <div class="grid lg:grid-cols-2 gap-4">
                    <div class="card p-5">
                        <h2 class="font-semibold mb-4 text-sm">🎯 模式设置</h2>
                        <div class="space-y-3">
                            <div class="flex items-center justify-between p-3 bg-gray-800/50 rounded-lg">
                                <div class="flex-1 min-w-0">
                                    <p class="font-semibold text-sm">自动充值模式 (B)</p>
                                    <p class="text-xs text-gray-500">开启后额度直接充值</p>
                                </div>
                                <div id="modeSwitch" class="switch" onclick="toggleMode()"></div>
                            </div>
                            <div id="modeDesc" class="text-xs text-gray-400 p-2 bg-blue-900/20 border border-blue-800 rounded"></div>
                            
                            <div class="flex items-center justify-between p-3 bg-gray-800/50 rounded-lg">
                                <div class="flex-1 min-w-0">
                                    <p class="font-semibold text-sm">概率: 权重×库存</p>
                                    <p class="text-xs text-gray-500">关闭则仅按权重</p>
                                </div>
                                <div id="probModeSwitch" class="switch" onclick="toggleProbMode()"></div>
                            </div>
                            
                            <div class="p-2 bg-yellow-900/20 border border-yellow-800 rounded">
                                <p id="tokenStatus" class="text-xs"></p>
                            </div>
                        </div>
                    </div>

                    <div class="card p-5">
                        <h2 class="font-semibold mb-4 text-sm">📊 额度比例 & 冷却</h2>
                        <div class="space-y-3">
                            <div>
                                <label class="block text-xs text-gray-400 mb-1">$1 = ? quota</label>
                                <input type="number" id="quotaRate" min="1" class="w-full ipt text-sm">
                            </div>
                            <div>
                                <label class="block text-xs text-gray-400 mb-1">冷却时间（分钟）</label>
                                <input type="number" id="cooldownMinutes" min="1" class="w-full ipt text-sm">
                            </div>
                            <div>
                                <label class="block text-xs text-gray-400 mb-1">每周期可领次数</label>
                                <input type="number" id="claimTimes" min="1" class="w-full ipt text-sm">
                            </div>
                            <button class="btn btn-blue w-full" onclick="saveBasicConfig()">保存</button>
                        </div>
                    </div>

                    <div class="card p-5 lg:col-span-2">
                        <h2 class="font-semibold mb-4 text-sm">🎰 概率权重 & 虚拟库存</h2>
                        <div id="weightsContainer" class="max-h-64 overflow-y-auto mb-4"></div>
                        <div class="flex gap-2 mb-4 flex-wrap">
                            <input type="number" id="newQuotaKey" step="0.01" placeholder="额度" class="w-16 ipt text-center text-sm py-2">
                            <input type="number" id="newQuotaWeight" step="0.01" placeholder="权重" class="w-16 ipt text-center text-sm py-2">
                            <input type="number" id="newQuotaStock" placeholder="库存" class="w-16 ipt text-center text-sm py-2">
                            <button class="btn btn-green" onclick="addWeight()">添加</button>
                        </div>
                        <button class="btn btn-purple w-full" onclick="saveWeightsAndStock()">保存配置</button>
                    </div>
                </div>
            </div>
        </main>
    </div>

    <div id="toast"></div>

    <script>
    var adminPwd='';var currentWeights={};var currentStock={};var selectedCoupons=new Set();var currentPage=1;var currentMode='A';var currentProbMode='weight_stock';

    (function(){
        var saved=sessionStorage.getItem('admin_pwd');
        if(saved){adminPwd=saved;verifyAndShow();}
        document.getElementById('loginPwd').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});
    })();

    function toast(msg,ok){var t=document.getElementById('toast');t.textContent=msg;t.style.display='block';t.style.background=ok?'#10b981':'#ef4444';setTimeout(()=>t.style.display='none',3000);}

    function doLogin(){
        var pwd=document.getElementById('loginPwd').value;
        if(!pwd){document.getElementById('loginError').textContent='请输入密码';document.getElementById('loginError').style.display='block';return;}
        fetch('/api/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pwd})})
        .then(r=>{if(r.ok){adminPwd=pwd;sessionStorage.setItem('admin_pwd',pwd);document.getElementById('overlay').style.display='none';document.getElementById('adminMain').style.display='block';loadStats();}else{document.getElementById('loginError').textContent='密码错误';document.getElementById('loginError').style.display='block';}});
    }

    function verifyAndShow(){
        fetch('/api/admin/stats?password='+encodeURIComponent(adminPwd))
        .then(r=>{if(r.ok){document.getElementById('overlay').style.display='none';document.getElementById('adminMain').style.display='block';loadStats();}else{sessionStorage.removeItem('admin_pwd');adminPwd='';}});
    }

    function doLogout(){sessionStorage.removeItem('admin_pwd');adminPwd='';location.reload();}

    function switchTab(tab){
        document.querySelectorAll('.tab-content').forEach(el=>el.style.display='none');
        document.querySelectorAll('.tab-btn').forEach(el=>el.classList.remove('active'));
        document.getElementById('tab-'+tab).style.display='block';
        event.target.classList.add('active');
        if(tab==='coupons')loadCoupons(1);
        if(tab==='overview')loadStats();
    }

    function setQuota(q){document.getElementById('quotaVal').value=q;}

    function doUpload(){
        var q=document.getElementById('quotaVal').value;
        var f=document.getElementById('txtFile').files[0];
        if(!f){toast('请选择文件',false);return;}
        var fd=new FormData();fd.append('password',adminPwd);fd.append('quota',q);fd.append('file',f);
        fetch('/api/admin/upload-txt',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{toast(d.message||d.detail,d.success);if(d.success){loadStats();document.getElementById('txtFile').value='';}});
    }

    function doAddCodes(){
        var q=parseFloat(document.getElementById('quotaVal').value);
        var txt=document.getElementById('codesText').value;
        var arr=txt.split('\\n').filter(s=>s.trim());
        if(!arr.length){toast('请输入兑换码',false);return;}
        fetch('/api/admin/add-coupons',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:adminPwd,quota:q,coupons:arr})})
        .then(r=>r.json()).then(d=>{toast(d.message||d.detail,d.success);if(d.success){loadStats();document.getElementById('codesText').value='';}});
    }

    function loadCoupons(page){
        currentPage=page;selectedCoupons.clear();
        var status=document.getElementById('couponStatus').value;
        var search=document.getElementById('couponSearch').value;
        fetch('/api/admin/coupons?password='+encodeURIComponent(adminPwd)+'&page='+page+'&status='+status+'&search='+encodeURIComponent(search))
        .then(r=>r.json()).then(res=>{if(res.success)renderCoupons(res.data);});
    }

    function renderCoupons(data){
        var html='';
        data.coupons.forEach(function(c){
            var statusClass=c.is_claimed?'text-gray-500':'text-green-400';
            var statusText=c.is_claimed?'已领':'可用';
            html+='<div class="coupon-row">';
            html+='<div class="flex items-center gap-1 min-w-0"><input type="checkbox" data-id="'+c.id+'" onchange="toggleSelect('+c.id+')"><span class="font-mono truncate">'+c.code+'</span></div>';
            html+='<div class="text-blue-400 font-bold">$'+c.quota+'</div>';
            html+='<div class="'+statusClass+'">'+statusText+'</div>';
            html+='<div class="text-gray-500 truncate">'+(c.claimed_by||'-')+'</div>';
            html+='<div><button class="text-red-400 hover:text-red-300" onclick="deleteCoupon('+c.id+')">删</button></div>';
            html+='</div>';
        });
        document.getElementById('couponList').innerHTML=html||'<p class="text-gray-500 text-center py-4 text-sm">暂无数据</p>';
        var phtml='';for(var i=1;i<=data.pages;i++){phtml+='<button class="px-2 py-1 rounded text-xs '+(i===data.page?'bg-blue-600':'bg-gray-700')+'" onclick="loadCoupons('+i+')">'+i+'</button>';}
        document.getElementById('pagination').innerHTML=phtml;
    }

    function toggleSelect(id){if(selectedCoupons.has(id))selectedCoupons.delete(id);else selectedCoupons.add(id);}
    function toggleSelectAll(){var checked=document.getElementById('selectAllCheck').checked;document.querySelectorAll('#couponList input[type=checkbox]').forEach(cb=>{cb.checked=checked;var id=parseInt(cb.dataset.id);if(checked)selectedCoupons.add(id);else selectedCoupons.delete(id);});}
    function selectAllCoupons(){document.getElementById('selectAllCheck').checked=true;toggleSelectAll();}

    function deleteCoupon(id){if(!confirm('确定删除？'))return;fetch('/api/admin/delete-coupon',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:adminPwd,id:id})}).then(r=>r.json()).then(d=>{toast(d.message,d.success);if(d.success)loadCoupons(currentPage);});}

    function deleteSelected(){if(selectedCoupons.size===0){toast('请先选择',false);return;}if(!confirm('确定删除选中的 '+selectedCoupons.size+' 个？'))return;fetch('/api/admin/delete-coupons-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:adminPwd,ids:Array.from(selectedCoupons),type:'selected'})}).then(r=>r.json()).then(d=>{toast(d.message,d.success);if(d.success)loadCoupons(currentPage);});}

    function deleteBatch(type){if(!confirm('确定删除？'))return;fetch('/api/admin/delete-coupons-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:adminPwd,type:type})}).then(r=>r.json()).then(d=>{toast(d.message,d.success);if(d.success)loadCoupons(1);});}

    function renderWeightsAndStock(weights, stock, probInfo){
        currentWeights={};currentStock={};
        for(var k in weights)currentWeights[k]=weights[k];
        for(var k in stock)currentStock[k]=stock[k];
        
        var probMap = {};
        if(probInfo){probInfo.forEach(function(p){ probMap[p.quota] = p.probability; });}
        
        var allKeys = new Set([...Object.keys(currentWeights), ...Object.keys(currentStock)]);
        var sortedKeys = Array.from(allKeys).sort((a,b)=>parseFloat(a)-parseFloat(b));
        
        var html='';
        sortedKeys.forEach(function(k){
            var weight = currentWeights[k] || 0;
            var stockVal = currentStock[k] || 0;
            var prob = probMap[k] || 0;
            var isBigPrize = parseFloat(k) >= 50;
            var rowClass = isBigPrize ? 'border-l-4 border-yellow-500' : '';
            var stockClass = stockVal <= 0 ? 'border-red-500 bg-red-900/20' : '';
            
            html+='<div class="weight-row '+rowClass+'">';
            html+='<div class="flex items-center gap-1 w-16">';
            if(isBigPrize) html+='<span class="text-yellow-400 text-xs">🏆</span>';
            html+='<span class="text-blue-400 font-bold text-sm">$'+k+'</span></div>';
            html+='<input type="number" step="0.01" min="0" value="'+weight+'" onchange="updateWeight(\\''+k+'\\', this.value)" class="w-14 ipt text-center text-xs p-1" title="权重">';
            html+='<input type="number" min="0" value="'+stockVal+'" onchange="updateStock(\\''+k+'\\', this.value)" class="w-14 ipt text-center text-xs p-1 '+stockClass+'" title="库存">';
            html+='<div class="flex-1 min-w-20"><div class="prob-bar"><div class="prob-fill" style="width:'+Math.min(prob,100)+'%"></div></div><span class="text-xs text-gray-400">'+prob.toFixed(1)+'%</span></div>';
            html+='<button onclick="removeQuota(\\''+k+'\\')" class="text-red-400 text-sm">✕</button>';
            html+='</div>';
        });
        document.getElementById('weightsContainer').innerHTML=html||'<p class="text-gray-500 text-sm">暂无配置</p>';
    }

    function updateWeight(key,val){currentWeights[key]=parseFloat(val)||0;}
    function updateStock(key,val){currentStock[key]=parseInt(val)||0;}
    function removeQuota(key){delete currentWeights[key];delete currentStock[key];renderWeightsAndStock(currentWeights,currentStock,null);}

    function addWeight(){
        var key=document.getElementById('newQuotaKey').value;
        var weight=document.getElementById('newQuotaWeight').value;
        var stock=document.getElementById('newQuotaStock').value;
        if(!key){toast('请输入额度',false);return;}
        currentWeights[key]=parseFloat(weight)||1;
        currentStock[key]=parseInt(stock)||0;
        renderWeightsAndStock(currentWeights,currentStock,null);
        document.getElementById('newQuotaKey').value='';
        document.getElementById('newQuotaWeight').value='';
        document.getElementById('newQuotaStock').value='';
    }

    function toggleMode(){
        currentMode=currentMode==='A'?'B':'A';
        updateModeUI();
        fetch('/api/admin/update-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:adminPwd,claim_mode:currentMode})})
        .then(r=>r.json()).then(d=>{toast(d.message,d.success);loadStats();});
    }

    function toggleProbMode(){
        currentProbMode=currentProbMode==='weight_only'?'weight_stock':'weight_only';
        updateProbModeUI();
        fetch('/api/admin/update-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:adminPwd,probability_mode:currentProbMode})})
        .then(r=>r.json()).then(d=>{toast(d.message,d.success);loadStats();});
    }

    function updateModeUI(){
        var sw=document.getElementById('modeSwitch');
        var desc=document.getElementById('modeDesc');
        var nav=document.getElementById('currentModeNav');
        if(currentMode==='B'){
            sw.classList.add('on');
            desc.innerHTML='<b class="text-green-400">模式B</b>：自动充值到用户账户';
            nav.innerHTML='<span class="bg-green-900/50 text-green-400 px-2 py-0.5 rounded text-xs">🔄 自动充值</span>';
        }else{
            sw.classList.remove('on');
            desc.innerHTML='<b class="text-blue-400">模式A</b>：返回兑换码，用户自行兑换';
            nav.innerHTML='<span class="bg-blue-900/50 text-blue-400 px-2 py-0.5 rounded text-xs">📝 返回兑换码</span>';
        }
    }

    function updateProbModeUI(){
        var sw=document.getElementById('probModeSwitch');
        if(currentProbMode==='weight_stock'){sw.classList.add('on');}else{sw.classList.remove('on');}
    }

    function saveBasicConfig(){
        var minutes=parseInt(document.getElementById('cooldownMinutes').value);
        var times=parseInt(document.getElementById('claimTimes').value);
        var rate=parseInt(document.getElementById('quotaRate').value);
        fetch('/api/admin/update-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:adminPwd,cooldown_minutes:minutes,claim_times:times,quota_rate:rate})}).then(r=>r.json()).then(d=>toast(d.message,d.success));
    }

    function saveWeightsAndStock(){
        fetch('/api/admin/update-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:adminPwd,quota_weights:currentWeights,quota_stock:currentStock})}).then(r=>r.json()).then(d=>{toast(d.message,d.success);if(d.success)loadStats();});
    }

    function loadStats(){
        fetch('/api/admin/stats?password='+encodeURIComponent(adminPwd)).then(r=>r.json()).then(res=>{
            if(!res.success)return;var d=res.data;
            document.getElementById('cooldownMinutes').value=d.cooldown_minutes;
            document.getElementById('claimTimes').value=d.claim_times;
            document.getElementById('quotaRate').value=d.quota_rate;
            currentMode=d.claim_mode;
            currentProbMode=d.probability_mode||'weight_stock';
            updateModeUI();
            updateProbModeUI();
            renderWeightsAndStock(d.quota_weights, d.quota_stock, d.probability_info);
            
            var tokenStatus=document.getElementById('tokenStatus');
            tokenStatus.textContent=d.admin_token_configured?'✅ 管理员令牌已配置':'❌ 未配置管理员令牌';
            tokenStatus.className='text-xs '+(d.admin_token_configured?'text-green-400':'text-red-400');
            
            var h='<div class="grid grid-cols-3 gap-3 mb-4">';
            h+='<div class="bg-gray-800 p-3 rounded-lg text-center"><div class="text-xl font-bold">'+d.total+'</div><div class="text-gray-500 text-xs">本地总数</div></div>';
            h+='<div class="bg-green-900/30 p-3 rounded-lg text-center border border-green-800"><div class="text-xl font-bold text-green-400">'+d.total_virtual_stock+'</div><div class="text-gray-500 text-xs">可抽库存</div></div>';
            h+='<div class="bg-blue-900/30 p-3 rounded-lg text-center border border-blue-800"><div class="text-xl font-bold text-blue-400">'+d.claimed+'</div><div class="text-gray-500 text-xs">已领取</div></div>';
            h+='</div>';
            
            if(d.probability_info && d.probability_info.length > 0){
                h+='<div class="mb-3"><h3 class="text-xs font-semibold text-gray-400 mb-2">📊 概率分布</h3><div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2">';
                d.probability_info.forEach(function(p){
                    var colorClass = p.stock > 0 ? 'bg-green-900/30 border-green-800 text-green-400' : 'bg-red-900/30 border-red-800 text-red-400';
                    h+='<div class="'+colorClass+' border rounded p-2 text-center text-xs">';
                    h+='<span class="font-bold">$'+p.quota+'</span><br>';
                    h+='<span>库存:'+p.stock+' | '+p.probability+'%</span></div>';
                });
                h+='</div></div>';
            }
            
            document.getElementById('statsBox').innerHTML=h;
            
            var rh='';d.recent_claims.forEach(c=>{var autoTag=c.auto_redeemed?'<span class="text-green-400">[自动]</span>':'';rh+='<div class="bg-gray-800/50 p-2 rounded text-gray-400"><span class="text-blue-400">ID:'+c.user_id+'</span> '+c.username+' <span class="text-green-400">$'+c.quota+'</span> '+autoTag+'<br><span class="text-gray-600">'+c.time+'</span></div>';});
            document.getElementById('recentBox').innerHTML=rh||'<p class="text-gray-600">暂无</p>';
        });
    }
    </script>
</body>
</html>'''

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))











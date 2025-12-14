from fastapi import FastAPI, HTTPException, Request, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Float, Text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from datetime import datetime, timedelta, timezone
import httpx
import random
import os
import json

# ============ 配置 ============
NEW_API_URL = os.getenv("NEW_API_URL", "https://velvenode.zeabur.app")
COUPON_SITE_URL = os.getenv("COUPON_SITE_URL", "https://velvenodehome.zeabur.app")

# 时区配置：默认 UTC+8（中国时区）
TIMEZONE_OFFSET_HOURS = int(os.getenv("TIMEZONE_OFFSET_HOURS", "8"))
APP_TIMEZONE = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

# 持久化数据目录
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR}/coupon.db")

SITE_NAME = os.getenv("SITE_NAME", "velvenode")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# 默认配置（会被数据库配置覆盖）
DEFAULT_COOLDOWN_MINUTES = 480  # 8小时 = 480分钟
DEFAULT_CLAIM_TIMES = 1  # 每个冷却周期可领取次数
DEFAULT_QUOTA_WEIGHTS = {"1": 50, "5": 30, "10": 15, "50": 4, "100": 1}

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
    # 新增：记录该次领取时的冷却结束时间
    cooldown_expires_at = Column(DateTime, nullable=True)

class SystemConfig(Base):
    __tablename__ = "system_config"
    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(64), unique=True, nullable=False)
    config_value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

# 自动迁移：添加缺失的数据库字段
def auto_migrate():
    """自动添加缺失的数据库字段"""
    from sqlalchemy import text
    with engine.connect() as conn:
        try:
            result = conn.execute(text("PRAGMA table_info(claim_records)"))
            columns = [row[1] for row in result]
            
            if 'cooldown_expires_at' not in columns:
                conn.execute(text("ALTER TABLE claim_records ADD COLUMN cooldown_expires_at DATETIME"))
                conn.commit()
                print("✅ 已自动添加 cooldown_expires_at 字段")
        except Exception as e:
            print(f"迁移检查: {e}")

auto_migrate()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ============ 时间工具函数 ============
def now_utc():
    """获取当前 UTC 时间"""
    return datetime.now(timezone.utc)

def ensure_utc(dt: datetime) -> datetime:
    """确保 datetime 有 UTC 时区信息"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def format_local_time(dt: datetime) -> str:
    """将 UTC 时间转换为本地时间并格式化"""
    if dt is None:
        return ""
    dt_utc = ensure_utc(dt)
    dt_local = dt_utc.astimezone(APP_TIMEZONE)
    return dt_local.strftime("%Y-%m-%d %H:%M:%S")

# ============ 配置管理函数 ============
def get_config(db: Session, key: str, default=None):
    config = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if config:
        return config.config_value
    return default

def set_config(db: Session, key: str, value: str):
    config = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if config:
        config.config_value = value
        config.updated_at = now_utc()
    else:
        config = SystemConfig(config_key=key, config_value=value)
        db.add(config)
    db.commit()

def get_cooldown_minutes(db: Session) -> int:
    val = get_config(db, "cooldown_minutes")
    if val:
        try:
            return int(val)
        except:
            pass
    return DEFAULT_COOLDOWN_MINUTES

def get_claim_times(db: Session) -> int:
    val = get_config(db, "claim_times")
    if val:
        try:
            return max(1, int(val))
        except:
            pass
    return DEFAULT_CLAIM_TIMES

def get_quota_weights(db: Session) -> dict:
    val = get_config(db, "quota_weights")
    if val:
        try:
            return json.loads(val)
        except:
            pass
    return DEFAULT_QUOTA_WEIGHTS.copy()

def init_default_config(db: Session):
    if not get_config(db, "cooldown_minutes"):
        set_config(db, "cooldown_minutes", str(DEFAULT_COOLDOWN_MINUTES))
    if not get_config(db, "claim_times"):
        set_config(db, "claim_times", str(DEFAULT_CLAIM_TIMES))
    if not get_config(db, "quota_weights"):
        set_config(db, "quota_weights", json.dumps(DEFAULT_QUOTA_WEIGHTS))

# 初始化默认配置
with SessionLocal() as db:
    init_default_config(db)

app = FastAPI(title="兑换券系统")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ============ 用户验证函数 ============
async def verify_user_identity(user_id: int, username: str, api_key: str) -> bool:
    """验证用户身份 - 必须是有效的 API Key"""
    
    # 1. 基本格式检查
    if not api_key or not api_key.startswith('sk-') or len(api_key) < 20:
        return False
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{NEW_API_URL}/v1/models",
                headers={"Authorization": f"Bearer {api_key}"}
            )
            
            # 不管状态码，检查返回内容
            try:
                data = response.json()
                
                # 如果返回包含 error 字段，说明验证失败
                if isinstance(data, dict) and 'error' in data:
                    return False
                
                # 如果返回包含 data 字段且是列表，说明验证成功
                if isinstance(data, dict) and 'data' in data and isinstance(data['data'], list):
                    return True
                
                # 有些 API 直接返回模型列表
                if isinstance(data, list) and len(data) > 0:
                    return True
                
                return False
            except:
                return False
                
    except Exception as e:
        print(f"验证失败: {e}")
        return False

def get_random_coupon(db: Session):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).all()
    if not available:
        return None
    
    quota_weights = get_quota_weights(db)
    
    by_quota = {}
    for c in available:
        q = c.quota_dollars
        if q not in by_quota:
            by_quota[q] = []
        by_quota[q].append(c)
    
    choices, weights = [], []
    for quota, coupons in by_quota.items():
        quota_str = str(quota)
        weight = None
        
        for key in [quota_str, str(int(quota)) if quota == int(quota) else None]:
            if key and key in quota_weights:
                weight = float(quota_weights[key])
                break
        
        if weight is None:
            weight = max(0.1, 100 / quota)
        
        choices.append((quota, coupons))
        weights.append(weight)
    
    if not choices:
        return None
    
    selected = random.choices(choices, weights=weights, k=1)[0]
    return random.choice(selected[1])

def format_cooldown(minutes: int) -> str:
    """格式化冷却时间显示"""
    if minutes >= 60:
        h = minutes // 60
        m = minutes % 60
        if m > 0:
            return f"{h}小时{m}分钟"
        return f"{h}小时"
    return f"{minutes}分钟"

def calculate_user_cooldown_status(db: Session, user_id: int, now: datetime):
    """
    计算用户的冷却状态
    
    返回: (can_claim, remaining_claims, cooldown_seconds, recent_claims)
    
    逻辑：
    1. 获取用户最近的领取记录
    2. 对于每条记录，计算两个可能的冷却结束时间：
       a. 记录中存储的 cooldown_expires_at（如果存在）
       b. claim_time + 当前配置的 cooldown_minutes
    3. 取两者中的较小值作为实际冷却结束时间
    4. 这样：如果管理员缩短冷却时间，用户立即受益；如果延长，不影响已有记录
    """
    cooldown_minutes = get_cooldown_minutes(db)
    claim_times = get_claim_times(db)
    
    # 获取用户所有可能在冷却期内的记录（取最大可能范围）
    # 使用较大的时间窗口来获取记录，然后在代码中精确计算
    max_lookback = now - timedelta(minutes=cooldown_minutes * 2)  # 2倍冷却时间作为安全边界
    
    recent_claims = db.query(ClaimRecord).filter(
        ClaimRecord.user_id == user_id,
        ClaimRecord.claim_time >= max_lookback
    ).order_by(ClaimRecord.claim_time.desc()).all()
    
    # 计算哪些记录仍在冷却期内
    active_claims = []
    for claim in recent_claims:
        claim_time = ensure_utc(claim.claim_time)
        
        # 方案1：使用当前配置计算的冷却结束时间
        config_expires = claim_time + timedelta(minutes=cooldown_minutes)
        
        # 方案2：使用记录中存储的冷却结束时间（如果存在）
        stored_expires = ensure_utc(claim.cooldown_expires_at) if claim.cooldown_expires_at else None
        
        # 取较小值（对用户更有利）
        if stored_expires:
            actual_expires = min(config_expires, stored_expires)
        else:
            actual_expires = config_expires
        
        # 如果还在冷却期内，加入活跃列表
        if now < actual_expires:
            active_claims.append({
                'claim': claim,
                'expires_at': actual_expires
            })
    
    claims_in_period = len(active_claims)
    remaining_claims = max(0, claim_times - claims_in_period)
    
    can_claim = True
    cooldown_seconds = 0
    
    if claims_in_period >= claim_times and active_claims:
        # 找到最早过期的那条记录
        earliest_expiry = min(c['expires_at'] for c in active_claims)
        
        if now < earliest_expiry:
            can_claim = False
            remaining_delta = earliest_expiry - now
            cooldown_seconds = int(remaining_delta.total_seconds())
    
    return can_claim, remaining_claims, cooldown_seconds, recent_claims

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
        raise HTTPException(status_code=401, detail="API Key 无效或已过期，请检查后重试")
    
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
    
    claim_times = get_claim_times(db)
    now = now_utc()
    
    can_claim, remaining_claims, cooldown_seconds, _ = calculate_user_cooldown_status(db, user_id, now)
    
    cooldown_text = None
    if not can_claim and cooldown_seconds > 0:
        h = cooldown_seconds // 3600
        m = (cooldown_seconds % 3600) // 60
        s = cooldown_seconds % 60
        if h > 0:
            cooldown_text = f"{h}小时 {m}分钟 {s}秒"
        else:
            cooldown_text = f"{m}分钟 {s}秒"
    
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
            "remaining_claims": remaining_claims,
            "claim_times": claim_times,
            "history": [
                {
                    "coupon_code": r.coupon_code,
                    "quota": r.quota_dollars,
                    "claim_time": r.claim_time.isoformat() if r.claim_time else ""
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
    
    cooldown_minutes = get_cooldown_minutes(db)
    claim_times = get_claim_times(db)
    now = now_utc()
    
    can_claim, remaining_claims, cooldown_seconds, _ = calculate_user_cooldown_status(db, user_id, now)
    
    if not can_claim:
        total_min = cooldown_seconds // 60
        if total_min >= 60:
            h = total_min // 60
            m = total_min % 60
            raise HTTPException(status_code=400, detail=f"冷却中，请在 {h}小时 {m}分钟 后再试")
        else:
            raise HTTPException(status_code=400, detail=f"冷却中，请在 {total_min}分钟 后再试")
    
    coupon = get_random_coupon(db)
    if not coupon:
        raise HTTPException(status_code=400, detail="兑换码已领完")
    
    coupon.is_claimed = True
    coupon.claimed_by_user_id = user_id
    coupon.claimed_by_username = username
    coupon.claimed_at = now
    
    # 计算并存储冷却结束时间
    cooldown_expires = now + timedelta(minutes=cooldown_minutes)
    
    record = ClaimRecord(
        user_id=user_id,
        username=username,
        coupon_code=coupon.coupon_code,
        quota_dollars=coupon.quota_dollars,
        claim_time=now,
        cooldown_expires_at=cooldown_expires  # 存储冷却结束时间
    )
    db.add(record)
    db.commit()
    
    # 返回剩余次数（领取后减1）
    new_remaining = remaining_claims - 1
    
    return {"success": True, "data": {"coupon_code": coupon.coupon_code, "quota": coupon.quota_dollars, "remaining_claims": new_remaining}}

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

@app.get("/api/admin/coupons")
async def get_coupons(password: str, page: int = 1, per_page: int = 20, status: str = "all", search: str = "", db: Session = Depends(get_db)):
    """获取兑换码列表"""
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
                    "created_at": format_local_time(c.created_at) if c.created_at else None
                } for c in coupons
            ]
        }
    }

@app.post("/api/admin/delete-coupon")
async def delete_coupon(request: Request, db: Session = Depends(get_db)):
    """删除单个兑换码"""
    body = await request.json()
    password = body.get("password", "")
    coupon_id = body.get("id")
    
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    coupon = db.query(CouponPool).filter(CouponPool.id == coupon_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="兑换码不存在")
    
    db.delete(coupon)
    db.commit()
    
    return {"success": True, "message": "删除成功"}

@app.post("/api/admin/delete-coupons-batch")
async def delete_coupons_batch(request: Request, db: Session = Depends(get_db)):
    """批量删除兑换码"""
    body = await request.json()
    password = body.get("password", "")
    ids = body.get("ids", [])
    delete_type = body.get("type", "selected")
    
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
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

@app.post("/api/admin/update-coupon")
async def update_coupon(request: Request, db: Session = Depends(get_db)):
    """更新兑换码信息"""
    body = await request.json()
    password = body.get("password", "")
    coupon_id = body.get("id")
    
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    coupon = db.query(CouponPool).filter(CouponPool.id == coupon_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="兑换码不存在")
    
    if "quota" in body:
        coupon.quota_dollars = float(body["quota"])
    if "code" in body:
        coupon.coupon_code = body["code"]
    
    db.commit()
    
    return {"success": True, "message": "更新成功"}

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
    
    cooldown_minutes = get_cooldown_minutes(db)
    claim_times = get_claim_times(db)
    quota_weights = get_quota_weights(db)
    
    return {
        "success": True,
        "data": {
            "total": total,
            "available": available,
            "claimed": claimed,
            "quota_stats": quota_stats,
            "cooldown_minutes": cooldown_minutes,
            "claim_times": claim_times,
            "quota_weights": quota_weights,
            "timezone_offset": TIMEZONE_OFFSET_HOURS,
            "recent_claims": [
                {
                    "user_id": r.user_id,
                    "username": r.username,
                    "quota": r.quota_dollars,
                    "code": r.coupon_code[:8] + "...",
                    "time": format_local_time(r.claim_time)
                } for r in recent
            ]
        }
    }

@app.post("/api/admin/update-config")
async def update_config(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    password = body.get("password", "")
    
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    
    if "cooldown_minutes" in body:
        try:
            minutes = int(body["cooldown_minutes"])
            if minutes < 1:
                raise HTTPException(status_code=400, detail="冷却时间至少为1分钟")
            set_config(db, "cooldown_minutes", str(minutes))
        except ValueError:
            raise HTTPException(status_code=400, detail="冷却时间必须是整数")
    
    if "claim_times" in body:
        try:
            times = int(body["claim_times"])
            if times < 1:
                raise HTTPException(status_code=400, detail="领取次数至少为1次")
            set_config(db, "claim_times", str(times))
        except ValueError:
            raise HTTPException(status_code=400, detail="领取次数必须是整数")
    
    if "quota_weights" in body:
        weights = body["quota_weights"]
        if not isinstance(weights, dict):
            raise HTTPException(status_code=400, detail="概率配置格式错误")
        for k, v in weights.items():
            if not isinstance(v, (int, float)) or v < 0:
                raise HTTPException(status_code=400, detail=f"概率权重必须是非负数: {k}={v}")
        set_config(db, "quota_weights", json.dumps(weights))
    
    return {"success": True, "message": "配置已更新"}

@app.get("/api/stats/public")
async def get_public_stats(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    cooldown_minutes = get_cooldown_minutes(db)
    claim_times = get_claim_times(db)
    return {
        "available": available, 
        "cooldown_minutes": cooldown_minutes,
        "cooldown_text": format_cooldown(cooldown_minutes),
        "claim_times": claim_times
    }

# ============ 页面路由 ============
@app.get("/", response_class=HTMLResponse)
async def index(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    cooldown_minutes = get_cooldown_minutes(db)
    claim_times = get_claim_times(db)
    html = HOME_PAGE
    html = html.replace("{{AVAILABLE}}", str(available))
    html = html.replace("{{SITE_NAME}}", SITE_NAME)
    html = html.replace("{{NEW_API_URL}}", NEW_API_URL)
    html = html.replace("{{COOLDOWN_TEXT}}", format_cooldown(cooldown_minutes))
    html = html.replace("{{CLAIM_TIMES}}", str(claim_times))
    html = html.replace("{{COUPON_SITE_URL}}", COUPON_SITE_URL)
    return html

@app.get("/claim", response_class=HTMLResponse)
async def claim_page(db: Session = Depends(get_db)):
    available = db.query(CouponPool).filter(CouponPool.is_claimed == False).count()
    cooldown_minutes = get_cooldown_minutes(db)
    claim_times = get_claim_times(db)
    html = CLAIM_PAGE
    html = html.replace("{{AVAILABLE}}", str(available))
    html = html.replace("{{SITE_NAME}}", SITE_NAME)
    html = html.replace("{{NEW_API_URL}}", NEW_API_URL)
    html = html.replace("{{COOLDOWN_TEXT}}", format_cooldown(cooldown_minutes))
    html = html.replace("{{CLAIM_TIMES}}", str(claim_times))
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
        .btn-console{background:linear-gradient(135deg,#8b5cf6,#6d28d9);color:#fff}.btn-console:hover{opacity:0.9}
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
                <a href="{{NEW_API_URL}}/console" target="_blank" class="btn btn-console text-base">🖥️ 控制台</a>
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
                        <p class="text-gray-400 mb-3">每 <span id="cd-text">{{COOLDOWN_TEXT}}</span> 可领取 <span id="claim-times">{{CLAIM_TIMES}}</span> 次，随机获得对应额度的兑换码</p>
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
                    <ul class="text-gray-400 text-sm space-y-1"><li>• 请勿分享API Key</li><li>• 违规将被封禁</li><li>• 额度用完使用兑换码</li></ul>
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
        (function(){
            var xhr = new XMLHttpRequest();
            xhr.open('GET', '/api/stats/public', true);
            xhr.onreadystatechange = function(){
                if(xhr.readyState === 4 && xhr.status === 200){
                    try{
                        var d = JSON.parse(xhr.responseText);
                        document.getElementById('avail-cnt').textContent = d.available;
                        document.getElementById('cd-text').textContent = d.cooldown_text;
                        document.getElementById('claim-times').textContent = d.claim_times;
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
                    <div class="flex items-center gap-2">
                        <span id="remainBadge" class="px-2 py-1 rounded text-xs bg-purple-900/50 text-purple-400 border border-purple-700"></span>
                        <span id="badge" class="px-3 py-1 rounded-full text-sm"></span>
                    </div>
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
        每 <span id="cd-text">{{COOLDOWN_TEXT}}</span> 可领取 <span id="claim-times">{{CLAIM_TIMES}}</span> 次 | <a href="{{NEW_API_URL}}/" class="text-blue-400 hover:underline">返回首页</a> | <a href="{{NEW_API_URL}}/console/topup" target="_blank" class="text-blue-400 hover:underline">钱包充值</a>
    </footer>

    <script>
    var ud = null;
    var NEW_API_URL = '{{NEW_API_URL}}';

    (function(){
        var s = localStorage.getItem('coupon_user');
        if(s){ try{ ud = JSON.parse(s); document.getElementById('uid').value = ud.user_id||''; document.getElementById('uname').value = ud.username||''; document.getElementById('ukey').value = ud.api_key||''; }catch(e){} }
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/stats/public', true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4 && xhr.status === 200){
                try{
                    var d = JSON.parse(xhr.responseText);
                    document.getElementById('cnt').textContent = d.available;
                    document.getElementById('cd-text').textContent = d.cooldown_text;
                    document.getElementById('claim-times').textContent = d.claim_times;
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
        var remainBadge = document.getElementById('remainBadge');
        var msg = document.getElementById('cdMsg');
        
        remainBadge.textContent = '剩余 ' + d.remaining_claims + '/' + d.claim_times + ' 次';
        
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
        .ipt{background:#0d0d12;border:1px solid #1f1f2e;color:#e0e0e0;border-radius:8px;padding:10px 14px;width:100%}
        .ipt:focus{border-color:#3b82f6;outline:none}
        .btn{padding:10px 20px;border-radius:8px;font-weight:600;border:none;cursor:pointer}
        .btn-blue{background:#3b82f6;color:#fff}.btn-blue:hover{background:#2563eb}
        .btn-green{background:#10b981;color:#fff}.btn-green:hover{background:#059669}
        .btn-purple{background:#8b5cf6;color:#fff}.btn-purple:hover{background:#7c3aed}
        .btn-red{background:#ef4444;color:#fff}.btn-red:hover{background:#dc2626}
        .btn-gray{background:#374151;color:#fff}.btn-gray:hover{background:#4b5563}
        #overlay{position:fixed;inset:0;background:rgba(0,0,0,0.95);display:flex;align-items:center;justify-content:center;z-index:100}
        #toast{position:fixed;top:20px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;color:#fff;z-index:200;display:none}
        .tab-btn{padding:8px 16px;border-radius:8px;cursor:pointer;transition:all .2s}
        .tab-btn.active{background:#3b82f6;color:#fff}
        .tab-btn:not(.active){background:#1f1f2e;color:#9ca3af}
        .tab-btn:not(.active):hover{background:#2a2a3a}
        .coupon-row{display:grid;grid-template-columns:1fr 80px 100px 120px 80px;gap:8px;padding:10px;border-bottom:1px solid #1f1f2e;align-items:center}
        .coupon-row:hover{background:#1a1a24}
        .weight-row{display:flex;align-items:center;gap:8px;padding:8px;background:#1a1a24;border-radius:8px;margin-bottom:8px}
    </style>
</head>
<body class="min-h-screen">
    <div id="overlay">
        <div class="card p-8 w-full max-w-sm mx-4">
            <div class="text-center mb-6">
                <div class="text-4xl mb-2">🔐</div>
                <h1 class="text-xl font-bold">管理后台</h1>
            </div>
            <input type="password" id="loginPwd" class="ipt mb-4" placeholder="管理员密码">
            <button type="button" class="btn btn-blue w-full" onclick="doLogin()">登录</button>
            <a href="/" class="block text-center text-gray-500 text-sm mt-4 hover:text-blue-400">← 返回首页</a>
            <p id="loginError" class="text-red-500 text-center text-sm mt-2" style="display:none"></p>
        </div>
    </div>

    <div id="adminMain" style="display:none">
        <nav class="border-b border-gray-800 py-4 px-6">
            <div class="max-w-7xl mx-auto flex justify-between items-center">
                <h1 class="font-bold text-xl">🔧 管理后台</h1>
                <div class="flex items-center gap-4">
                    <a href="/" class="text-gray-400 hover:text-white text-sm">← 首页</a>
                    <button type="button" class="text-red-400 text-sm" onclick="doLogout()">退出</button>
                </div>
            </div>
        </nav>

        <!-- 标签页导航 -->
        <div class="max-w-7xl mx-auto px-4 py-4">
            <div class="flex gap-2 flex-wrap">
                <button class="tab-btn active" onclick="switchTab('overview')">📊 总览</button>
                <button class="tab-btn" onclick="switchTab('coupons')">🎫 兑换码管理</button>
                <button class="tab-btn" onclick="switchTab('add')">➕ 添加兑换码</button>
                <button class="tab-btn" onclick="switchTab('config')">⚙️ 系统配置</button>
            </div>
        </div>

        <main class="max-w-7xl mx-auto px-4 pb-8">
            <!-- 总览 -->
            <div id="tab-overview" class="tab-content">
                <div class="grid lg:grid-cols-3 gap-6">
                    <div class="lg:col-span-2">
                        <div class="card p-6">
                            <h2 class="font-semibold mb-4">📊 统计数据</h2>
                            <div id="statsBox">加载中...</div>
                        </div>
                    </div>
                    <div>
                        <div class="card p-6">
                            <h2 class="font-semibold mb-4">📋 最近领取</h2>
                            <div id="recentBox" class="max-h-96 overflow-y-auto space-y-2 text-sm"></div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 兑换码管理 -->
            <div id="tab-coupons" class="tab-content" style="display:none">
                <div class="card p-6">
                    <div class="flex flex-wrap justify-between items-center gap-4 mb-4">
                        <h2 class="font-semibold">🎫 兑换码列表</h2>
                        <div class="flex gap-2 flex-wrap">
                            <select id="couponStatus" class="ipt w-auto" onchange="loadCoupons(1)">
                                <option value="all">全部</option>
                                <option value="available">可用</option>
                                <option value="claimed">已领取</option>
                            </select>
                            <input type="text" id="couponSearch" class="ipt w-40" placeholder="搜索兑换码..." onkeyup="if(event.key==='Enter')loadCoupons(1)">
                            <button class="btn btn-blue" onclick="loadCoupons(1)">搜索</button>
                        </div>
                    </div>
                    
                    <!-- 批量操作 -->
                    <div class="flex gap-2 mb-4 flex-wrap">
                        <button class="btn btn-gray text-sm" onclick="selectAllCoupons()">全选当页</button>
                        <button class="btn btn-red text-sm" onclick="deleteSelected()">删除选中</button>
                        <button class="btn btn-red text-sm" onclick="deleteBatch('all_claimed')">删除所有已领取</button>
                        <button class="btn btn-red text-sm" onclick="deleteBatch('all_available')">删除所有可用</button>
                    </div>
                    
                    <!-- 表头 -->
                    <div class="coupon-row text-gray-500 text-sm font-semibold border-b-2 border-gray-700">
                        <div class="flex items-center gap-2"><input type="checkbox" id="selectAllCheck" onchange="toggleSelectAll()"> 兑换码</div>
                        <div>额度</div>
                        <div>状态</div>
                        <div>领取信息</div>
                        <div>操作</div>
                    </div>
                    
                    <div id="couponList"></div>
                    
                    <!-- 分页 -->
                    <div id="pagination" class="flex justify-center gap-2 mt-4"></div>
                </div>
            </div>

            <!-- 添加兑换码 -->
            <div id="tab-add" class="tab-content" style="display:none">
                <div class="card p-6">
                    <h2 class="font-semibold mb-4">➕ 添加兑换码</h2>
                    <div class="grid grid-cols-5 gap-2 mb-4">
                        <button type="button" onclick="setQuota(1)" class="bg-green-900/50 text-green-400 border border-green-700 py-2 rounded font-bold hover:opacity-80">$1</button>
                        <button type="button" onclick="setQuota(5)" class="bg-blue-900/50 text-blue-400 border border-blue-700 py-2 rounded font-bold hover:opacity-80">$5</button>
                        <button type="button" onclick="setQuota(10)" class="bg-purple-900/50 text-purple-400 border border-purple-700 py-2 rounded font-bold hover:opacity-80">$10</button>
                        <button type="button" onclick="setQuota(50)" class="bg-orange-900/50 text-orange-400 border border-orange-700 py-2 rounded font-bold hover:opacity-80">$50</button>
                        <button type="button" onclick="setQuota(100)" class="bg-red-900/50 text-red-400 border border-red-700 py-2 rounded font-bold hover:opacity-80">$100</button>
                    </div>
                    <div class="flex items-center gap-2 mb-4">
                        <span class="text-gray-400">额度:</span>
                        <input type="number" id="quotaVal" value="1" step="0.01" min="0.01" class="w-24 ipt text-center font-bold">
                        <span class="text-gray-400">美元（支持小数）</span>
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm text-gray-400 mb-2">上传TXT文件</label>
                        <input type="file" id="txtFile" accept=".txt" class="ipt">
                    </div>
                    <button type="button" class="btn btn-blue w-full mb-4" onclick="doUpload()">上传文件</button>
                    <hr class="border-gray-700 my-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-2">或手动粘贴（每行一个）</label>
                        <textarea id="codesText" rows="6" class="ipt font-mono text-sm" placeholder="每行一个兑换码"></textarea>
                    </div>
                    <button type="button" class="btn btn-green w-full mt-3" onclick="doAddCodes()">添加兑换码</button>
                </div>
            </div>

            <!-- 系统配置 -->
            <div id="tab-config" class="tab-content" style="display:none">
                <div class="grid lg:grid-cols-2 gap-6">
                    <!-- 冷却时间配置 -->
                    <div class="card p-6">
                        <h2 class="font-semibold mb-4">⏱️ 冷却时间设置</h2>
                        <div class="space-y-4">
                            <div>
                                <label class="block text-sm text-gray-400 mb-2">冷却时间</label>
                                <div class="flex items-center gap-2">
                                    <input type="number" id="cooldownMinutes" min="1" class="w-24 ipt text-center font-bold">
                                    <span class="text-gray-400">分钟</span>
                                </div>
                                <p class="text-xs text-gray-500 mt-1">提示：60分钟=1小时，480分钟=8小时</p>
                            </div>
                            <div>
                                <label class="block text-sm text-gray-400 mb-2">每周期可领取次数</label>
                                <div class="flex items-center gap-2">
                                    <input type="number" id="claimTimes" min="1" max="100" class="w-24 ipt text-center font-bold">
                                    <span class="text-gray-400">次</span>
                                </div>
                                <p class="text-xs text-gray-500 mt-1">用户在冷却时间内可领取的次数</p>
                            </div>
                            <div class="bg-yellow-900/20 border border-yellow-800 rounded p-3 text-sm text-yellow-400">
                                <p>💡 <b>冷却时间调整说明：</b></p>
                                <p class="mt-1 text-yellow-500">• 缩短冷却时间：用户立即受益，冷却时间减少</p>
                                <p class="text-yellow-500">• 延长冷却时间：不影响已有用户的冷却，只对新领取生效</p>
                            </div>
                            <button class="btn btn-blue w-full" onclick="saveCooldownConfig()">保存冷却配置</button>
                        </div>
                    </div>

                    <!-- 概率配置 -->
                    <div class="card p-6">
                        <h2 class="font-semibold mb-4">🎰 概率权重设置</h2>
                        <p class="text-xs text-gray-500 mb-4">权重越大概率越高，支持小数。例如：权重0.1表示极低概率</p>
                        <div id="weightsContainer" class="max-h-64 overflow-y-auto mb-4"></div>
                        <div class="flex gap-2 mb-4">
                            <input type="number" id="newQuotaKey" step="0.01" placeholder="额度" class="w-24 ipt text-center text-sm">
                            <input type="number" id="newQuotaWeight" step="0.01" placeholder="权重" class="w-24 ipt text-center text-sm">
                            <button class="btn btn-green" onclick="addWeight()">添加</button>
                        </div>
                        <button class="btn btn-purple w-full" onclick="saveWeights()">保存概率配置</button>
                    </div>
                </div>
            </div>
        </main>
    </div>

    <div id="toast"></div>

    <script>
    var adminPwd = '';
    var currentWeights = {};
    var selectedCoupons = new Set();
    var currentPage = 1;

    (function(){
        var saved = sessionStorage.getItem('admin_pwd');
        if(saved){ adminPwd = saved; verifyAndShow(); }
        document.getElementById('loginPwd').addEventListener('keydown', function(e){ if(e.key === 'Enter') doLogin(); });
    })();

    function toast(msg, ok){
        var t = document.getElementById('toast');
        t.textContent = msg; t.style.display = 'block'; t.style.background = ok ? '#10b981' : '#ef4444';
        setTimeout(function(){ t.style.display = 'none'; }, 3000);
    }

    function doLogin(){
        var pwd = document.getElementById('loginPwd').value;
        var errEl = document.getElementById('loginError');
        if(!pwd){ errEl.textContent = '请输入密码'; errEl.style.display = 'block'; return; }
        errEl.style.display = 'none';
        
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/login', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                if(xhr.status === 200){
                    adminPwd = pwd; sessionStorage.setItem('admin_pwd', pwd);
                    document.getElementById('overlay').style.display = 'none';
                    document.getElementById('adminMain').style.display = 'block';
                    loadStats();
                } else { errEl.textContent = '密码错误'; errEl.style.display = 'block'; }
            }
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
                } else { sessionStorage.removeItem('admin_pwd'); adminPwd = ''; }
            }
        };
        xhr.send();
    }

    function doLogout(){ sessionStorage.removeItem('admin_pwd'); adminPwd = ''; location.reload(); }

    function switchTab(tab){
        document.querySelectorAll('.tab-content').forEach(function(el){ el.style.display = 'none'; });
        document.querySelectorAll('.tab-btn').forEach(function(el){ el.classList.remove('active'); });
        document.getElementById('tab-' + tab).style.display = 'block';
        event.target.classList.add('active');
        
        if(tab === 'coupons') loadCoupons(1);
        if(tab === 'overview') loadStats();
    }

    function setQuota(q){ document.getElementById('quotaVal').value = q; }

    function doUpload(){
        var q = document.getElementById('quotaVal').value;
        var f = document.getElementById('txtFile').files[0];
        if(!f){ toast('请选择文件', false); return; }
        var fd = new FormData(); fd.append('password', adminPwd); fd.append('quota', q); fd.append('file', f);
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/upload-txt', true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{ var res = JSON.parse(xhr.responseText); toast(res.message || res.detail, xhr.status === 200);
                if(xhr.status === 200){ loadStats(); document.getElementById('txtFile').value = ''; } }catch(e){ toast('失败', false); }
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
                try{ var res = JSON.parse(xhr.responseText); toast(res.message || res.detail, xhr.status === 200);
                if(xhr.status === 200){ loadStats(); document.getElementById('codesText').value = ''; } }catch(e){ toast('失败', false); }
            }
        };
        xhr.send(JSON.stringify({password: adminPwd, quota: q, coupons: arr}));
    }

    // 兑换码管理
    function loadCoupons(page){
        currentPage = page;
        selectedCoupons.clear();
        var status = document.getElementById('couponStatus').value;
        var search = document.getElementById('couponSearch').value;
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/admin/coupons?password=' + encodeURIComponent(adminPwd) + '&page=' + page + '&status=' + status + '&search=' + encodeURIComponent(search), true);
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4 && xhr.status === 200){
                try{
                    var res = JSON.parse(xhr.responseText);
                    if(res.success) renderCoupons(res.data);
                }catch(e){}
            }
        };
        xhr.send();
    }

    function renderCoupons(data){
        var html = '';
        data.coupons.forEach(function(c){
            var statusClass = c.is_claimed ? 'text-gray-500' : 'text-green-400';
            var statusText = c.is_claimed ? '已领取' : '可用';
            html += '<div class="coupon-row">';
            html += '<div class="flex items-center gap-2"><input type="checkbox" data-id="'+c.id+'" onchange="toggleSelect('+c.id+')"> <span class="font-mono text-sm truncate">'+c.code+'</span></div>';
            html += '<div class="text-blue-400 font-bold">$'+c.quota+'</div>';
            html += '<div class="'+statusClass+'">'+statusText+'</div>';
            html += '<div class="text-xs text-gray-500">'+(c.claimed_by ? c.claimed_by + '<br>' + c.claimed_at : '-')+'</div>';
            html += '<div><button class="text-red-400 hover:text-red-300 text-sm" onclick="deleteCoupon('+c.id+')">删除</button></div>';
            html += '</div>';
        });
        document.getElementById('couponList').innerHTML = html || '<p class="text-gray-500 text-center py-4">暂无数据</p>';
        
        // 分页
        var phtml = '';
        for(var i = 1; i <= data.pages; i++){
            phtml += '<button class="px-3 py-1 rounded '+(i===data.page?'bg-blue-600':'bg-gray-700')+'" onclick="loadCoupons('+i+')">'+i+'</button>';
        }
        document.getElementById('pagination').innerHTML = phtml;
    }

    function toggleSelect(id){
        if(selectedCoupons.has(id)) selectedCoupons.delete(id);
        else selectedCoupons.add(id);
    }

    function toggleSelectAll(){
        var checked = document.getElementById('selectAllCheck').checked;
        document.querySelectorAll('#couponList input[type=checkbox]').forEach(function(cb){
            cb.checked = checked;
            var id = parseInt(cb.dataset.id);
            if(checked) selectedCoupons.add(id);
            else selectedCoupons.delete(id);
        });
    }

    function selectAllCoupons(){
        document.getElementById('selectAllCheck').checked = true;
        toggleSelectAll();
    }

    function deleteCoupon(id){
        if(!confirm('确定删除此兑换码？')) return;
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/delete-coupon', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{ var res = JSON.parse(xhr.responseText); toast(res.message || res.detail, xhr.status === 200);
                if(xhr.status === 200) loadCoupons(currentPage); }catch(e){ toast('失败', false); }
            }
        };
        xhr.send(JSON.stringify({password: adminPwd, id: id}));
    }

    function deleteSelected(){
        if(selectedCoupons.size === 0){ toast('请先选择兑换码', false); return; }
        if(!confirm('确定删除选中的 ' + selectedCoupons.size + ' 个兑换码？')) return;
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/delete-coupons-batch', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{ var res = JSON.parse(xhr.responseText); toast(res.message || res.detail, xhr.status === 200);
                if(xhr.status === 200) loadCoupons(currentPage); }catch(e){ toast('失败', false); }
            }
        };
        xhr.send(JSON.stringify({password: adminPwd, ids: Array.from(selectedCoupons), type: 'selected'}));
    }

    function deleteBatch(type){
        var msg = type === 'all_claimed' ? '所有已领取的兑换码' : '所有可用的兑换码';
        if(!confirm('确定删除' + msg + '？此操作不可恢复！')) return;
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/delete-coupons-batch', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{ var res = JSON.parse(xhr.responseText); toast(res.message || res.detail, xhr.status === 200);
                if(xhr.status === 200) loadCoupons(1); }catch(e){ toast('失败', false); }
            }
        };
        xhr.send(JSON.stringify({password: adminPwd, type: type}));
    }

    // 配置管理
    function renderWeights(weights){
        currentWeights = {};
        for(var k in weights) currentWeights[k] = weights[k];
        
        var container = document.getElementById('weightsContainer');
        var html = '';
        var sortedKeys = Object.keys(currentWeights).sort(function(a,b){ return parseFloat(a) - parseFloat(b); });
        
        sortedKeys.forEach(function(k){
            html += '<div class="weight-row">';
            html += '<span class="text-blue-400 font-bold w-20">$'+k+'</span>';
            html += '<input type="number" step="0.01" min="0" value="'+currentWeights[k]+'" onchange="updateWeight(\\''+k+'\\', this.value)" class="w-20 ipt text-center text-sm">';
            html += '<span class="text-gray-500 text-sm">权重</span>';
            html += '<button onclick="removeWeight(\\''+k+'\\')" class="text-red-400 hover:text-red-300 ml-auto">✕</button>';
            html += '</div>';
        });
        container.innerHTML = html || '<p class="text-gray-500">暂无配置</p>';
    }

    function updateWeight(key, val){ currentWeights[key] = parseFloat(val) || 0; }
    function removeWeight(key){ delete currentWeights[key]; renderWeights(currentWeights); }

    function addWeight(){
        var key = document.getElementById('newQuotaKey').value;
        var val = document.getElementById('newQuotaWeight').value;
        if(!key || !val){ toast('请输入额度和权重', false); return; }
        currentWeights[key] = parseFloat(val);
        renderWeights(currentWeights);
        document.getElementById('newQuotaKey').value = '';
        document.getElementById('newQuotaWeight').value = '';
    }

    function saveCooldownConfig(){
        var minutes = parseInt(document.getElementById('cooldownMinutes').value);
        var times = parseInt(document.getElementById('claimTimes').value);
        if(!minutes || minutes < 1){ toast('冷却时间至少1分钟', false); return; }
        if(!times || times < 1){ toast('领取次数至少1次', false); return; }
        
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/update-config', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{ var res = JSON.parse(xhr.responseText); toast(res.message || res.detail, xhr.status === 200); }catch(e){ toast('失败', false); }
            }
        };
        xhr.send(JSON.stringify({password: adminPwd, cooldown_minutes: minutes, claim_times: times}));
    }

    function saveWeights(){
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/admin/update-config', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onreadystatechange = function(){
            if(xhr.readyState === 4){
                try{ var res = JSON.parse(xhr.responseText); toast(res.message || res.detail, xhr.status === 200); }catch(e){ toast('失败', false); }
            }
        };
        xhr.send(JSON.stringify({password: adminPwd, quota_weights: currentWeights}));
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
                        
                        document.getElementById('cooldownMinutes').value = d.cooldown_minutes || 480;
                        document.getElementById('claimTimes').value = d.claim_times || 1;
                        renderWeights(d.quota_weights || {});
                        
                        var cooldownText = d.cooldown_minutes >= 60 ? 
                            Math.floor(d.cooldown_minutes/60) + '小时' + (d.cooldown_minutes%60 > 0 ? d.cooldown_minutes%60 + '分钟' : '') : 
                            d.cooldown_minutes + '分钟';
                        
                        var h = '<div class="grid grid-cols-3 gap-4 mb-6">';
                        h += '<div class="bg-gray-800 p-4 rounded-lg text-center"><div class="text-2xl font-bold">'+d.total+'</div><div class="text-gray-500 text-sm">总数</div></div>';
                        h += '<div class="bg-green-900/30 p-4 rounded-lg text-center border border-green-800"><div class="text-2xl font-bold text-green-400">'+d.available+'</div><div class="text-gray-500 text-sm">可用</div></div>';
                        h += '<div class="bg-blue-900/30 p-4 rounded-lg text-center border border-blue-800"><div class="text-2xl font-bold text-blue-400">'+d.claimed+'</div><div class="text-gray-500 text-sm">已领</div></div>';
                        h += '</div>';
                        
                        var tzText = d.timezone_offset >= 0 ? 'UTC+' + d.timezone_offset : 'UTC' + d.timezone_offset;
                        h += '<div class="grid grid-cols-3 gap-4 mb-6">';
                        h += '<div class="bg-purple-900/30 p-3 rounded-lg border border-purple-800"><span class="text-purple-400">⏱️ 冷却时间: '+cooldownText+'</span></div>';
                        h += '<div class="bg-orange-900/30 p-3 rounded-lg border border-orange-800"><span class="text-orange-400">🎯 每周期可领: '+d.claim_times+'次</span></div>';
                        h += '<div class="bg-cyan-900/30 p-3 rounded-lg border border-cyan-800"><span class="text-cyan-400">🌍 时区: '+tzText+'</span></div>';
                        h += '</div>';
                        
                        h += '<div class="space-y-2">';
                        for(var k in d.quota_stats){
                            var v = d.quota_stats[k];
                            h += '<div class="flex justify-between text-sm bg-gray-800/50 p-3 rounded"><span class="font-bold">'+k+'</span><span class="text-green-400">可用: '+v.available+'</span><span class="text-gray-500">已领: '+v.claimed+'</span></div>';
                        }
                        h += '</div>';
                        document.getElementById('statsBox').innerHTML = h;

                        var rh = '';
                        d.recent_claims.forEach(function(c){
                            rh += '<div class="bg-gray-800/50 p-2 rounded text-gray-400"><span class="text-blue-400">ID:'+c.user_id+'</span> '+c.username+' <span class="text-green-400">$'+c.quota+'</span><br><span class="text-gray-600 text-xs">'+c.time+'</span></div>';
                        });
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
        .widget-header { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
        .widget-icon { font-size: 24px; }
        .widget-title { font-weight: 600; font-size: 14px; }
        .widget-stats { display: flex; justify-content: space-between; margin-bottom: 12px; font-size: 12px; color: #94a3b8; }
        .widget-count { color: #60a5fa; font-weight: 700; font-size: 18px; }
        .widget-btn {
            display: block; width: 100%;
            background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
            color: #fff; text-align: center; padding: 10px; border-radius: 8px;
            text-decoration: none; font-weight: 600; font-size: 14px; transition: all 0.2s;
        }
        .widget-btn:hover { opacity: 0.9; transform: translateY(-1px); }
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
        <a href="{{COUPON_SITE_URL}}/claim" target="_blank" class="widget-btn">🎁 免费领取 →</a>
    </div>
</body>
</html>'''

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

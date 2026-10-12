#!/usr/bin/env python3
"""매장 POS - 재고/고객/포인트/직원권한/결제·현금영수증/세금계산서.

표준 라이브러리만 사용. 설치할 패키지 없음.

    python pos.py --seed                                   # 샘플 데이터
    POS_PIN=... 는 더 이상 쓰지 않습니다. 직원 계정으로 로그인하세요.
    POS_ADMIN_PW=비번 python pos.py                         # 첫 실행 시 owner 계정 비번
    python pos.py --host 0.0.0.0 --port 8000               # 매장 LAN 공유
    POS_VAN_URL=http://127.0.0.1:9900/pay python pos.py     # 카드단말(VAN) 연동 시

ponytail: ThreadingHTTPServer + SQLite. 매장 한 곳(단말 수 대)까지는 충분.
여러 지점/수백 TPS가 되면 그때 uvicorn+Postgres로 옮기면 됨.
"""
import argparse, calendar, csv, glob, hashlib, io, json, math, os, re, secrets, sqlite3
import threading, time, urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("POS_DB", os.path.join(BASE, "pos.db"))
SESSIONS = {}  # token -> {"uid","name","role","exp"}
SESSION_HOURS = 12

# VAN(카드단말) 연동 — 매장 PC에 설치된 결제모듈 주소. 미설정이면 수기 승인번호 모드.
VAN_URL = os.environ.get("POS_VAN_URL")
VAN_TIMEOUT = int(os.environ.get("POS_VAN_TIMEOUT", "60"))

BACKUP_DIR = os.environ.get("POS_BACKUP_DIR", os.path.join(BASE, "backup"))
BACKUP_KEEP = int(os.environ.get("POS_BACKUP_KEEP", "14"))  # 보관 일수

ROLES = {"staff": 1, "manager": 2, "owner": 3}
ROLE_KO = {"staff": "직원", "manager": "매니저", "owner": "점주"}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  pw TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT,
  action TEXT NOT NULL, detail TEXT DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, barcode TEXT UNIQUE, name TEXT NOT NULL,
  cost INTEGER NOT NULL DEFAULT 0, price INTEGER NOT NULL,
  stock INTEGER NOT NULL DEFAULT 0, min_stock INTEGER NOT NULL DEFAULT 0,
  category TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, phone TEXT UNIQUE NOT NULL, name TEXT DEFAULT '',
  points INTEGER NOT NULL DEFAULT 0, memo TEXT DEFAULT '',
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stock_moves(
  id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  delta INTEGER NOT NULL, reason TEXT NOT NULL, sale_id INTEGER,
  note TEXT DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sales(
  id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
  subtotal INTEGER NOT NULL, discount INTEGER NOT NULL DEFAULT 0,
  point_used INTEGER NOT NULL DEFAULT 0, point_earned INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL, pay_method TEXT NOT NULL,
  at TEXT NOT NULL, voided_at TEXT);
CREATE TABLE IF NOT EXISTS sale_items(
  id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL REFERENCES sales(id),
  product_id INTEGER NOT NULL REFERENCES products(id), name TEXT NOT NULL,
  qty INTEGER NOT NULL, price INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS point_moves(
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  delta INTEGER NOT NULL, reason TEXT NOT NULL, sale_id INTEGER, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cash_receipts(
  id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL REFERENCES sales(id),
  kind TEXT NOT NULL, ident_no TEXT NOT NULL, amount INTEGER NOT NULL,
  approval_no TEXT DEFAULT '', status TEXT NOT NULL, message TEXT DEFAULT '',
  at TEXT NOT NULL, voided_at TEXT);
CREATE TABLE IF NOT EXISTS closings(
  id INTEGER PRIMARY KEY, d TEXT UNIQUE NOT NULL,
  opening_cash INTEGER NOT NULL, cash_in INTEGER NOT NULL, cash_out INTEGER NOT NULL,
  expected_cash INTEGER NOT NULL, counted_cash INTEGER NOT NULL, diff INTEGER NOT NULL,
  card_total INTEGER NOT NULL, transfer_total INTEGER NOT NULL,
  sales_total INTEGER NOT NULL, sales_count INTEGER NOT NULL,
  memo TEXT DEFAULT '', user_id INTEGER, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_sales_at ON sales(at);
CREATE INDEX IF NOT EXISTS ix_items_sale ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS ix_moves_pid ON stock_moves(product_id, at);
CREATE INDEX IF NOT EXISTS ix_pmoves_cid ON point_moves(customer_id, at);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit(at);
CREATE INDEX IF NOT EXISTS ix_cr_sale ON cash_receipts(sale_id);
INSERT OR IGNORE INTO settings(k,v) VALUES
  ('store_name','우리매장'), ('point_rate','5'), ('point_min_use','1000'),
  ('store_biz_no',''), ('store_ceo',''), ('store_addr',''), ('store_tel',''),
  ('store_item','의류 소매'), ('opening_cash','100000');
"""

# v1(PIN 버전) DB에서 올라올 때 채워 넣는 컬럼
MIGRATIONS = [
    ("sales", "user_id", "INTEGER"),
    ("sales", "approval_no", "TEXT DEFAULT ''"),
    ("sales", "card_name", "TEXT DEFAULT ''"),
    ("sales", "installment", "INTEGER DEFAULT 0"),
    ("sales", "void_user_id", "INTEGER"),
    ("stock_moves", "user_id", "INTEGER"),
    ("point_moves", "user_id", "INTEGER"),
    ("customers", "business_no", "TEXT DEFAULT ''"),
    ("customers", "biz_name", "TEXT DEFAULT ''"),
    ("customers", "biz_ceo", "TEXT DEFAULT ''"),
    ("customers", "biz_addr", "TEXT DEFAULT ''"),
    ("customers", "biz_item", "TEXT DEFAULT ''"),
    ("customers", "biz_email", "TEXT DEFAULT ''"),
    ("products", "style_code", "TEXT DEFAULT ''"),  # 의류: 품번 (색상/사이즈 묶는 단위)
    ("products", "color", "TEXT DEFAULT ''"),
    ("products", "size", "TEXT DEFAULT ''"),
]

_local = threading.local()


def db():
    c = getattr(_local, "c", None)
    if c is None:
        c = _local.c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
    return c


class Bad(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class tx:
    """쓰기 트랜잭션. 재고 차감과 포인트 적립이 따로 커밋되면 안 됨.
    ponytail: 네트워크 호출(VAN)은 절대 이 안에서 하지 않는다 - 락을 수십 초 잡는다."""

    def __enter__(self):
        self.c = db()
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, et, *a):
        self.c.execute("ROLLBACK" if et else "COMMIT")
        return False


def now():
    return datetime.now().isoformat(timespec="seconds")


def setting(k, cast=str):
    r = db().execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return cast(r["v"]) if r else None


def rows(sql, args=()):
    return [dict(r) for r in db().execute(sql, args)]


def as_int(v, name, lo=None, hi=None):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Bad(f"{name}: 숫자여야 합니다")
    if lo is not None and n < lo:
        raise Bad(f"{name}: {lo} 이상이어야 합니다")
    if hi is not None and n > hi:
        raise Bad(f"{name}: {hi} 이하여야 합니다")
    return n


def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if not (9 <= len(d) <= 11):
        raise Bad("전화번호 형식이 올바르지 않습니다")
    return d


def norm_biz_no(b):
    """사업자등록번호 10자리 + 국세청 체크섬."""
    d = re.sub(r"\D", "", b or "")
    if not d:
        return ""
    if len(d) != 10:
        raise Bad("사업자등록번호는 10자리입니다")
    w = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    s = sum(int(d[i]) * w[i] for i in range(9)) + (int(d[8]) * 5) // 10
    if (10 - s % 10) % 10 != int(d[9]):
        raise Bad("사업자등록번호가 올바르지 않습니다")
    return d


# ------------------------------------------------------------------ 계정/권한
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + "$" + h.hex()


def verify_pw(pw, stored):
    try:
        salt, _ = stored.split("$")
    except ValueError:
        return False
    return secrets.compare_digest(hash_pw(pw, bytes.fromhex(salt)), stored)


def me():
    u = getattr(_local, "user", None)
    if not u:
        raise Bad("로그인이 필요합니다", 401)
    return u


def need(role):
    if ROLES[me()["role"]] < ROLES[role]:
        raise Bad(f"{ROLE_KO[role]} 이상만 할 수 있습니다", 403)


def audit(c, action, detail=""):
    u = me()
    c.execute("INSERT INTO audit(user_id,username,action,detail,at) VALUES(?,?,?,?,?)",
              (u["uid"], u["name"], action, detail, now()))


def login(username, pw):
    u = db().execute("SELECT * FROM users WHERE username=? AND active=1",
                     ((username or "").strip(),)).fetchone()
    if not u or not verify_pw(pw or "", u["pw"]):
        raise Bad("아이디 또는 비밀번호가 올바르지 않습니다", 401)
    tok = secrets.token_hex(16)
    SESSIONS[tok] = {"uid": u["id"], "name": u["name"], "role": u["role"],
                     "exp": datetime.now() + timedelta(hours=SESSION_HOURS)}
    return tok, {"name": u["name"], "role": u["role"], "username": u["username"]}


def list_users(q):
    need("owner")
    return rows("SELECT id,username,name,role,active,created_at FROM users ORDER BY id")


def save_user(b):
    need("owner")
    un = (b.get("username") or "").strip().lower()
    name = (b.get("name") or "").strip()
    role = b.get("role")
    if role not in ROLES:
        raise Bad("권한이 올바르지 않습니다")
    if not name:
        raise Bad("이름은 필수입니다")
    pw = b.get("password") or ""
    try:
        with tx() as c:
            if b.get("id"):
                uid = as_int(b["id"], "id")
                old = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                if not old:
                    raise Bad("직원을 찾을 수 없습니다", 404)
                if old["role"] == "owner" and role != "owner" and c.execute(
                        "SELECT COUNT(*) n FROM users WHERE role='owner' AND active=1"
                ).fetchone()["n"] <= 1:
                    raise Bad("점주 계정이 최소 1개는 있어야 합니다")
                c.execute("UPDATE users SET name=?,role=? WHERE id=?", (name, role, uid))
                if pw:
                    if len(pw) < 6:
                        raise Bad("비밀번호는 6자 이상이어야 합니다")
                    c.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(pw), uid))
                audit(c, "직원수정", f"{old['username']} -> {name}/{ROLE_KO[role]}"
                                     + (" (비밀번호 변경)" if pw else ""))
            else:
                if not re.fullmatch(r"[a-z0-9_]{3,20}", un):
                    raise Bad("아이디는 영문소문자/숫자/_ 3~20자입니다")
                if len(pw) < 6:
                    raise Bad("비밀번호는 6자 이상이어야 합니다")
                uid = c.execute("INSERT INTO users(username,name,pw,role,created_at)"
                                " VALUES(?,?,?,?,?)",
                                (un, name, hash_pw(pw), role, now())).lastrowid
                audit(c, "직원등록", f"{un}/{name}/{ROLE_KO[role]}")
    except sqlite3.IntegrityError:
        raise Bad("이미 사용 중인 아이디입니다")
    return {"id": uid}


def toggle_user(b):
    need("owner")
    uid = as_int(b.get("id"), "id")
    if uid == me()["uid"]:
        raise Bad("본인 계정은 비활성화할 수 없습니다")
    with tx() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise Bad("직원을 찾을 수 없습니다", 404)
        new = 0 if u["active"] else 1
        if not new and u["role"] == "owner" and c.execute(
                "SELECT COUNT(*) n FROM users WHERE role='owner' AND active=1"
        ).fetchone()["n"] <= 1:
            raise Bad("점주 계정이 최소 1개는 있어야 합니다")
        c.execute("UPDATE users SET active=? WHERE id=?", (new, uid))
        audit(c, "직원" + ("활성화" if new else "비활성화"), u["username"])
    return {"active": new}


def change_my_pw(b):
    u = me()
    old = db().execute("SELECT pw FROM users WHERE id=?", (u["uid"],)).fetchone()
    if not verify_pw(b.get("old") or "", old["pw"]):
        raise Bad("현재 비밀번호가 틀렸습니다")
    new = b.get("new") or ""
    if len(new) < 6:
        raise Bad("비밀번호는 6자 이상이어야 합니다")
    with tx() as c:
        c.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(new), u["uid"]))
        audit(c, "비밀번호변경", "")
    return {"ok": True}


def audit_log(q):
    need("owner")
    kw = (q.get("q", [""])[0] or "").strip()
    if kw:
        return rows("SELECT * FROM audit WHERE username LIKE ? OR action LIKE ?"
                    " OR detail LIKE ? ORDER BY id DESC LIMIT 300", (f"%{kw}%",) * 3)
    return rows("SELECT * FROM audit ORDER BY id DESC LIMIT 300")


# ------------------------------------------------------- VAN(카드단말/현금영수증)
def van(op, payload):
    """매장 PC의 로컬 결제모듈 호출. POS_VAN_URL 미설정이면 None (수기입력 모드).

    VAN사(나이스/KIS/KICC/스마트로 등)마다 전문 규격이 다르다.
    계약 후 받는 연동규격서대로 **이 함수 안의 payload 키와 응답 파싱만** 고치면 된다.
    기대 응답: {"ok": true, "approval_no": "...", "card_name": "...", "message": "..."}
    """
    if not VAN_URL:
        return None
    body = json.dumps({"op": op, **payload}, ensure_ascii=False).encode()
    req = urllib.request.Request(VAN_URL, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=VAN_TIMEOUT) as r:
            res = json.loads(r.read().decode())
    except Exception as e:
        raise Bad(f"결제단말 통신 실패: {e}", 502)
    if not res.get("ok"):
        raise Bad(f"승인 거절: {res.get('message') or '단말 응답 오류'}", 402)
    return res


def norm_ident(kind, ident):
    """현금영수증 식별번호. 소득공제=휴대폰/주민, 지출증빙=사업자번호."""
    d = re.sub(r"\D", "", ident or "")
    if kind == "지출증빙":
        return norm_biz_no(d)
    if not (10 <= len(d) <= 13):
        raise Bad("현금영수증 식별번호가 올바르지 않습니다 (휴대폰번호 등)")
    return d


def issue_cash_receipt(sale_id, kind, ident, amount):
    """판매 커밋 후에 호출. 실패해도 판매는 유효하고, 상태만 '실패'로 남겨 재시도한다."""
    approval, status, msg = "", "발급", ""
    try:
        res = van("cash_receipt", {"sale_id": sale_id, "kind": kind,
                                   "ident_no": ident, "amount": amount})
        approval = (res or {}).get("approval_no", "")
        if res is None:
            status, msg = "대기", "VAN 미연동 - 승인번호 수기입력 필요"
    except Bad as e:
        status, msg = "실패", str(e)
    with tx() as c:
        c.execute("INSERT INTO cash_receipts(sale_id,kind,ident_no,amount,approval_no,"
                  "status,message,at) VALUES(?,?,?,?,?,?,?,?)",
                  (sale_id, kind, ident, amount, approval, status, msg, now()))
    return {"status": status, "message": msg}


def cash_receipt_manual(b):
    """VAN 미연동/실패 건에 승인번호를 수기로 채운다."""
    need("manager")
    rid = as_int(b.get("id"), "id")
    no = (b.get("approval_no") or "").strip()
    if not no:
        raise Bad("승인번호를 입력하세요")
    with tx() as c:
        c.execute("UPDATE cash_receipts SET approval_no=?,status='발급',message='수기입력'"
                  " WHERE id=? AND voided_at IS NULL", (no, rid))
        audit(c, "현금영수증 수기입력", f"#{rid} {no}")
    return {"ok": True}


def cash_receipts(q):
    need("manager")
    st = q.get("status", [""])[0]
    sql = ("SELECT r.*, s.at sale_at FROM cash_receipts r JOIN sales s ON s.id=r.sale_id"
           " WHERE 1=1")
    args = []
    if st:
        sql += " AND r.status=?"
        args.append(st)
    return rows(sql + " ORDER BY r.id DESC LIMIT 200", args)


# ---------------------------------------------------------------------- 백업
def backup_now():
    """SQLite 온라인 백업. 영업 중에도 일관된 스냅샷이 나온다
    (WAL 상태에서 .db 파일만 복사하면 깨질 수 있으므로 파일 복사를 쓰지 않는다)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f"pos-{datetime.now():%Y%m%d}.db"   # 하루 한 파일, 그날 최신 상태로 덮어씀
    dst = os.path.join(BACKUP_DIR, name)
    out = sqlite3.connect(dst)
    try:
        db().backup(out)
    finally:
        out.close()
    old = sorted(glob.glob(os.path.join(BACKUP_DIR, "pos-????????.db")))[:-BACKUP_KEEP]
    for f in old:
        os.remove(f)
    return {"file": name, "at": now(), "removed": len(old)}


def list_backups(q=None, b=None):
    need("owner")
    out = []
    for f in sorted(glob.glob(os.path.join(BACKUP_DIR, "pos-????????.db")), reverse=True):
        st = os.stat(f)
        out.append({"file": os.path.basename(f), "size": st.st_size,
                    "at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    return {"dir": BACKUP_DIR, "keep_days": BACKUP_KEEP, "files": out}


def backup_api(b):
    need("owner")
    r = backup_now()
    with tx() as c:
        audit(c, "수동백업", r["file"])
    return r


def backup_loop():
    """30분마다 깨어나 '오늘 백업이 없으면' 만든다. 서버가 꺼졌다 켜져도 하루 한 번은 남는다."""
    while True:
        try:
            if not os.path.exists(os.path.join(BACKUP_DIR, f"pos-{datetime.now():%Y%m%d}.db")):
                print("자동 백업:", backup_now()["file"])
        except Exception as e:                      # 백업 실패가 영업을 멈추면 안 된다
            print("자동 백업 실패:", e)
        time.sleep(1800)


# ------------------------------------------------------------------ 일일 마감
def one_date(d=None):
    d = d or datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise Bad("날짜 형식은 YYYY-MM-DD 입니다")
    return d


def closing_status(d=None):
    """그날 서랍에 있어야 할 현금을 계산한다.

    들어온 현금 = 그날 '판매된' 현금 건 전부 (나중에 취소돼도 그날 서랍엔 들어왔다)
    나간 현금   = 그날 '취소된' 현금 건 전부 (판매일이 언제든 환불은 오늘 서랍에서 나간다)
    둘 다 같은 날이면 서로 상쇄되어 자동으로 맞는다.
    """
    need("manager")
    d = one_date(d)
    a, b = d, d + "T99"
    one = lambda sql, args: db().execute(sql, args).fetchone()[0] or 0
    cash_in = one("SELECT SUM(total) FROM sales WHERE at>=? AND at<=? AND pay_method='cash'",
                  (a, b))
    cash_out = one("SELECT SUM(total) FROM sales WHERE voided_at>=? AND voided_at<=?"
                   " AND pay_method='cash'", (a, b))
    live = "WHERE at>=? AND at<=? AND voided_at IS NULL"
    card = one(f"SELECT SUM(total) FROM sales {live} AND pay_method='card'", (a, b))
    tr = one(f"SELECT SUM(total) FROM sales {live} AND pay_method='transfer'", (a, b))
    total = one(f"SELECT SUM(total) FROM sales {live}", (a, b))
    cnt = one(f"SELECT COUNT(*) FROM sales {live}", (a, b))

    done = db().execute("SELECT c.*, u.name uname FROM closings c LEFT JOIN users u"
                        " ON u.id=c.user_id WHERE d=?", (d,)).fetchone()
    prev = db().execute("SELECT counted_cash FROM closings WHERE d<? ORDER BY d DESC LIMIT 1",
                        (d,)).fetchone()
    opening = (done["opening_cash"] if done else
               prev["counted_cash"] if prev else setting("opening_cash", int) or 0)
    return {"date": d, "opening_cash": opening, "cash_in": cash_in, "cash_out": cash_out,
            "expected_cash": opening + cash_in - cash_out, "card_total": card,
            "transfer_total": tr, "sales_total": total, "sales_count": cnt,
            "closing": dict(done) if done else None}


def save_closing(b):
    need("manager")
    d = one_date(b.get("date"))
    s = closing_status(d)
    opening = as_int(b.get("opening_cash", s["opening_cash"]), "개시 시재금", 0)
    counted = as_int(b.get("counted_cash"), "실제 현금", 0)
    expected = opening + s["cash_in"] - s["cash_out"]
    memo = (b.get("memo") or "").strip()[:200]
    diff = counted - expected
    with tx() as c:
        old = c.execute("SELECT * FROM closings WHERE d=?", (d,)).fetchone()
        c.execute(
            "INSERT INTO closings(d,opening_cash,cash_in,cash_out,expected_cash,counted_cash,"
            "diff,card_total,transfer_total,sales_total,sales_count,memo,user_id,at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(d) DO UPDATE SET opening_cash=excluded.opening_cash,"
            " cash_in=excluded.cash_in, cash_out=excluded.cash_out,"
            " expected_cash=excluded.expected_cash, counted_cash=excluded.counted_cash,"
            " diff=excluded.diff, card_total=excluded.card_total,"
            " transfer_total=excluded.transfer_total, sales_total=excluded.sales_total,"
            " sales_count=excluded.sales_count, memo=excluded.memo,"
            " user_id=excluded.user_id, at=excluded.at",
            (d, opening, s["cash_in"], s["cash_out"], expected, counted, diff,
             s["card_total"], s["transfer_total"], s["sales_total"], s["sales_count"],
             memo, me()["uid"], now()))
        audit(c, "재마감" if old else "일일마감",
              f"{d} 실제 {counted:,}원 / 예상 {expected:,}원 / 차액 {diff:+,}원"
              + (f" (이전 마감 차액 {old['diff']:+,}원)" if old else "")
              + (f" — {memo}" if memo else ""))
    try:
        backup_now()                               # 마감하면 그 시점 상태를 백업해 둔다
    except Exception as e:
        print("마감 백업 실패:", e)
    return closing_status(d)


def list_closings(q):
    need("manager")
    return rows("SELECT c.*, u.name uname FROM closings c LEFT JOIN users u ON u.id=c.user_id"
                " ORDER BY c.d DESC LIMIT 60")


# ---------------------------------------------------------------- 상품 / 재고
def list_products(q):
    kw = (q.get("q", [""])[0] or "").strip()
    sql = "SELECT * FROM products WHERE active=1"
    args = []
    if kw:
        sql += (" AND (name LIKE ? OR barcode LIKE ? OR category LIKE ?"
                " OR style_code LIKE ? OR color LIKE ?)")
        args += [f"%{kw}%"] * 5
    if q.get("ids"):
        # 복원된 장바구니가 현재 가격·재고를 정확히 다시 받아오기 위한 경로
        ids = [int(x) for x in q["ids"][0].split(",") if x.strip().isdigit()][:100]
        if not ids:
            return []
        sql += " AND id IN (%s)" % ",".join("?" * len(ids))
        args += ids
    if q.get("low"):
        sql += " AND stock <= min_stock"
    # name 은 맨 뒤 — 의류는 이름에 색상/사이즈가 붙어 있어 앞에 두면 사이즈 정렬을 덮어쓴다
    return rows(sql + " ORDER BY style_code, color, " + SIZE_ORDER + ", name LIMIT 500", args)


# 사이즈는 글자라 그냥 정렬하면 L < M < S 가 된다. 알파벳 사이즈는 순서를 박고,
# 숫자 사이즈(28/30/32 등)는 숫자로 비교한다.
SIZE_ORDER = """
  CASE upper(size) WHEN 'XS' THEN 1 WHEN 'S' THEN 2 WHEN 'M' THEN 3 WHEN 'L' THEN 4
       WHEN 'XL' THEN 5 WHEN '2XL' THEN 6 WHEN 'XXL' THEN 6
       WHEN '3XL' THEN 7 WHEN 'XXXL' THEN 7 WHEN 'FREE' THEN 8 ELSE 9 END,
  CAST(size AS INTEGER), size"""


def save_product(b):
    need("manager")
    name = (b.get("name") or "").strip()
    if not name:
        raise Bad("상품명은 필수입니다")
    f = (name, (b.get("barcode") or "").strip() or None,
         as_int(b.get("cost", 0), "매입가", 0), as_int(b.get("price"), "판매가", 0),
         as_int(b.get("min_stock", 0), "안전재고", 0), (b.get("category") or "").strip(),
         (b.get("style_code") or "").strip(), (b.get("color") or "").strip(),
         (b.get("size") or "").strip())
    try:
        with tx() as c:
            if b.get("id"):
                pid = as_int(b["id"], "id")
                old = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                if not old:
                    raise Bad("상품을 찾을 수 없습니다", 404)
                c.execute("UPDATE products SET name=?,barcode=?,cost=?,price=?,min_stock=?,"
                          "category=?,style_code=?,color=?,size=? WHERE id=?", f + (pid,))
                if old["price"] != f[3]:  # 가격 변경은 반드시 흔적을 남긴다
                    audit(c, "가격변경", f"{name}: {old['price']} -> {f[3]}원")
            else:
                pid = c.execute("INSERT INTO products(name,barcode,cost,price,min_stock,"
                                "category,style_code,color,size,stock)"
                                " VALUES(?,?,?,?,?,?,?,?,?,0)", f).lastrowid
                audit(c, "상품등록", name)
                init = as_int(b.get("stock", 0), "초기재고", 0)
                if init:
                    c.execute("UPDATE products SET stock=? WHERE id=?", (init, pid))
                    c.execute("INSERT INTO stock_moves(product_id,delta,reason,note,"
                              "user_id,at) VALUES(?,?,'초기',?,?,?)",
                              (pid, init, "상품 등록", me()["uid"], now()))
    except sqlite3.IntegrityError:
        raise Bad("이미 등록된 바코드입니다")
    return {"id": pid}


def save_product_bulk(b):
    """의류용 — 품번 하나에 색상×사이즈 조합을 한 번에 SKU로 깐다.
    전부 한 트랜잭션. 중간에 실패하면 하나도 안 만들어진다."""
    need("manager")
    style = (b.get("style_code") or "").strip()
    name = (b.get("name") or "").strip()
    split = lambda s: [x.strip() for x in re.split(r"[,/|]", s or "") if x.strip()]
    colors, sizes = split(b.get("colors")), split(b.get("sizes"))
    if not style:
        raise Bad("품번은 필수입니다")
    if not name:
        raise Bad("상품명은 필수입니다")
    if not colors or not sizes:
        raise Bad("색상과 사이즈를 하나 이상 입력하세요 (쉼표로 구분)")
    if len(colors) * len(sizes) > 200:
        raise Bad("한 번에 200개까지만 만들 수 있습니다")
    price = as_int(b.get("price"), "판매가", 0)
    cost = as_int(b.get("cost", 0), "매입가", 0)
    mins = as_int(b.get("min_stock", 0), "안전재고", 0)
    stock = as_int(b.get("stock", 0), "색상·사이즈별 초기재고", 0)
    cat = (b.get("category") or "").strip()
    made, skipped = [], []
    with tx() as c:
        for color in colors:
            for size in sizes:
                bc = f"{style}-{color}-{size}"
                if c.execute("SELECT 1 FROM products WHERE barcode=?", (bc,)).fetchone():
                    skipped.append(bc)
                    continue
                pid = c.execute(
                    "INSERT INTO products(name,barcode,cost,price,min_stock,category,"
                    "style_code,color,size,stock) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"{name} {color}/{size}", bc, cost, price, mins, cat,
                     style, color, size, stock)).lastrowid
                if stock:
                    c.execute("INSERT INTO stock_moves(product_id,delta,reason,note,"
                              "user_id,at) VALUES(?,?,'초기',?,?,?)",
                              (pid, stock, "일괄등록", me()["uid"], now()))
                made.append(bc)
        if not made:
            raise Bad("이미 모두 등록되어 있습니다")
        audit(c, "상품일괄등록", f"{style} {name} — {len(made)}개 SKU")
    return {"created": len(made), "skipped": skipped}


def delete_product(pid):
    need("manager")
    pid = as_int(pid, "id")
    with tx() as c:
        p = c.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
        c.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
        audit(c, "상품삭제", p["name"] if p else str(pid))
    return {"ok": True}


SENSITIVE_REASONS = ("폐기", "실사조정")


def move_stock(b):
    """입고/폐기/실사조정. delta 양수=입고, 음수=차감."""
    delta = as_int(b.get("delta"), "수량")
    if delta == 0:
        raise Bad("수량이 0입니다")
    reason = (b.get("reason") or "입고").strip()
    if reason in SENSITIVE_REASONS:  # 재고를 임의로 줄이는 건 매니저 이상
        need("manager")
    with tx() as c:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1",
                      (as_int(b.get("product_id"), "상품"),)).fetchone()
        if not p:
            raise Bad("상품을 찾을 수 없습니다", 404)
        if p["stock"] + delta < 0:
            raise Bad(f"재고 부족: 현재 {p['stock']}개")
        c.execute("UPDATE products SET stock=stock+? WHERE id=?", (delta, p["id"]))
        c.execute("INSERT INTO stock_moves(product_id,delta,reason,note,user_id,at)"
                  " VALUES(?,?,?,?,?,?)",
                  (p["id"], delta, reason, b.get("note", ""), me()["uid"], now()))
        if reason in SENSITIVE_REASONS:
            audit(c, f"재고{reason}", f"{p['name']} {delta:+d}개 ({b.get('note','')})")
        return {"stock": p["stock"] + delta}


# ------------------------------------------------------------------ 고객/포인트
def list_customers(q):
    kw = (q.get("q", [""])[0] or "").strip()
    if not kw:
        return rows("SELECT * FROM customers ORDER BY id DESC LIMIT 50")
    digits = re.sub(r"\D", "", kw)
    return rows("SELECT * FROM customers WHERE (?<>'' AND phone LIKE ?) OR name LIKE ?"
                " OR biz_name LIKE ? ORDER BY name LIMIT 100",
                (digits, f"%{digits}%", f"%{kw}%", f"%{kw}%"))


def save_customer(b):
    phone = norm_phone(b.get("phone"))
    name = (b.get("name") or "").strip()
    memo = (b.get("memo") or "").strip()
    biz = (norm_biz_no(b.get("business_no")), (b.get("biz_name") or "").strip(),
           (b.get("biz_ceo") or "").strip(), (b.get("biz_addr") or "").strip(),
           (b.get("biz_item") or "").strip(), (b.get("biz_email") or "").strip())
    if biz[5] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", biz[5]):
        raise Bad("이메일 형식이 올바르지 않습니다")
    try:
        with tx() as c:
            if b.get("id"):
                cid = as_int(b["id"], "id")
                c.execute("UPDATE customers SET phone=?,name=?,memo=?,business_no=?,"
                          "biz_name=?,biz_ceo=?,biz_addr=?,biz_item=?,biz_email=?"
                          " WHERE id=?", (phone, name, memo) + biz + (cid,))
                return {"id": cid}
            row = c.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
            if row:
                return {"id": row["id"], "existing": True}
            cid = c.execute(
                "INSERT INTO customers(phone,name,memo,business_no,biz_name,biz_ceo,"
                "biz_addr,biz_item,biz_email,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (phone, name, memo) + biz + (now(),)).lastrowid
            return {"id": cid}
    except sqlite3.IntegrityError:
        raise Bad("이미 등록된 전화번호입니다")


def adjust_points(b):
    """수기 포인트 조정(보상/정정). 잔액은 음수가 될 수 없다."""
    need("manager")
    delta = as_int(b.get("delta"), "포인트")
    if delta == 0:
        raise Bad("포인트가 0입니다")
    cid = as_int(b.get("customer_id"), "고객")
    reason = (b.get("reason") or "수기조정")[:40]
    with tx() as c:
        cu = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if not cu:
            raise Bad("고객을 찾을 수 없습니다", 404)
        if cu["points"] + delta < 0:
            raise Bad(f"포인트 부족: 보유 {cu['points']}P")
        c.execute("UPDATE customers SET points=points+? WHERE id=?", (delta, cid))
        c.execute("INSERT INTO point_moves(customer_id,delta,reason,user_id,at)"
                  " VALUES(?,?,?,?,?)", (cid, delta, reason, me()["uid"], now()))
        audit(c, "포인트조정", f"{cu['name'] or cu['phone']} {delta:+d}P ({reason})")
        return {"points": cu["points"] + delta}


def customer_detail(cid):
    cid = as_int(cid, "고객")
    cu = db().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not cu:
        raise Bad("고객을 찾을 수 없습니다", 404)
    return {"customer": dict(cu),
            "points": rows("SELECT p.*, u.name uname FROM point_moves p"
                           " LEFT JOIN users u ON u.id=p.user_id"
                           " WHERE customer_id=? ORDER BY p.id DESC LIMIT 50", (cid,)),
            "sales": rows("SELECT * FROM sales WHERE customer_id=?"
                          " ORDER BY id DESC LIMIT 30", (cid,))}


# ---------------------------------------------------------------------- 판매
def _quote(c, items, discount, point_used, cu):
    """금액 계산 + 재고/포인트 검증. 트랜잭션 안팎 양쪽에서 동일하게 쓴다."""
    lines, subtotal = [], 0
    for it in items:
        pid = as_int(it.get("product_id"), "상품")
        qty = as_int(it.get("qty"), "수량", 1)
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            raise Bad(f"상품 {pid} 없음", 404)
        if p["stock"] < qty:
            raise Bad(f"[{p['name']}] 재고 부족 (현재 {p['stock']}개)")
        lines.append((pid, p["name"], qty, p["price"]))  # 가격은 서버 DB 기준
        subtotal += p["price"] * qty
    if discount > subtotal:
        raise Bad("할인이 판매금액보다 큽니다")
    if point_used:
        if not cu:
            raise Bad("포인트를 쓰려면 고객을 선택하세요")
        if point_used > cu["points"]:
            raise Bad(f"포인트 부족: 보유 {cu['points']}P")
        if point_used > subtotal - discount:
            raise Bad("포인트가 결제금액보다 큽니다")
        lo = setting("point_min_use", int) or 0
        if cu["points"] < lo:
            raise Bad(f"{lo}P 이상부터 사용 가능합니다")
    total = subtotal - discount - point_used
    rate = setting("point_rate", int) or 0
    earned = (total * rate) // 100 if cu else 0  # 포인트로 낸 금액은 적립 제외
    return lines, subtotal, total, earned


def create_sale(b):
    items = b.get("items") or []
    if not items:
        raise Bad("판매 항목이 없습니다")
    discount = as_int(b.get("discount", 0), "할인", 0)
    point_used = as_int(b.get("point_used", 0), "포인트사용", 0)
    pay = (b.get("pay_method") or "card").strip()
    if pay not in ("card", "cash", "transfer"):
        raise Bad("결제수단이 올바르지 않습니다")
    cid = b.get("customer_id")
    cid = as_int(cid, "고객") if cid else None
    install = as_int(b.get("installment", 0), "할부", 0, 36)
    cr = b.get("cash_receipt") or None
    if cr:
        cr_kind = cr.get("kind") if cr.get("kind") in ("소득공제", "지출증빙") else None
        if not cr_kind:
            raise Bad("현금영수증 용도를 선택하세요")
        cr_ident = norm_ident(cr_kind, cr.get("ident_no"))

    c = db()
    cu = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone() if cid else None
    if cid and not cu:
        raise Bad("고객을 찾을 수 없습니다", 404)

    # 1) 금액 확정 (읽기 전용)
    _, _, quoted, _ = _quote(c, items, discount, point_used, cu)

    # 2) 카드 승인 — 네트워크 호출이라 트랜잭션 **밖**에서 한다
    approval = (b.get("approval_no") or "").strip()
    card_name = (b.get("card_name") or "").strip()
    van_res = None
    if pay == "card" and quoted > 0:
        van_res = van("approve", {"amount": quoted, "installment": install})
        if van_res:
            approval = van_res.get("approval_no", "")
            card_name = van_res.get("card_name", "")

    # 3) 기록 — 여기서 실패하면 2)의 승인을 되돌려야 한다
    try:
        with tx() as c:
            lines, subtotal, total, earned = _quote(c, items, discount, point_used, cu)
            if total != quoted:
                raise Bad("계산 중 가격/재고가 변경되었습니다. 다시 시도하세요", 409)
            sid = c.execute(
                "INSERT INTO sales(customer_id,subtotal,discount,point_used,point_earned,"
                "total,pay_method,approval_no,card_name,installment,user_id,at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, subtotal, discount, point_used, earned, total, pay, approval,
                 card_name, install, me()["uid"], now())).lastrowid
            for pid, pname, qty, price in lines:
                c.execute("INSERT INTO sale_items(sale_id,product_id,name,qty,price)"
                          " VALUES(?,?,?,?,?)", (sid, pid, pname, qty, price))
                c.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, pid))
                c.execute("INSERT INTO stock_moves(product_id,delta,reason,sale_id,"
                          "user_id,at) VALUES(?,?,'판매',?,?,?)",
                          (pid, -qty, sid, me()["uid"], now()))
            if cu:
                net = earned - point_used
                if net:
                    c.execute("UPDATE customers SET points=points+? WHERE id=?", (net, cid))
                if point_used:
                    c.execute("INSERT INTO point_moves(customer_id,delta,reason,sale_id,"
                              "user_id,at) VALUES(?,?,'사용',?,?,?)",
                              (cid, -point_used, sid, me()["uid"], now()))
                if earned:
                    c.execute("INSERT INTO point_moves(customer_id,delta,reason,sale_id,"
                              "user_id,at) VALUES(?,?,'적립',?,?,?)",
                              (cid, earned, sid, me()["uid"], now()))
    except Exception:
        if van_res:  # 카드는 승인됐는데 DB 기록이 깨진 경우 - 승인 취소
            try:
                van("cancel", {"approval_no": approval, "amount": quoted})
            except Bad:
                pass  # 취소까지 실패하면 단말 매출전표로 수기 취소해야 한다
        raise

    if cr and total > 0:
        res = issue_cash_receipt(sid, cr_kind, cr_ident, total)
        out = sale_detail(sid)          # 발급 결과를 영수증에 싣기 위해 발급 후에 읽는다
        out["cash_receipt"] = res
        return out
    return sale_detail(sid)


def void_sale(b):
    """반품/취소: 재고 복구 + 포인트 원복 + 카드 승인취소."""
    need("manager")  # 횡령 방지 - 직원 단독 취소 금지
    sid = as_int(b.get("id"), "판매")
    s = db().execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone()
    if not s:
        raise Bad("판매 내역을 찾을 수 없습니다", 404)
    if s["voided_at"]:
        raise Bad("이미 취소된 건입니다")

    van_done = False
    if s["pay_method"] == "card" and s["approval_no"] and VAN_URL:
        van("cancel", {"approval_no": s["approval_no"], "amount": s["total"],
                       "sale_at": s["at"]})
        van_done = True
    with tx() as c:
        for it in c.execute("SELECT * FROM sale_items WHERE sale_id=?", (sid,)).fetchall():
            c.execute("UPDATE products SET stock=stock+? WHERE id=?",
                      (it["qty"], it["product_id"]))
            c.execute("INSERT INTO stock_moves(product_id,delta,reason,sale_id,user_id,at)"
                      " VALUES(?,?,'판매취소',?,?,?)",
                      (it["product_id"], it["qty"], sid, me()["uid"], now()))
        if s["customer_id"]:
            net = s["point_used"] - s["point_earned"]
            cu = c.execute("SELECT points FROM customers WHERE id=?",
                           (s["customer_id"],)).fetchone()
            if cu["points"] + net < 0:
                raise Bad("적립 포인트를 이미 사용해 자동 취소가 안 됩니다. "
                          "포인트 수기조정 후 다시 시도하세요")
            if net:
                c.execute("UPDATE customers SET points=points+? WHERE id=?",
                          (net, s["customer_id"]))
                c.execute("INSERT INTO point_moves(customer_id,delta,reason,sale_id,"
                          "user_id,at) VALUES(?,?,'판매취소',?,?,?)",
                          (s["customer_id"], net, sid, me()["uid"], now()))
        c.execute("UPDATE sales SET voided_at=?, void_user_id=? WHERE id=?",
                  (now(), me()["uid"], sid))
        c.execute("UPDATE cash_receipts SET voided_at=?, status='취소' WHERE sale_id=?"
                  " AND voided_at IS NULL", (now(), sid))
        audit(c, "판매취소", f"#{sid} {s['total']}원"
                            + (" (카드 승인취소 완료)" if van_done else ""))
    return {"ok": True, "van_cancelled": van_done}


def sale_detail(sid):
    sid = as_int(sid, "판매")
    s = db().execute("SELECT s.*, u.name uname FROM sales s LEFT JOIN users u"
                     " ON u.id=s.user_id WHERE s.id=?", (sid,)).fetchone()
    if not s:
        raise Bad("판매 내역을 찾을 수 없습니다", 404)
    d = dict(s)
    d["items"] = rows("SELECT * FROM sale_items WHERE sale_id=?", (sid,))
    d["receipts"] = rows("SELECT * FROM cash_receipts WHERE sale_id=?", (sid,))
    if s["customer_id"]:
        cu = db().execute("SELECT name,phone,points FROM customers WHERE id=?",
                          (s["customer_id"],)).fetchone()
        d["customer"] = dict(cu) if cu else None
    d["store"] = {k: setting(k) for k in
                  ("store_name", "store_biz_no", "store_ceo", "store_addr", "store_tel")}
    return d


# ---------------------------------------------------------------------- 정산
def date_range(q):
    today = datetime.now().strftime("%Y-%m-%d")
    d1 = q.get("from", [today])[0]
    d2 = q.get("to", [d1])[0]
    if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", d1) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d2)):
        raise Bad("날짜 형식은 YYYY-MM-DD 입니다")
    return d1, d2 + "T99"  # ISO 문자열 비교라 하루 끝까지 포함


def report(q):
    need("manager")
    a, b = date_range(q)
    live = "WHERE at>=? AND at<=? AND voided_at IS NULL"
    sm = db().execute(
        "SELECT COUNT(*) n, COALESCE(SUM(total),0) total, COALESCE(SUM(discount),0) disc,"
        " COALESCE(SUM(point_used),0) used, COALESCE(SUM(point_earned),0) earned,"
        f" COALESCE(SUM(subtotal),0) gross FROM sales {live}", (a, b)).fetchone()
    return {
        "summary": dict(sm),
        "by_pay": rows(f"SELECT pay_method, COUNT(*) n, SUM(total) total FROM sales {live}"
                       " GROUP BY pay_method", (a, b)),
        "by_day": rows(f"SELECT substr(at,1,10) d, COUNT(*) n, SUM(total) total"
                       f" FROM sales {live} GROUP BY d ORDER BY d", (a, b)),
        "by_user": rows("SELECT COALESCE(u.name,'(미상)') uname, COUNT(*) n,"
                        " SUM(s.total) total FROM sales s LEFT JOIN users u ON u.id=s.user_id"
                        " WHERE s.at>=? AND s.at<=? AND s.voided_at IS NULL"
                        " GROUP BY s.user_id ORDER BY total DESC", (a, b)),
        "top": rows("SELECT i.name, SUM(i.qty) qty, SUM(i.qty*i.price) amt"
                    " FROM sale_items i JOIN sales s ON s.id=i.sale_id"
                    " WHERE s.at>=? AND s.at<=? AND s.voided_at IS NULL"
                    " GROUP BY i.product_id ORDER BY amt DESC LIMIT 10", (a, b)),
        "sales": rows("SELECT s.*, c.name cname, c.phone, u.name uname, v.name vname"
                      " FROM sales s LEFT JOIN customers c ON c.id=s.customer_id"
                      " LEFT JOIN users u ON u.id=s.user_id"
                      " LEFT JOIN users v ON v.id=s.void_user_id"
                      " WHERE s.at>=? AND s.at<=? ORDER BY s.id DESC LIMIT 200", (a, b)),
    }


def report_csv(q):
    need("manager")
    a, b = date_range(q)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["판매번호", "일시", "담당직원", "고객", "상품", "수량", "단가", "금액",
                "할인", "포인트사용", "결제액", "결제수단", "승인번호", "취소"])
    for r in rows("SELECT s.*, c.name cname, u.name uname, i.name iname, i.qty, i.price"
                  " FROM sales s JOIN sale_items i ON i.sale_id=s.id"
                  " LEFT JOIN customers c ON c.id=s.customer_id"
                  " LEFT JOIN users u ON u.id=s.user_id"
                  " WHERE s.at>=? AND s.at<=? ORDER BY s.id", (a, b)):
        w.writerow([r["id"], r["at"], r["uname"] or "", r["cname"] or "", r["iname"],
                    r["qty"], r["price"], r["qty"] * r["price"], r["discount"],
                    r["point_used"], r["total"], r["pay_method"], r["approval_no"] or "",
                    "취소" if r["voided_at"] else ""])
    return out.getvalue()


# ------------------------------------------------------------------ 발주 제안
def reorder(q):
    """판매속도로 SKU별 권장 발주량을 뽑는다.

        일평균 판매 = 기간 판매수량 / 실제 판매 가능일수
        목표 재고   = 일평균 × (입고 소요일 + 커버 일수)
        권장 발주   = max(목표, 안전재고) − 현재고

    '실제 판매 가능일수'를 쓰는 게 핵심이다. 30일로 나누면 2주 전 들어온 신상이
    속도가 절반으로 깎여 재발주가 안 나간다 — 의류에서 제일 아픈 실수.
    """
    need("manager")
    days = as_int(q.get("days", ["30"])[0], "분석 기간", 7, 365)
    lead = as_int(q.get("lead", ["7"])[0], "입고 소요일", 0, 90)
    cover = as_int(q.get("cover", ["14"])[0], "커버 일수", 1, 180)
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    raw = rows(
        "SELECT p.id, p.name, p.style_code, p.color, p.size, p.barcode, p.price, p.cost,"
        " p.stock, p.min_stock, p.category, COALESCE(s.qty,0) sold,"
        " (SELECT MIN(at) FROM stock_moves m WHERE m.product_id=p.id) first_at"
        " FROM products p LEFT JOIN ("
        "   SELECT i.product_id, SUM(i.qty) qty FROM sale_items i"
        "   JOIN sales s ON s.id=i.sale_id"
        "   WHERE s.at>=? AND s.voided_at IS NULL GROUP BY i.product_id) s"
        " ON s.product_id=p.id WHERE p.active=1", (start,))

    out = []
    for r in raw:
        # 취급 시작일이 분석 시작일보다 늦으면 그 날부터만 센다. 이력이 없으면 전 기간으로 본다.
        began = max(start, (r.pop("first_at") or start)[:10])
        sell_days = max(1, (datetime.strptime(today, "%Y-%m-%d")
                            - datetime.strptime(began, "%Y-%m-%d")).days + 1)
        rate = r["sold"] / sell_days
        target = max(math.ceil(rate * (lead + cover)), r["min_stock"])
        r["sell_days"] = sell_days
        r["rate"] = round(rate, 2)
        r["target"] = target
        r["qty"] = max(0, target - r["stock"])
        r["amount"] = r["qty"] * r["cost"]
        r["days_left"] = round(r["stock"] / rate, 1) if rate else None  # 재고 소진 예상일
        if r["stock"] == 0 and r["sold"]:
            r["flag"] = "품절"            # 팔리는데 재고 0 — 제일 급하다
        elif not r["sold"] and sell_days >= 14 and r["stock"]:
            r["flag"] = "안팔림"          # 2주 넘게 안 팔린 재고 — 발주 말고 정리 대상
        elif r["qty"]:
            r["flag"] = "보충"
        else:
            r["flag"] = "충분"
        out.append(r)

    # 품절 > 보충 > 안팔림 > 충분, 같은 등급이면 잘 나가는 순
    rank = {"품절": 0, "보충": 1, "안팔림": 2, "충분": 3}
    out.sort(key=lambda r: (rank[r["flag"]], -r["rate"], r["style_code"], r["color"]))
    need_rows = [r for r in out if r["qty"]]
    return {"params": {"days": days, "lead": lead, "cover": cover, "from": start},
            "summary": {"skus": len(need_rows),
                        "qty": sum(r["qty"] for r in need_rows),
                        "amount": sum(r["amount"] for r in need_rows),
                        "dead": sum(1 for r in out if r["flag"] == "안팔림"),
                        "dead_amount": sum(r["stock"] * r["cost"] for r in out
                                           if r["flag"] == "안팔림")},
            "rows": out}


def reorder_csv(q):
    """거래처에 보낼 발주서. 권장 수량이 있는 SKU만 담는다."""
    d = reorder(q)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["품번", "상품명", "색상", "사이즈", "바코드", "현재고", "일평균판매",
                "권장발주", "매입가", "발주금액", "상태"])
    for r in d["rows"]:
        if not r["qty"]:
            continue
        w.writerow([r["style_code"], r["name"], r["color"], r["size"], r["barcode"] or "",
                    r["stock"], r["rate"], r["qty"], r["cost"], r["amount"], r["flag"]])
    w.writerow([])
    w.writerow(["합계", "", "", "", "", "", "", d["summary"]["qty"], "",
                d["summary"]["amount"], ""])
    return out.getvalue()


# ------------------------------------------------------------------ 세금계산서
def tax_month(q):
    m = q.get("month", [datetime.now().strftime("%Y-%m")])[0]
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        raise Bad("월 형식은 YYYY-MM 입니다")
    return m, m + "-01", m + "-31T99"


def tax_summary(q):
    """사업자 고객별 월 거래 집계. 발행 자체는 홈택스/팝빌 등 외부에서 한다."""
    need("manager")
    m, a, b = tax_month(q)
    data = rows(
        "SELECT c.id, c.business_no, c.biz_name, c.biz_ceo, c.biz_addr, c.biz_item,"
        " c.biz_email, c.name, c.phone, COUNT(*) n, SUM(s.total) total"
        " FROM sales s JOIN customers c ON c.id=s.customer_id"
        " WHERE s.at>=? AND s.at<=? AND s.voided_at IS NULL AND c.business_no<>''"
        " GROUP BY c.id ORDER BY total DESC", (a, b))
    for r in data:  # 판매가는 부가세 포함 가정
        r["supply"] = round(r["total"] / 1.1)
        r["vat"] = r["total"] - r["supply"]
    return {"month": m, "rows": data,
            "store": {k: setting(k) for k in
                      ("store_name", "store_biz_no", "store_ceo", "store_addr",
                       "store_tel", "store_item")}}


def tax_csv(q):
    """팝빌/더존 등 대량발행 업로드용. 실제 양식은 서비스마다 열 이름만 다르다."""
    need("manager")
    d = tax_summary(q)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["작성일자", "공급자등록번호", "공급자상호", "공급받는자등록번호",
                "공급받는자상호", "대표자", "주소", "업태종목", "이메일",
                "품목", "공급가액", "세액", "합계"])
    y, mo = (int(x) for x in d["month"].split("-"))
    last = f"{d['month']}-{calendar.monthrange(y, mo)[1]:02d}"  # 작성일자 = 월말일
    for r in d["rows"]:
        w.writerow([last, d["store"]["store_biz_no"], d["store"]["store_name"],
                    r["business_no"], r["biz_name"] or r["name"], r["biz_ceo"],
                    r["biz_addr"], r["biz_item"], r["biz_email"],
                    f"{d['month']} 상품대금", r["supply"], r["vat"], r["total"]])
    return out.getvalue()


# ---------------------------------------------------------------------- 설정
SETTING_KEYS = ("store_name", "store_biz_no", "store_ceo", "store_addr", "store_tel",
                "store_item", "point_rate", "point_min_use", "opening_cash")


def get_settings(q=None, b=None):
    return {r["k"]: r["v"] for r in db().execute("SELECT * FROM settings")}


def save_settings(b):
    need("owner")
    with tx() as c:
        for k in SETTING_KEYS:
            if k in b:
                v = str(b[k]).strip()
                if k == "store_biz_no":
                    v = norm_biz_no(v)
                elif k == "point_rate":
                    as_int(v, "적립률", 0, 100)
                elif k in ("point_min_use", "opening_cash"):
                    as_int(v, k, 0, 10 ** 9)
                c.execute("UPDATE settings SET v=? WHERE k=?", (v, k))
        audit(c, "설정변경", ", ".join(k for k in SETTING_KEYS if k in b))
    return get_settings()


# ---------------------------------------------------------------------- 라우팅
# (메서드, 경로) -> (핸들러, 필요권한)
ROUTES = {
    ("GET", "/api/me"): (lambda q, b: {**me(), "exp": None, "van": bool(VAN_URL)}, "staff"),
    ("POST", "/api/me/password"): (lambda q, b: change_my_pw(b), "staff"),
    ("GET", "/api/users"): (lambda q, b: list_users(q), "owner"),
    ("POST", "/api/users"): (lambda q, b: save_user(b), "owner"),
    ("POST", "/api/users/toggle"): (lambda q, b: toggle_user(b), "owner"),
    ("GET", "/api/audit"): (lambda q, b: audit_log(q), "owner"),
    ("GET", "/api/products"): (lambda q, b: list_products(q), "staff"),
    ("POST", "/api/products"): (lambda q, b: save_product(b), "manager"),
    ("POST", "/api/products/bulk"): (lambda q, b: save_product_bulk(b), "manager"),
    ("POST", "/api/products/delete"): (lambda q, b: delete_product(b.get("id")), "manager"),
    ("POST", "/api/stock"): (lambda q, b: move_stock(b), "staff"),
    ("GET", "/api/stock/moves"): (lambda q, b: rows(
        "SELECT m.*, p.name, u.name uname FROM stock_moves m"
        " JOIN products p ON p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id"
        " ORDER BY m.id DESC LIMIT 200"), "staff"),
    ("GET", "/api/customers"): (lambda q, b: list_customers(q), "staff"),
    ("POST", "/api/customers"): (lambda q, b: save_customer(b), "staff"),
    ("GET", "/api/customer"): (lambda q, b: customer_detail(q.get("id", [0])[0]), "staff"),
    ("POST", "/api/points"): (lambda q, b: adjust_points(b), "manager"),
    ("POST", "/api/sales"): (lambda q, b: create_sale(b), "staff"),
    ("GET", "/api/sale"): (lambda q, b: sale_detail(q.get("id", [0])[0]), "staff"),
    ("POST", "/api/sales/void"): (lambda q, b: void_sale(b), "manager"),
    ("GET", "/api/receipts"): (lambda q, b: cash_receipts(q), "manager"),
    ("POST", "/api/receipts/manual"): (lambda q, b: cash_receipt_manual(b), "manager"),
    ("GET", "/api/report"): (lambda q, b: report(q), "manager"),
    ("GET", "/api/closing"): (lambda q, b: closing_status(q.get("date", [None])[0]), "manager"),
    ("POST", "/api/closing"): (lambda q, b: save_closing(b), "manager"),
    ("GET", "/api/closings"): (lambda q, b: list_closings(q), "manager"),
    ("GET", "/api/backups"): (list_backups, "owner"),
    ("POST", "/api/backups"): (lambda q, b: backup_api(b), "owner"),
    ("GET", "/api/tax"): (lambda q, b: tax_summary(q), "manager"),
    ("GET", "/api/reorder"): (lambda q, b: reorder(q), "manager"),
    ("GET", "/api/settings"): (get_settings, "staff"),
    ("POST", "/api/settings"): (lambda q, b: save_settings(b), "owner"),
}
CSV_ROUTES = {"/api/report.csv": (report_csv, "sales.csv"),
              "/api/tax.csv": (tax_csv, "tax_invoices.csv"),
              "/api/reorder.csv": (reorder_csv, "purchase_order.csv")}


class H(BaseHTTPRequestHandler):
    server_version = "POS"

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str), extra=extra)

    def _auth(self):
        """세션 쿠키 -> _local.user. 요청마다 스레드가 새로 뜨므로 누수 없음."""
        _local.user = None
        m = re.search(r"sid=([a-f0-9]+)", self.headers.get("Cookie", "") or "")
        if not m:
            return False
        s = SESSIONS.get(m.group(1))
        if not s or s["exp"] < datetime.now():
            SESSIONS.pop(m.group(1), None)
            return False
        _local.user = s
        return True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(404, b"index.html not found", "text/plain")
        if u.path in CSV_ROUTES:
            fn, fname = CSV_ROUTES[u.path]
            if not self._auth():
                return self._json(401, {"error": "로그인이 필요합니다"})
            try:
                body = "﻿" + fn(parse_qs(u.query))  # 엑셀 한글 BOM
            except Bad as e:
                return self._json(e.code, {"error": str(e)})
            return self._send(200, body, "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{fname}"'})
        self._handle("GET", u)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return self._json(413, {"error": "요청이 너무 큽니다"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"error": "잘못된 JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "잘못된 요청"})
        if u.path == "/api/login":
            try:
                tok, who = login(body.get("username"), body.get("password"))
            except Bad as e:
                return self._json(e.code, {"error": str(e)})
            return self._json(200, {**who, "van": bool(VAN_URL)}, {
                "Set-Cookie": f"sid={tok}; Path=/; HttpOnly; SameSite=Strict; "
                              f"Max-Age={SESSION_HOURS * 3600}"})
        if u.path == "/api/logout":
            m = re.search(r"sid=([a-f0-9]+)", self.headers.get("Cookie", "") or "")
            SESSIONS.pop(m.group(1), None) if m else None
            return self._json(200, {"ok": True}, {"Set-Cookie": "sid=; Path=/; Max-Age=0"})
        self._handle("POST", u, body)

    def _handle(self, method, u, body=None):
        if not self._auth():
            return self._json(401, {"error": "로그인이 필요합니다"})
        route = ROUTES.get((method, u.path))
        if not route:
            return self._json(404, {"error": "없는 경로"})
        fn, role = route
        try:
            need(role)
            return self._json(200, fn(parse_qs(u.query), body or {}))
        except Bad as e:
            return self._json(e.code, {"error": str(e)})
        except sqlite3.Error as e:
            return self._json(500, {"error": f"DB 오류: {e}"})

    def log_message(self, *a):
        pass


# ------------------------------------------------------------------- 부팅
def init_db(seed=False):
    c = db()
    c.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        pw = os.environ.get("POS_ADMIN_PW", "admin1234")
        c.execute("INSERT INTO users(username,name,pw,role,created_at) VALUES(?,?,?,?,?)",
                  ("admin", "점주", hash_pw(pw), "owner", now()))
        print(f"점주 계정 생성: admin / {pw}")
        if pw == "admin1234":
            print("!! 기본 비밀번호입니다. 로그인 후 설정 > 비밀번호 변경 하세요.")

    if seed and not c.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        _local.user = {"uid": 1, "name": "점주", "role": "owner"}
        for style, name, cost, price, colors, sizes, stock, cat in [
            ("TS-2601", "베이직 반팔티", 4000, 19000, "블랙,화이트,네이비", "S,M,L,XL", 8, "상의"),
            ("KN-2602", "울 니트 가디건", 21000, 69000, "오트밀,차콜", "S,M,L", 4, "상의"),
            ("DN-2603", "와이드 데님팬츠", 18000, 59000, "라이트블루,인디고", "26,28,30,32", 3, "하의"),
            ("OT-2604", "오버핏 코트", 58000, 189000, "베이지,블랙", "FREE", 2, "아우터"),
        ]:
            save_product_bulk({"style_code": style, "name": name, "cost": cost,
                               "price": price, "colors": colors, "sizes": sizes,
                               "stock": stock, "min_stock": 2, "category": cat})
        save_customer({"phone": "010-1111-2222", "name": "김단골"})
        save_customer({"phone": "010-3333-4444", "name": "박사장", "business_no": "2208162517",
                       "biz_name": "(주)박사장상사", "biz_ceo": "박사장",
                       "biz_email": "biz@example.com", "biz_item": "도소매"})
        _local.user = None
        print("샘플 데이터 생성 완료")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    init_db(a.seed)
    threading.Thread(target=backup_loop, daemon=True).start()
    print(f"카드단말(VAN): {VAN_URL or '미연동 (승인번호 수기입력 모드)'}")
    print(f"자동 백업: {BACKUP_DIR} (하루 1회, {BACKUP_KEEP}일 보관)")
    print(f"POS 실행: http://{a.host}:{a.port}   (DB: {DB_PATH})")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()

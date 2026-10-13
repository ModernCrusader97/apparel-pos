"""돈/재고/포인트/권한/결제 경로 자가검증. `python test_pos.py` 로 실행."""
import os, tempfile

os.environ["POS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["POS_ADMIN_PW"] = "owner123"
os.environ["POS_BACKUP_DIR"] = os.path.join(os.path.dirname(os.environ["POS_DB"]), "backup")
import pos
from pos import Bad


def fails(fn, frag):
    try:
        fn()
    except Bad as e:
        assert frag in str(e), f"다른 오류가 났습니다: {e}"
        return
    raise AssertionError(f"막혔어야 함: {frag}")


def act_as(role):
    pos._local.user = {"uid": {"owner": 1, "manager": 2, "staff": 3}[role],
                       "name": role, "role": role}


pos.init_db()
act_as("owner")
pos.save_settings({"point_rate": "10", "point_min_use": "0", "store_biz_no": "220-81-62517"})

# ---------------------------------------------------------------- 계정 / 권한
assert pos.verify_pw("owner123", pos.db().execute(
    "SELECT pw FROM users WHERE username='admin'").fetchone()["pw"])
assert not pos.verify_pw("틀린비번", pos.db().execute(
    "SELECT pw FROM users WHERE username='admin'").fetchone()["pw"])
fails(lambda: pos.login("admin", "틀린비번"), "비밀번호가 올바르지")
tok, who = pos.login("admin", "owner123")
assert who["role"] == "owner" and tok in pos.SESSIONS

M = pos.save_user({"username": "mgr", "name": "매니저", "password": "mgr123", "role": "manager"})["id"]
S = pos.save_user({"username": "staff1", "name": "알바", "password": "stf123", "role": "staff"})["id"]
fails(lambda: pos.save_user({"username": "shortpw", "name": "짧은비번", "password": "123", "role": "staff"}), "6자 이상")
fails(lambda: pos.save_user({"username": "대문자X", "name": "a", "password": "123456", "role": "staff"}), "아이디는")
fails(lambda: pos.save_user({"username": "mgr", "name": "중복", "password": "123456", "role": "staff"}), "이미 사용")
fails(lambda: pos.save_user({"username": "whoami", "name": "a", "password": "123456", "role": "god"}), "권한이")
fails(lambda: pos.save_user({"username": "noname", "name": " ", "password": "123456", "role": "staff"}), "이름은 필수")
fails(lambda: pos.toggle_user({"id": 1}), "본인 계정")
act_as("manager")
fails(lambda: pos.save_user({"username": "z", "name": "a", "password": "123456", "role": "staff"}), "점주 이상")
fails(lambda: pos.save_settings({"point_rate": "1"}), "점주 이상")
fails(lambda: pos.audit_log({}), "점주 이상")

# 사업자번호 체크섬
act_as("owner")
fails(lambda: pos.save_settings({"store_biz_no": "220-81-62518"}), "올바르지 않습니다")

# ------------------------------------------------------------- 로그인 시도 제한
pos.LOGIN_FAILS.clear()
for i in range(pos.LOGIN_MAX):
    fails(lambda: pos.login("admin", "틀린비번"), "비밀번호가 올바르지")
# 한도를 넘기면 비밀번호가 맞아도 막힌다
fails(lambda: pos.login("admin", "owner123"), "잠겼습니다")
fails(lambda: pos.login("admin", "틀린비번"), "잠겼습니다")
# 다른 계정은 영향 없다 (한 명이 틀렸다고 매장 전체가 멈추면 안 된다)
fails(lambda: pos.login("mgr", "틀린비번"), "비밀번호가 올바르지")
# 잠금은 감사 로그에 남는다
act_as("owner")
assert any(a["action"] == "로그인잠금" and a["username"] == "admin"
           for a in pos.audit_log({})), "잠금이 감사 로그에 없음"
# 시간이 지나면 풀리고, 성공하면 카운터가 초기화된다
pos.LOGIN_FAILS["admin"][1] = pos.datetime.now() - pos.timedelta(seconds=1)
tok2, who2 = pos.login("admin", "owner123")
assert who2["role"] == "owner" and "admin" not in pos.LOGIN_FAILS, pos.LOGIN_FAILS
assert any(a["action"] == "로그인잠금해제" for a in pos.audit_log({})), "해제가 감사 로그에 없음"
# 풀린 뒤에는 다시 한도만큼 시도할 수 있다
for i in range(pos.LOGIN_MAX - 1):
    fails(lambda: pos.login("admin", "틀린비번"), "비밀번호가 올바르지")
assert pos.login("admin", "owner123")[1]["role"] == "owner"
# 없는 아이디로 무한 시도해도 메모리가 무한정 늘지 않는다
pos.LOGIN_FAILS.clear()
for i in range(pos.LOGIN_PRUNE_AT + 50):
    try:
        pos.login(f"nobody{i}", "x")
    except Bad:
        pass
assert len(pos.LOGIN_FAILS) <= pos.LOGIN_PRUNE_AT + 1, len(pos.LOGIN_FAILS)
pos.LOGIN_FAILS.clear()

# -------------------------------------------------------------- 만료 세션 정리
pos.SESSIONS.clear()
live_tok, _ = pos.login("admin", "owner123")
pos.SESSIONS["dead1"] = {"uid": 1, "name": "x", "role": "staff",
                         "exp": pos.datetime.now() - pos.timedelta(seconds=1)}
pos.SESSIONS["dead2"] = {"uid": 1, "name": "x", "role": "staff",
                         "exp": pos.datetime.now() - pos.timedelta(days=3)}
assert len(pos.SESSIONS) == 3
left = pos.prune_sessions()
assert left == 1 and list(pos.SESSIONS) == [live_tok], pos.SESSIONS
assert pos.prune_sessions() == 1, "두 번 돌려도 멀쩡한 세션은 그대로여야 한다"
pos.SESSIONS.clear()

# ---------------------------------------------------------------- 상품 / 판매
act_as("manager")
A = pos.save_product({"name": "커피", "price": 3000, "cost": 1000, "stock": 10})["id"]
B = pos.save_product({"name": "빵", "price": 5000, "cost": 2000, "stock": 2, "barcode": "123"})["id"]
act_as("staff")
fails(lambda: pos.save_product({"name": "직원이등록", "price": 1}), "매니저 이상")
C = pos.save_customer({"phone": "010-1234-5678", "name": "김단골"})["id"]
assert pos.save_customer({"phone": "01012345678"}) == {"id": C, "existing": True}
fails(lambda: pos.save_customer({"phone": "123"}), "전화번호")

# 비회원 판매
s = pos.create_sale({"items": [{"product_id": A, "qty": 2}], "pay_method": "cash"})
assert (s["subtotal"], s["total"], s["point_earned"]) == (6000, 6000, 0), s
assert (s["user_id"], s["uname"]) == (S, "알바"), s  # 누가 팔았는지 기록
assert pos.rows("SELECT stock FROM products WHERE id=?", (A,))[0]["stock"] == 8

# 회원 판매: 적립 10%, 할인 반영
s = pos.create_sale({"items": [{"product_id": A, "qty": 1}, {"product_id": B, "qty": 1}],
                     "customer_id": C, "discount": 1000, "pay_method": "card"})
assert (s["subtotal"], s["total"], s["point_earned"]) == (8000, 7000, 700), s
assert pos.customer_detail(C)["customer"]["points"] == 700

# 포인트 사용분은 적립 제외
s = pos.create_sale({"items": [{"product_id": A, "qty": 1}], "customer_id": C,
                     "point_used": 700, "pay_method": "card", "approval_no": "A1234"})
assert (s["total"], s["point_used"], s["point_earned"]) == (2300, 700, 230), s
assert s["approval_no"] == "A1234"
assert pos.customer_detail(C)["customer"]["points"] == 230

# 방어
fails(lambda: pos.create_sale({"items": [{"product_id": B, "qty": 99}], "pay_method": "cash"}), "재고 부족")
fails(lambda: pos.create_sale({"items": [{"product_id": A, "qty": 1}], "customer_id": C,
                               "point_used": 99999, "pay_method": "cash"}), "포인트 부족")
fails(lambda: pos.create_sale({"items": [{"product_id": A, "qty": 1}],
                               "point_used": 10, "pay_method": "cash"}), "고객을 선택")
fails(lambda: pos.create_sale({"items": [{"product_id": A, "qty": 1}], "discount": 99999,
                               "pay_method": "cash"}), "할인")
fails(lambda: pos.create_sale({"items": [], "pay_method": "cash"}), "판매 항목")
fails(lambda: pos.create_sale({"items": [{"product_id": A, "qty": 1}], "pay_method": "btc"}), "결제수단")

# 실패한 판매가 재고를 건드리지 않았는지 (트랜잭션 롤백)
stock_a = pos.rows("SELECT stock FROM products WHERE id=?", (A,))[0]["stock"]
fails(lambda: pos.create_sale({"items": [{"product_id": A, "qty": 1}, {"product_id": B, "qty": 99}],
                               "pay_method": "cash"}), "재고 부족")
assert pos.rows("SELECT stock FROM products WHERE id=?", (A,))[0]["stock"] == stock_a

# ------------------------------------------------------------- 판매취소 / 권한
fails(lambda: pos.void_sale({"id": s["id"]}), "매니저 이상")  # 직원은 못 한다
act_as("manager")
before = pos.customer_detail(C)["customer"]["points"]
pos.void_sale({"id": s["id"]})
assert pos.rows("SELECT stock FROM products WHERE id=?", (A,))[0]["stock"] == stock_a + 1
assert pos.customer_detail(C)["customer"]["points"] == before + 700 - 230
fails(lambda: pos.void_sale({"id": s["id"]}), "이미 취소")

# ---------------------------------------------------------------- 재고 / 포인트
act_as("staff")
assert pos.move_stock({"product_id": B, "delta": 5, "reason": "입고"})["stock"] == 6
fails(lambda: pos.move_stock({"product_id": B, "delta": -1, "reason": "폐기"}), "매니저 이상")
fails(lambda: pos.adjust_points({"customer_id": C, "delta": 100}), "매니저 이상")
act_as("manager")
assert pos.move_stock({"product_id": B, "delta": -1, "reason": "폐기"})["stock"] == 5
fails(lambda: pos.move_stock({"product_id": B, "delta": -99}), "재고 부족")
fails(lambda: pos.adjust_points({"customer_id": C, "delta": -99999}), "포인트 부족")
p0 = pos.customer_detail(C)["customer"]["points"]
assert pos.adjust_points({"customer_id": C, "delta": 500, "reason": "불만보상"})["points"] == p0 + 500
assert pos.adjust_points({"customer_id": C, "delta": -500, "reason": "오적립 정정"})["points"] == p0

# ------------------------------------------------------------------ 현금영수증
act_as("staff")
assert pos.norm_ident("소득공제", "010-1234-5678") == "01012345678"
fails(lambda: pos.norm_ident("소득공제", "123"), "식별번호")
fails(lambda: pos.norm_ident("지출증빙", "220-81-62518"), "올바르지 않습니다")
s = pos.create_sale({"items": [{"product_id": A, "qty": 1}], "pay_method": "cash",
                     "cash_receipt": {"kind": "소득공제", "ident_no": "010-1234-5678"}})
assert s["cash_receipt"]["status"] == "대기", s  # VAN 미연동이므로 수기입력 대기
assert s["receipts"] and s["receipts"][0]["kind"] == "소득공제", s  # 영수증에 찍히도록 포함
fails(lambda: pos.create_sale({"items": [{"product_id": A, "qty": 1}], "pay_method": "cash",
                               "cash_receipt": {"kind": "엉뚱", "ident_no": "01012345678"}}), "용도를 선택")
act_as("manager")
r = pos.cash_receipts({})[0]
assert r["status"] == "대기" and r["amount"] == 3000
pos.cash_receipt_manual({"id": r["id"], "approval_no": "CR999"})
assert pos.cash_receipts({})[0]["status"] == "발급"
fails(lambda: pos.cash_receipt_manual({"id": r["id"], "approval_no": ""}), "승인번호")
# 판매를 취소하면 현금영수증도 취소 상태가 된다
pos.void_sale({"id": s["id"]})
assert pos.cash_receipts({})[0]["status"] == "취소"

# ---------------------------------------------------------- VAN 연동 (가짜 단말)
calls = []


def fake_van(op, payload):
    calls.append((op, payload))
    if op == "approve" and payload["amount"] > 100000:
        raise Bad("승인 거절: 한도초과", 402)
    return {"ok": True, "approval_no": "VAN-" + op, "card_name": "신한"}


pos.VAN_URL, real_van = "http://fake", pos.van
pos.van = fake_van
act_as("staff")
s = pos.create_sale({"items": [{"product_id": A, "qty": 1}], "pay_method": "card"})
assert s["approval_no"] == "VAN-approve" and s["card_name"] == "신한", s
act_as("manager")
pos.void_sale({"id": s["id"]})
assert calls[-1][0] == "cancel" and calls[-1][1]["approval_no"] == "VAN-approve"

# 승인은 났는데 DB 기록이 깨지면 승인을 되돌려야 한다
calls.clear()
stock_a = pos.rows("SELECT stock FROM products WHERE id=?", (A,))[0]["stock"]
_real_quote = pos._quote
seen = []


def flaky_quote(c, *a):
    seen.append(1)
    if len(seen) > 1:  # 트랜잭션 안쪽 두 번째 호출에서 터뜨린다
        raise Bad("재고 부족: 동시 판매")
    return _real_quote(c, *a)


pos._quote = flaky_quote
fails(lambda: pos.create_sale({"items": [{"product_id": A, "qty": 1}], "pay_method": "card"}),
      "재고 부족")
pos._quote = _real_quote
assert [c[0] for c in calls] == ["approve", "cancel"], calls  # 승인 취소까지 갔는지
assert pos.rows("SELECT stock FROM products WHERE id=?", (A,))[0]["stock"] == stock_a
pos.van, pos.VAN_URL = real_van, None

# ------------------------------------------------------------- 정산 / 세금계산서
rep = pos.report({})
live = pos.rows("SELECT SUM(total) t FROM sales WHERE voided_at IS NULL")[0]["t"]
assert rep["summary"]["total"] == live
assert sum(u["total"] for u in rep["by_user"]) == live  # 직원별 합 == 전체
assert len(pos.report_csv({}).splitlines()) > 1

act_as("staff")
BIZ = pos.save_customer({"phone": "010-9999-8888", "name": "박사장",
                         "business_no": "220-81-62517", "biz_name": "(주)박사장상사",
                         "biz_email": "biz@example.com"})["id"]
fails(lambda: pos.save_customer({"phone": "010-9999-7777", "biz_email": "엉뚱주소"}), "이메일")
pos.create_sale({"items": [{"product_id": A, "qty": 2}], "customer_id": BIZ, "pay_method": "transfer"})
act_as("manager")
tax = pos.tax_summary({})
row = next(r for r in tax["rows"] if r["id"] == BIZ)
assert row["supply"] + row["vat"] == row["total"], row          # 공급가액+세액 == 합계
assert row["vat"] == row["total"] - round(row["total"] / 1.1)   # 부가세 10% 포함가 기준
assert len(pos.tax_csv({}).splitlines()) > 1
fails(lambda: pos.tax_summary({"month": "2026-13-99"}), "월 형식")

# --------------------------------------------------------------- 의류 SKU 일괄등록
act_as("manager")
r = pos.save_product_bulk({"style_code": "TS-9901", "name": "반팔티", "price": 19000,
                           "cost": 4000, "colors": "블랙, 화이트", "sizes": "S,M,L",
                           "stock": 5, "category": "상의"})
assert r == {"created": 6, "skipped": []}, r
sku = pos.list_products({"q": ["TS-9901"]})
assert len(sku) == 6 and all(p["stock"] == 5 for p in sku)
assert [p["size"] for p in sku if p["color"] == "블랙"] == ["S", "M", "L"]  # L<M<S 가 아니어야
assert sku[0]["barcode"] == "TS-9901-블랙-S" and sku[0]["name"] == "반팔티 블랙/S"
# 숫자 사이즈도 숫자 순서
pos.save_product_bulk({"style_code": "DN-9902", "name": "데님", "price": 59000,
                       "colors": "인디고", "sizes": "30,28,32,26"})
assert [p["size"] for p in pos.list_products({"q": ["DN-9902"]})] == ["26", "28", "30", "32"]
# 재실행하면 이미 있는 건 건너뛴다
r = pos.save_product_bulk({"style_code": "TS-9901", "name": "반팔티", "price": 19000,
                           "colors": "블랙,그레이", "sizes": "S,M,L"})
assert r["created"] == 3 and len(r["skipped"]) == 3, r
fails(lambda: pos.save_product_bulk({"style_code": "TS-9901", "name": "반팔티",
                                     "price": 1, "colors": "블랙", "sizes": "S"}), "이미 모두")
fails(lambda: pos.save_product_bulk({"name": "품번없음", "price": 1,
                                     "colors": "블랙", "sizes": "S"}), "품번은 필수")
fails(lambda: pos.save_product_bulk({"style_code": "X", "name": "색상없음",
                                     "price": 1, "sizes": "S"}), "색상과 사이즈")
fails(lambda: pos.save_product_bulk({"style_code": "X", "name": "너무많음", "price": 1,
                                     "colors": ",".join(str(i) for i in range(30)),
                                     "sizes": "S,M,L,XL,2XL,3XL,4XL"}), "200개까지")
# 일괄등록이 터지면 하나도 안 만들어진다 (한 트랜잭션)
n0 = len(pos.rows("SELECT id FROM products"))
fails(lambda: pos.save_product_bulk({"style_code": "BAD", "name": "음수가격",
                                     "price": -1, "colors": "블랙", "sizes": "S,M"}), "판매가")
assert len(pos.rows("SELECT id FROM products")) == n0

# ------------------------------------------------------------------- 일일 마감
TODAY = pos.datetime.now().strftime("%Y-%m-%d")
act_as("staff")
BLACK_M = next(p["id"] for p in pos.list_products({"q": ["TS-9901"]})
               if p["color"] == "블랙" and p["size"] == "M")
cash = pos.create_sale({"items": [{"product_id": BLACK_M, "qty": 1}], "pay_method": "cash"})
pos.create_sale({"items": [{"product_id": BLACK_M, "qty": 1}], "pay_method": "card"})

act_as("manager")
st = pos.closing_status()
assert st["date"] == TODAY
assert st["opening_cash"] == 100000                      # 설정 기본 시재금
assert st["cash_in"] >= 19000 and st["card_total"] >= 19000
assert st["expected_cash"] == st["opening_cash"] + st["cash_in"] - st["cash_out"]

# 당일 판매를 당일 취소하면 현금은 들어왔다 나가서 상쇄된다
exp_before = st["expected_cash"]
pos.void_sale({"id": cash["id"]})
st2 = pos.closing_status()
assert st2["cash_in"] == st["cash_in"] and st2["cash_out"] == st["cash_out"] + 19000
assert st2["expected_cash"] == exp_before - 19000, (exp_before, st2)

# 어제 현금판매를 오늘 취소하면 오늘 서랍에서만 빠진다
YDAY = (pos.datetime.now() - pos.timedelta(days=1)).strftime("%Y-%m-%d")
old = pos.create_sale({"items": [{"product_id": BLACK_M, "qty": 1}], "pay_method": "cash"})
with pos.tx() as c:
    c.execute("UPDATE sales SET at=? WHERE id=?", (YDAY + "T15:00:00", old["id"]))
st3 = pos.closing_status()
assert st3["cash_in"] == st2["cash_in"], "어제 판매가 오늘 입금에 잡히면 안 됨"
pos.void_sale({"id": old["id"]})
st4 = pos.closing_status()
assert st4["cash_in"] == st2["cash_in"], "어제 판매 취소가 오늘 입금에 잡히면 안 됨"
assert st4["cash_out"] == st2["cash_out"] + 19000, "어제 판매 환불은 오늘 서랍에서 나가야 함"

# 마감 저장 — 실제 현금이 모자라면 차액이 음수로 남는다
res = pos.save_closing({"counted_cash": st4["expected_cash"] - 5000, "memo": "5천원 비는 중"})
assert res["closing"]["diff"] == -5000, res["closing"]
assert res["closing"]["expected_cash"] == st4["expected_cash"]
assert res["closing"]["uname"] == "매니저"
# 다시 세서 재마감 가능 (감사로그에 남음)
res = pos.save_closing({"counted_cash": st4["expected_cash"]})
assert res["closing"]["diff"] == 0
assert len(pos.list_closings({})) == 1, "같은 날 마감은 한 줄만"
act_as("staff")
fails(lambda: pos.save_closing({"counted_cash": 1}), "매니저 이상")
fails(lambda: pos.closing_status(), "매니저 이상")
act_as("manager")
fails(lambda: pos.save_closing({"date": "2026/01/01", "counted_cash": 1}), "날짜 형식")
fails(lambda: pos.save_closing({"counted_cash": -1}), "실제 현금")

# 다음날 개시 시재금은 전날 마감 때 센 금액을 이어받는다
tomorrow = (pos.datetime.now() + pos.timedelta(days=1)).strftime("%Y-%m-%d")
assert pos.closing_status(tomorrow)["opening_cash"] == st4["expected_cash"]

# ------------------------------------------------------------ 영수증 프린터
act_as("manager")
pos.save_product({"name": "프린터테스트", "price": 12000, "cost": 5000, "stock": 10})
PRN = pos.list_products({"q": ["프린터테스트"]})[0]["id"]
act_as("staff")
psale = pos.create_sale({"items": [{"product_id": PRN, "qty": 2}], "pay_method": "cash"})

# 프린터 미설정이면 아무 일도 안 하고, 판매는 그대로다
assert pos.PRINTER is None
assert pos.print_receipt(psale["id"])["printed"] is False

# 가짜 네트워크 프린터를 띄워 실제로 받은 바이트를 본다
import socket as _sock, threading as _th

got = []


def _fake_printer(srv):
    conn, _ = srv.accept()
    with conn:
        buf = b""
        conn.settimeout(2)
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except Exception:
            pass
    got.append(buf)


_srv = _sock.socket()
_srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
_srv.bind(("127.0.0.1", 0))
_srv.listen(1)
_th.Thread(target=_fake_printer, args=(_srv,), daemon=True).start()
pos.PRINTER = f"127.0.0.1:{_srv.getsockname()[1]}"
assert pos.print_receipt(psale["id"])["printed"] is True
for _ in range(50):
    if got:
        break
    pos.time.sleep(0.05)
_srv.close()
assert got, "프린터가 아무것도 못 받았다"
raw = got[0]
assert raw.startswith(b"\x1b@"), "초기화 명령(ESC @)이 없다"
assert b"\x1dV" in raw, "용지 절단 명령이 없다"
txt = raw.decode(pos.PRINTER_CP, "replace")
assert "프린터테스트" in txt and "24,000" in txt, txt[:200]
assert "우리매장" in txt or "매장" in txt, txt[:200]

# 프린터가 죽어 있어도 판매는 되고, 실패가 감사 로그에 남는다
pos.PRINTER = "127.0.0.1:9"          # 아무것도 안 듣는 포트
r = pos.print_receipt(psale["id"])
assert r["printed"] is False and "실패" in r["reason"], r
act_as("staff")
ok_sale = pos.create_sale({"items": [{"product_id": PRN, "qty": 1}], "pay_method": "cash"})
assert ok_sale["id"], "프린터가 죽었다고 판매가 막히면 안 된다"
act_as("owner")
assert any(a["action"] == "영수증출력실패" for a in pos.audit_log({})), "실패가 감사 로그에 없음"
pos.PRINTER = None
act_as("manager")

# -------------------------------------------------------------- 요청 키(멱등성)
act_as("manager")
pos.save_product({"name": "멱등테스트", "price": 5000, "cost": 2000, "stock": 50})
IDEM = pos.list_products({"q": ["멱등테스트"]})[0]["id"]
stock0 = pos.list_products({"q": ["멱등테스트"]})[0]["stock"]
act_as("staff")

# 같은 키로 두 번 → 판매 한 건, 두 번째는 첫 결과를 그대로 돌려준다
a = pos.create_sale({"items": [{"product_id": IDEM, "qty": 2}], "pay_method": "cash",
                     "req_key": "key-aaa"})
b2 = pos.create_sale({"items": [{"product_id": IDEM, "qty": 2}], "pay_method": "cash",
                      "req_key": "key-aaa"})
assert a["id"] == b2["id"], (a["id"], b2["id"])
assert pos.list_products({"q": ["멱등테스트"]})[0]["stock"] == stock0 - 2, "재고가 두 번 깎였다"
assert len(pos.rows("SELECT id FROM sales WHERE req_key='key-aaa'")) == 1

# 내용이 달라도 같은 키면 첫 결과를 돌려준다 (재시도이지 새 판매가 아니다)
c3 = pos.create_sale({"items": [{"product_id": IDEM, "qty": 9}], "pay_method": "card",
                      "req_key": "key-aaa"})
assert c3["id"] == a["id"] and c3["total"] == a["total"], c3

# 다른 키는 새 판매
d = pos.create_sale({"items": [{"product_id": IDEM, "qty": 1}], "pay_method": "cash",
                     "req_key": "key-bbb"})
assert d["id"] != a["id"]
# 키 없이 보내면 지금까지와 똑같이 매번 새 판매 (하위호환)
e1 = pos.create_sale({"items": [{"product_id": IDEM, "qty": 1}], "pay_method": "cash"})
e2 = pos.create_sale({"items": [{"product_id": IDEM, "qty": 1}], "pay_method": "cash"})
assert e1["id"] != e2["id"]

# 재시도로 카드가 두 번 승인되면 안 된다 — 키 확인이 VAN 호출보다 먼저여야 한다
van_calls2 = []


def fake_van2(op, payload):
    van_calls2.append(op)
    return {"ok": True, "approval_no": "VAN-IDEM", "card_name": "신한"}


pos.VAN_URL, _real = "http://fake", pos.van
pos.van = fake_van2
f1 = pos.create_sale({"items": [{"product_id": IDEM, "qty": 1}], "pay_method": "card",
                      "req_key": "key-card"})
f2 = pos.create_sale({"items": [{"product_id": IDEM, "qty": 1}], "pay_method": "card",
                      "req_key": "key-card"})
assert f1["id"] == f2["id"]
assert van_calls2 == ["approve"], f"카드가 {len(van_calls2)}번 승인됐다: {van_calls2}"
pos.van, pos.VAN_URL = _real, None
act_as("manager")

# ----------------------------------------------------------------- 시즌 할인
act_as("manager")
pos.save_product_bulk({"style_code": "SS-01", "name": "시즌테스트", "price": 10000,
                       "cost": 4000, "colors": "블랙", "sizes": "S", "stock": 100})
SS = pos.list_products({"q": ["SS-01"]})[0]["id"]
today = pos.datetime.now().strftime("%Y-%m-%d")
yday = (pos.datetime.now() - pos.timedelta(days=1)).strftime("%Y-%m-%d")
tmrw = (pos.datetime.now() + pos.timedelta(days=1)).strftime("%Y-%m-%d")

# 할인 없을 때
act_as("staff")
s = pos.create_sale({"items": [{"product_id": SS, "qty": 2}], "pay_method": "cash"})
assert (s["list_total"], s["subtotal"], s["total"]) == (20000, 20000, 20000), s

# 30% 할인 — 판매가 = 정가 - (정가 x 30%), 원 단위 내림
act_as("manager")
pos.save_discount({"style_code": "SS-01", "rate": 30, "starts": today, "ends": tmrw})
act_as("staff")
s = pos.create_sale({"items": [{"product_id": SS, "qty": 2}], "pay_method": "cash"})
assert s["items"][0]["price"] == 7000 and s["items"][0]["list_price"] == 10000, s["items"]
assert (s["list_total"], s["subtotal"], s["total"]) == (20000, 14000, 14000), s

# 건별 수기할인·포인트는 **할인된 소계** 에서 뺀다
C2 = pos.save_customer({"phone": "010-7777-8888", "name": "할인고객"})["id"]
act_as("manager")
pos.adjust_points({"customer_id": C2, "delta": 5000, "reason": "테스트"})
act_as("staff")
s = pos.create_sale({"items": [{"product_id": SS, "qty": 2}], "customer_id": C2,
                     "discount": 1000, "point_used": 2000, "pay_method": "cash"})
assert (s["subtotal"], s["discount"], s["point_used"], s["total"]) == (14000, 1000, 2000, 11000), s
assert s["point_earned"] == 11000 * 10 // 100, s        # 적립은 결제금액 기준 (적립률 10%)

# 기간 밖이면 정가
act_as("manager")
pos.save_discount({"style_code": "SS-01", "rate": 30, "starts": yday, "ends": yday})
act_as("staff")
assert pos.create_sale({"items": [{"product_id": SS, "qty": 1}],
                        "pay_method": "cash"})["total"] == 10000
# 0% 로 되돌리면 정가
act_as("manager")
pos.save_discount({"style_code": "SS-01", "rate": 0, "starts": "", "ends": ""})
act_as("staff")
assert pos.create_sale({"items": [{"product_id": SS, "qty": 1}],
                        "pay_method": "cash"})["total"] == 10000

# 할인율을 바꿔도 **과거 판매 금액은 안 변한다**
# s 는 30% 할인 때 판 건(판매가 7,000 / 정가 10,000 / 결제 11,000)
act_as("manager")
pos.save_discount({"style_code": "SS-01", "rate": 70, "starts": today, "ends": tmrw})
again = pos.sale_detail(s["id"])
assert (again["items"][0]["price"], again["items"][0]["list_price"]) == (7000, 10000), again["items"]
assert (again["subtotal"], again["total"]) == (14000, 11000), again

# 방어
fails(lambda: pos.save_discount({"style_code": "SS-01", "rate": 101}), "할인율")
fails(lambda: pos.save_discount({"style_code": "SS-01", "rate": -1}), "할인율")
fails(lambda: pos.save_discount({"style_code": "", "rate": 10}), "품번")
fails(lambda: pos.save_discount({"style_code": "SS-01", "rate": 10, "starts": "2026/01/01"}), "날짜")
act_as("staff")
fails(lambda: pos.save_discount({"style_code": "SS-01", "rate": 10}), "매니저 이상")
act_as("manager")
assert any(a["action"] == "할인설정" for a in (act_as("owner"), pos.audit_log({}))[1]), "감사 로그 없음"
act_as("manager")

# ----------------------------------------------------------------- 재고 실사
act_as("manager")
pos.save_product_bulk({"style_code": "CNT-01", "name": "실사테스트", "price": 10000,
                       "cost": 5000, "colors": "블랙", "sizes": "S,M,L", "stock": 10})
cnt = pos.list_products({"q": ["CNT-01"]})
c_s, c_m, c_l = cnt[0], cnt[1], cnt[2]

# 센 수량과 장부가 다르면 그 차이만큼 조정한다
r = pos.apply_count({"items": [{"product_id": c_s["id"], "counted": 8},
                               {"product_id": c_m["id"], "counted": 12}], "note": "4분기 실사"})
assert {x["id"]: x["delta"] for x in r["adjusted"]} == {c_s["id"]: -2, c_m["id"]: 2}, r
after = {p["id"]: p["stock"] for p in pos.list_products({"q": ["CNT-01"]})}
assert after[c_s["id"]] == 8 and after[c_m["id"]] == 12
# 스캔 안 된 SKU 는 건드리지 않는다 (부분 실사)
assert after[c_l["id"]] == 10, "미스캔 SKU 가 바뀌었다"

# 기존 '실사조정' 장부를 그대로 쓴다 — 새 경로를 만들면 이력이 따로 논다
mv = pos.rows("SELECT delta,reason,note FROM stock_moves WHERE product_id=? ORDER BY id DESC",
              (c_s["id"],))[0]
assert (mv["delta"], mv["reason"], mv["note"]) == (-2, "실사조정", "4분기 실사"), mv
act_as("owner")
log = pos.audit_log({})
assert any(a["action"] == "재고실사" and "2개 SKU" in a["detail"] for a in log), "요약이 없음"
assert any(a["action"] == "재고실사조정" and "실사테스트 블랙/S" in a["detail"] for a in log), "SKU별 기록이 없음"
act_as("manager")

# 차이가 없으면 장부를 건드리지 않는다
n0 = len(pos.rows("SELECT id FROM stock_moves WHERE product_id=?", (c_l["id"],)))
assert pos.apply_count({"items": [{"product_id": c_l["id"], "counted": 10}]})["adjusted"] == []
assert len(pos.rows("SELECT id FROM stock_moves WHERE product_id=?", (c_l["id"],))) == n0

# 세는 동안 판매가 일어나면, 반영은 **그 시점 장부**와 비교해야 한다
act_as("staff")
pos.create_sale({"items": [{"product_id": c_l["id"], "qty": 3}], "pay_method": "cash"})
act_as("manager")
# 장부는 이제 7. 실사에서 10장을 셌다면 +3 이 되어야 한다 (판매 전 10과 비교해 0 이 아니라)
r = pos.apply_count({"items": [{"product_id": c_l["id"], "counted": 10}]})
assert r["adjusted"][0]["delta"] == 3, r
assert r["adjusted"][0]["before"] == 7, r

# 방어
fails(lambda: pos.apply_count({"items": []}), "반영할 항목")
fails(lambda: pos.apply_count({"items": [{"product_id": c_s["id"], "counted": -1}]}), "실사 수량")
fails(lambda: pos.apply_count({"items": [{"product_id": 999999, "counted": 1}]}), "없")
# 한 건이라도 실패하면 전부 되돌린다 (한 트랜잭션)
before = {p["id"]: p["stock"] for p in pos.list_products({"q": ["CNT-01"]})}
fails(lambda: pos.apply_count({"items": [{"product_id": c_s["id"], "counted": 99},
                                         {"product_id": 999999, "counted": 1}]}), "없")
assert {p["id"]: p["stock"] for p in pos.list_products({"q": ["CNT-01"]})} == before
act_as("staff")
fails(lambda: pos.apply_count({"items": [{"product_id": c_s["id"], "counted": 1}]}), "매니저 이상")
act_as("manager")

# --------------------------------------------------------- 제조사 택 바코드 등록
act_as("manager")
TAG = pos.save_product_bulk({"style_code": "TAG-01", "name": "택테스트", "price": 10000,
                             "cost": 5000, "colors": "블랙", "sizes": "S,M"})
sku = pos.list_products({"q": ["TAG-01"]})
s_sku, m_sku = sku[0], sku[1]
assert s_sku["barcode"] == "TAG-01-블랙-S"


def set_barcode(p, code):
    return pos.save_product({**{k: p[k] for k in ("id", "name", "cost", "price", "min_stock",
                                                  "category", "style_code", "color", "size")},
                             "barcode": code})


# 제조사 택 바코드로 교체
set_barcode(s_sku, "8809999001234")
assert pos.list_products({"q": ["8809999001234"]})[0]["id"] == s_sku["id"], "그 바코드로 안 잡힌다"
# 바코드 변경은 감사 로그에 남는다 (판매 경로의 핵심이므로)
act_as("owner")
assert any(a["action"] == "바코드변경" and "8809999001234" in a["detail"]
           for a in pos.audit_log({})), "바코드 변경이 감사 로그에 없음"
act_as("manager")
# 다른 상품이 쓰는 바코드면 **그 상품 이름을 알려주고** 막는다
fails(lambda: set_barcode(m_sku, "8809999001234"), "택테스트 블랙/S")
# 막혔으면 원래 바코드가 그대로여야 한다
assert pos.list_products({"q": ["TAG-01"]})[1]["barcode"] == "TAG-01-블랙-M"
# 공백만 떼고 그대로 저장한다 (대소문자·하이픈을 임의로 바꾸지 않는다)
set_barcode(m_sku, "  ab-CD_12  ")
assert pos.list_products({"q": ["TAG-01"]})[1]["barcode"] == "ab-CD_12"
# 비우면 바코드 없음으로 돌아간다
set_barcode(m_sku, "")
assert pos.list_products({"q": ["TAG-01"]})[1]["barcode"] is None
# 두 상품이 동시에 바코드 없음인 건 허용된다 (UNIQUE 는 NULL 을 중복으로 보지 않는다)
set_barcode(s_sku, "")
assert [p["barcode"] for p in pos.list_products({"q": ["TAG-01"]})] == [None, None]

# ------------------------------------------------------------------- 발주 제안
act_as("manager")
RO = pos.save_product({"name": "발주테스트", "price": 10000, "cost": 4000,
                       "stock": 10, "min_stock": 2, "style_code": "RO-01"})["id"]
ago = lambda n: (pos.datetime.now() - pos.timedelta(days=n)).strftime("%Y-%m-%dT12:00:00")


def sell(pid, qty, days_ago):
    """날짜를 거슬러 판매 한 건 기록 (판매속도 계산용). 재고는 넉넉히 채워 두고 판다."""
    with pos.tx() as c:
        c.execute("UPDATE products SET stock=stock+? WHERE id=?", (qty, pid))
    act_as("staff")
    s = pos.create_sale({"items": [{"product_id": pid, "qty": qty}], "pay_method": "cash"})
    with pos.tx() as c:
        c.execute("UPDATE sales SET at=? WHERE id=?", (ago(days_ago), s["id"]))
    act_as("manager")
    return s


def find(res, pid):
    return next(r for r in res["rows"] if r["id"] == pid)


# 취급 시작일을 20일 전으로 돌려놓고 20일간 40개 판매 => 하루 2개
with pos.tx() as c:
    c.execute("UPDATE stock_moves SET at=? WHERE product_id=?", (ago(20), RO))
for d in range(20):
    sell(RO, 2, d)
with pos.tx() as c:  # 재고는 판매와 무관하게 10개로 맞춰 계산만 본다
    c.execute("UPDATE products SET stock=10 WHERE id=?", (RO,))

r = find(pos.reorder({"days": ["30"], "lead": ["7"], "cover": ["14"]}), RO)
assert r["sell_days"] == 21 and r["sold"] == 40, r      # 20일 전부터 오늘까지 = 21일
assert r["rate"] == round(40 / 21, 2), r
assert r["target"] == pos.math.ceil(40 / 21 * 21), r    # 일평균 × (7+14)
assert r["qty"] == r["target"] - 10, r
assert r["amount"] == r["qty"] * 4000, r
assert r["flag"] == "보충"

# 리드타임/커버를 늘리면 발주량도 늘어난다
assert find(pos.reorder({"lead": ["14"], "cover": ["28"]}), RO)["qty"] > r["qty"]
# 재고가 충분하면 발주하지 않는다
with pos.tx() as c:
    c.execute("UPDATE products SET stock=9999 WHERE id=?", (RO,))
r2 = find(pos.reorder({}), RO)
assert r2["qty"] == 0 and r2["flag"] == "충분", r2

# 팔리는데 재고가 0이면 '품절'로 최우선
with pos.tx() as c:
    c.execute("UPDATE products SET stock=0 WHERE id=?", (RO,))
res = pos.reorder({})
assert find(res, RO)["flag"] == "품절"
assert res["rows"][0]["flag"] == "품절", "품절이 맨 위로 와야 함"

# 신상품은 '판매 가능일수'로 나눈다 — 30일로 나누면 속도가 깎여 발주가 안 나간다
NEW = pos.save_product({"name": "신상", "price": 10000, "cost": 5000, "stock": 2,
                        "style_code": "RO-02"})["id"]
with pos.tx() as c:
    c.execute("UPDATE stock_moves SET at=? WHERE product_id=?", (ago(3), NEW))
for d in range(4):
    sell(NEW, 3, d)
with pos.tx() as c:
    c.execute("UPDATE products SET stock=2 WHERE id=?", (NEW,))
n = find(pos.reorder({"days": ["30"], "lead": ["7"], "cover": ["14"]}), NEW)
assert n["sell_days"] == 4, n                       # 30이 아니라 4
assert n["rate"] == 3.0, n                          # 12개 / 4일
assert n["target"] == 63 and n["qty"] == 61, n      # 3 × 21 − 재고 2
assert n["days_left"] == round(2 / 3, 1), n

# 2주 넘게 안 팔린 재고는 발주 대상이 아니라 정리 대상
DEAD = pos.save_product({"name": "악성재고", "price": 10000, "cost": 7000, "stock": 5,
                         "min_stock": 0, "style_code": "RO-03"})["id"]
with pos.tx() as c:
    c.execute("UPDATE stock_moves SET at=? WHERE product_id=?", (ago(60), DEAD))
res = pos.reorder({})
d = find(res, DEAD)
assert (d["flag"], d["qty"], d["rate"]) == ("안팔림", 0, 0.0), d
assert res["summary"]["dead"] >= 1
assert res["summary"]["dead_amount"] >= 5 * 7000

# 취소된 판매는 속도에 반영되지 않는다
VOID = pos.save_product({"name": "취소분", "price": 10000, "cost": 1000, "stock": 5,
                         "style_code": "RO-04"})["id"]
with pos.tx() as c:
    c.execute("UPDATE stock_moves SET at=? WHERE product_id=?", (ago(10), VOID))
v = sell(VOID, 5, 5)
assert find(pos.reorder({}), VOID)["sold"] == 5
pos.void_sale({"id": v["id"]})
assert find(pos.reorder({}), VOID)["sold"] == 0, "취소분이 판매속도에 남으면 안 됨"

# 합계는 발주가 필요한 SKU만 더한다
res = pos.reorder({})
assert res["summary"]["qty"] == sum(x["qty"] for x in res["rows"])
assert res["summary"]["amount"] == sum(x["qty"] * x["cost"] for x in res["rows"])
assert res["summary"]["skus"] == sum(1 for x in res["rows"] if x["qty"])

# 발주서 CSV에는 필요한 SKU만, 합계 줄까지
csv_txt = pos.reorder_csv({})
assert csv_txt.splitlines()[0].startswith("품번")
assert len([l for l in csv_txt.splitlines() if l.strip()]) == res["summary"]["skus"] + 2
assert str(res["summary"]["amount"]) in csv_txt.splitlines()[-1]

fails(lambda: pos.reorder({"days": ["0"]}), "분석 기간")
fails(lambda: pos.reorder({"cover": ["999"]}), "커버 일수")
act_as("staff")
fails(lambda: pos.reorder({}), "매니저 이상")
act_as("manager")

# ----------------------------------------------------------------- 마감 누락 알림
act_as("manager")
MISS = pos.save_product({"name": "마감알림테스트", "price": 1000, "cost": 500, "stock": 999})["id"]


def sale_on(days_ago):
    act_as("staff")
    s = pos.create_sale({"items": [{"product_id": MISS, "qty": 1}], "pay_method": "cash"})
    act_as("manager")
    if days_ago:
        at = (pos.datetime.now() - pos.timedelta(days=days_ago)).strftime("%Y-%m-%dT14:00:00")
        with pos.tx() as c:
            c.execute("UPDATE sales SET at=? WHERE id=?", (at, s["id"]))
    return s


day = lambda n: (pos.datetime.now() - pos.timedelta(days=n)).strftime("%Y-%m-%d")
miss = lambda: [m["d"] for m in pos.unclosed_days()]

sale_on(1)                                   # 어제 판매, 마감 없음
assert day(1) in miss(), miss()
# 오늘은 영업 중이니 빼야 한다
assert day(0) not in miss(), miss()
# 7일 범위 밖은 안 나온다
sale_on(9)
assert day(9) not in miss(), miss()
# 불변식: 나오는 날은 전부 '미취소 판매가 있고' '마감 행이 없는' 날이어야 한다.
# (앞선 발주 테스트가 과거 날짜에 판매를 깔아둬서, 특정 날짜를 콕 집어 비교하면 안 된다)
for m in pos.unclosed_days():
    assert m["n"] > 0 and m["total"] > 0, m
    assert not pos.rows("SELECT 1 FROM closings WHERE d=?", (m["d"],)), m
    assert pos.rows("SELECT 1 FROM sales WHERE substr(at,1,10)=? AND voided_at IS NULL",
                    (m["d"],)), m
    assert day(0) > m["d"] >= day(7), m       # 오늘 제외, 7일 이내
# 마감하면 사라진다
st = pos.closing_status(day(1))
pos.save_closing({"date": day(1), "counted_cash": st["expected_cash"]})
assert day(1) not in miss(), miss()
# 금액·건수도 같이 준다 (알림에 "얼마짜리 하루인지" 보여주려면 필요).
# 그 날 다른 판매가 섞여 있을 수 있으니 DB 집계와 대조한다.
sale_on(2)
row = next(m for m in pos.unclosed_days() if m["d"] == day(2))
real = pos.rows("SELECT COUNT(*) n, SUM(total) total FROM sales"
                " WHERE substr(at,1,10)=? AND voided_at IS NULL", (day(2),))[0]
assert (row["n"], row["total"]) == (real["n"], real["total"]), (row, real)
# 취소된 판매만 남은 날은 조르지 않는다 (서랍이 안 움직였다).
# 그 날 판매를 전부 취소시켜서 확인한다 — 앞 테스트가 깔아둔 판매가 섞여 있으므로.
sale_on(5)
assert day(5) in miss(), miss()
for r in pos.rows("SELECT id FROM sales WHERE substr(at,1,10)=? AND voided_at IS NULL",
                  (day(5),)):
    pos.void_sale({"id": r["id"]})
assert day(5) not in miss(), miss()
act_as("staff")
fails(lambda: pos.unclosed_days(), "매니저 이상")
act_as("manager")

# --------------------------------------------------------------------- 백업
import glob as _glob

act_as("owner")
r = pos.backup_api({})
files = _glob.glob(os.path.join(pos.BACKUP_DIR, "pos-????????.db"))
assert len(files) == 1 and r["file"] in files[0], (r, files)
# 백업본이 실제로 열리고 같은 판매 건수를 담고 있는지
import sqlite3 as _sq

bk = _sq.connect(files[0])
assert bk.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == \
       pos.db().execute("SELECT COUNT(*) FROM sales").fetchone()[0]
assert bk.execute("SELECT COUNT(*) FROM closings").fetchone()[0] == \
       pos.db().execute("SELECT COUNT(*) FROM closings").fetchone()[0]
bk.close()
assert pos.list_backups()["files"][0]["size"] > 0

# 백업본을 실제로 열어 검증한다 — 깨진 걸 복구하려는 순간에 아는 게 최악이다
pos._verify_backup(files[0])                       # 방금 만든 백업은 통과해야 한다
junk = os.path.join(pos.BACKUP_DIR, "junk.db")
with open(junk, "wb") as f:
    f.write(b"this is not a database")
fails(lambda: pos._verify_backup(junk), "무결성")
# 열리긴 하지만 내용이 비어 있는 DB — 판매 건수가 안 맞으면 걸러야 한다
empty = os.path.join(pos.BACKUP_DIR, "empty.db")
_e = _sq.connect(empty)
_e.executescript("CREATE TABLE sales(id INTEGER PRIMARY KEY, total INTEGER)")
_e.commit(); _e.close()
fails(lambda: pos._verify_backup(empty), "건수")
os.remove(junk); os.remove(empty)

# 검증에 실패한 백업은 지운다. 쓸모없는 파일이 멀쩡한 백업을 밀어내면 안 된다.
good = os.path.join(pos.BACKUP_DIR, "pos-20200101.db")
import shutil as _sh

_sh.copyfile(files[0], good)
_real_verify = pos._verify_backup
pos._verify_backup = lambda path: (_ for _ in ()).throw(Bad("일부러 실패"))
fails(lambda: pos.backup_api({}), "일부러 실패")
pos._verify_backup = _real_verify
assert not os.path.exists(files[0]), "실패한 백업이 남아 있다"
assert os.path.exists(good), "멀쩡한 이전 백업까지 지워졌다"
os.remove(good)
assert pos.backup_api({})["file"], "다시 백업하면 정상 동작해야 한다"
act_as("manager")
fails(lambda: pos.backup_api({}), "점주 이상")
act_as("owner")

# ------------------------------------------------------------------- 감사로그
act_as("owner")
log = pos.audit_log({})
acts = [a["action"] for a in log]
for must in ("판매취소", "포인트조정", "재고폐기", "직원등록", "설정변경",
             "상품일괄등록", "일일마감", "재마감", "수동백업"):
    assert must in acts, f"감사로그 누락: {must} / {acts}"
assert all(a["username"] for a in log)  # 누가 했는지 항상 남는다

print("자가검증 통과 —", len(log), "건의 감사로그 기록됨")

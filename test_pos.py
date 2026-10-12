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
assert bk.execute("SELECT COUNT(*) FROM closings").fetchone()[0] == 1
bk.close()
assert pos.list_backups()["files"][0]["size"] > 0
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

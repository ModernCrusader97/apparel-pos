"""HTTP 레이어 스모크 — 실제로 서버를 띄워 로그인/권한/결제/증빙을 호출한다."""
import json, os, shutil, sqlite3, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

HERE = os.path.dirname(os.path.abspath(__file__))
B = "http://127.0.0.1:8732"
VAN_PORT = 9901
van_calls = []


class FakeVan(BaseHTTPRequestHandler):
    """가짜 카드단말. 10만원 넘으면 거절한다."""

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
        van_calls.append(req)
        ok = not (req["op"] == "approve" and req.get("amount", 0) > 100000)
        res = {"ok": ok, "approval_no": f"VAN{len(van_calls):04d}",
               "card_name": "신한", "message": "" if ok else "한도초과"}
        body = json.dumps(res).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def call(path, body=None, cookie=None, raw=False):
    req = urllib.request.Request(
        B + urllib.parse.quote(path, safe="/?=&"),
        json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        {"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
    try:
        with urllib.request.urlopen(req) as r:
            data = r.read().decode()
            return (r.status, data if raw else json.loads(data),
                    (r.headers.get("Set-Cookie") or "").split(";")[0])
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), ""


def check(label, got, want, extra=None):
    mark = "OK " if got == want else "실패"
    print(f"  {mark} {label}: {got}")
    if got != want:
        raise SystemExit(f"기대값 {want} / 응답: {extra}")


van = ThreadingHTTPServer(("127.0.0.1", VAN_PORT), FakeVan)
Thread(target=van.serve_forever, daemon=True).start()

DB = os.path.join(HERE, "smoke.db")
BK = os.path.join(HERE, "smoke-backup")
shutil.rmtree(BK, ignore_errors=True)
for f in (DB, DB + "-wal", DB + "-shm"):
    try:
        os.remove(f)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise SystemExit(f"이전 서버가 아직 {f} 를 잡고 있습니다: {e}")

srv = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "pos.py"), "--port", "8732", "--seed"],
    cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    env={**os.environ, "POS_DB": DB,
         "POS_ADMIN_PW": "owner123", "POS_BACKUP_DIR": BK,
         "POS_VAN_URL": f"http://127.0.0.1:{VAN_PORT}/pay"})
for _ in range(50):
    time.sleep(0.2)
    try:
        if call("/api/me")[0] == 401:
            break
    except Exception:
        pass
else:
    raise SystemExit("서버가 뜨지 않았습니다:\n" + srv.stdout.read())

try:
    print("[인증]")
    check("비로그인 차단", call("/api/products")[0], 401)
    check("틀린 비밀번호", call("/api/login", {"username": "admin", "password": "x"})[0], 401)
    st, who, OWNER = call("/api/login", {"username": "admin", "password": "owner123"})
    check("점주 로그인", who["role"], "owner")
    check("VAN 연동 인식", who["van"], True)

    print("[직원 계정]")
    st, body, _ = call("/api/users", {"username": "staff1", "name": "알바",
                                      "password": "stf123", "role": "staff"}, OWNER)
    check("직원 생성", st, 200, body)
    call("/api/users", {"username": "mgr1", "name": "매니저", "password": "mgr123",
                        "role": "manager"}, OWNER)
    _, _, STAFF = call("/api/login", {"username": "staff1", "password": "stf123"})
    _, _, MGR = call("/api/login", {"username": "mgr1", "password": "mgr123"})
    check("직원→정산 차단", call("/api/report", None, STAFF)[0], 403)
    check("직원→직원목록 차단", call("/api/users", None, STAFF)[0], 403)
    check("직원→설정저장 차단", call("/api/settings", {"point_rate": "1"}, STAFF)[0], 403)
    check("매니저→정산 허용", call("/api/report", None, MGR)[0], 200)
    check("매니저→직원목록 차단", call("/api/users", None, MGR)[0], 403)

    _, allp, _ = call("/api/products", None, STAFF)
    byname = {p["name"]: p for p in allp}
    P1 = byname["베이직 반팔티 블랙/M"]["id"]
    P2 = byname["울 니트 가디건 차콜/M"]["id"]
    P3 = byname["오버핏 코트 블랙/FREE"]["id"]        # 189,000 x2 = 한도초과 유발

    print("[판매 · 카드 VAN 승인]")
    van_calls.clear()
    st, sale, _ = call("/api/sales", {"items": [{"product_id": P1, "qty": 2}],
                                      "customer_id": 1, "pay_method": "card"}, STAFF)
    check("카드 판매", st, 200)
    check("VAN 승인번호 수신", sale["approval_no"].startswith("VAN"), True)
    check("담당 직원 기록", sale["uname"], "알바")
    check("단말 호출", van_calls[-1]["op"], "approve")

    print("[판매 · 현금영수증]")
    st, cash, _ = call("/api/sales", {"items": [{"product_id": P2, "qty": 1}],
                                      "pay_method": "cash",
                                      "cash_receipt": {"kind": "소득공제",
                                                       "ident_no": "010-1234-5678"}}, STAFF)
    check("현금영수증 발급", cash["cash_receipt"]["status"], "발급")
    check("단말 호출", van_calls[-1]["op"], "cash_receipt")
    check("식별번호 검증", call("/api/sales", {"items": [{"product_id": P2, "qty": 1}],
                                           "pay_method": "cash",
                                           "cash_receipt": {"kind": "소득공제",
                                                            "ident_no": "123"}}, STAFF)[0], 400)

    print("[판매취소 권한 + 카드 승인취소]")
    check("직원 취소 차단", call("/api/sales/void", {"id": sale["id"]}, STAFF)[0], 403)
    st, r, _ = call("/api/sales/void", {"id": sale["id"]}, MGR)
    check("매니저 취소 허용", st, 200)
    check("카드 승인취소 수행", r["van_cancelled"], True)
    check("재취소 차단", call("/api/sales/void", {"id": sale["id"]}, MGR)[0], 400)
    _, rec, _ = call("/api/receipts", None, MGR)
    check("판매취소 시 현금영수증 유지", [x["status"] for x in rec], ["발급"])

    print("[VAN 거절]")
    check("한도초과 거절", call("/api/sales", {"items": [{"product_id": P3, "qty": 2}],
                                          "pay_method": "card"}, STAFF)[0], 402)
    _, prods, _ = call("/api/products?q=오버핏", None, STAFF)
    check("거절 시 재고 보존", [p["stock"] for p in prods if p["id"] == P3], [2])

    print("[세금계산서]")
    call("/api/sales", {"items": [{"product_id": P1, "qty": 1}], "customer_id": 2,
                        "pay_method": "transfer"}, STAFF)
    _, tax, _ = call("/api/tax?month=" + time.strftime("%Y-%m"), None, MGR)
    row = tax["rows"][0]
    check("사업자 고객 집계", row["biz_name"], "(주)박사장상사")
    check("공급가액+세액=합계", row["supply"] + row["vat"] == row["total"], True)
    st, csv_body, _ = call("/api/tax.csv?month=" + time.strftime("%Y-%m"), None, MGR, raw=True)
    check("발행용 CSV", csv_body.splitlines()[0].startswith("﻿작성일자"), True)

    print("[의류 SKU]")
    st, r, _ = call("/api/products/bulk", {"style_code": "SK-01", "name": "플리츠 스커트",
                                          "colors": "블랙,베이지", "sizes": "S,M,L",
                                          "price": 49000, "stock": 4}, MGR)
    check("일괄등록", (st, r["created"]), (200, 6), r)
    _, sku, _ = call("/api/products?q=SK-01", None, STAFF)
    check("사이즈 정렬", [p["size"] for p in sku if p["color"] == "블랙"], ["S", "M", "L"])
    black_s = next(p for p in sku if p["color"] == "블랙" and p["size"] == "S")
    check("바코드 자동생성", black_s["barcode"], "SK-01-블랙-S")
    check("SKU 이름", black_s["name"], "플리츠 스커트 블랙/S")
    check("직원은 일괄등록 불가", call("/api/products/bulk", {"style_code": "X", "name": "x",
                                                      "colors": "a", "sizes": "b",
                                                      "price": 1}, STAFF)[0], 403)

    print("[발주 제안]")
    check("직원은 발주 조회 불가", call("/api/reorder", None, STAFF)[0], 403)
    _, ro, _ = call("/api/reorder?days=30&lead=7&cover=14", None, MGR)
    sold = next(r for r in ro["rows"] if r["id"] == P1)
    check("판매된 SKU 속도 계산", sold["sold"] > 0 and sold["rate"] > 0, True, sold)
    check("발주량 = 목표 − 재고", sold["qty"], max(0, sold["target"] - sold["stock"]), sold)
    check("합계 = 필요한 것만", ro["summary"]["qty"],
          sum(r["qty"] for r in ro["rows"]), ro["summary"])
    check("안 판 신상은 발주 안 함",
          all(r["qty"] == 0 for r in ro["rows"] if r["style_code"] == "SK-01"), True)
    _, wide, _ = call("/api/reorder?days=30&lead=30&cover=60", None, MGR)
    check("커버를 늘리면 발주가 는다",
          next(r for r in wide["rows"] if r["id"] == P1)["qty"] > sold["qty"], True)
    check("잘못된 파라미터", call("/api/reorder?days=1", None, MGR)[0], 400)
    st, po, _ = call("/api/reorder.csv?" + "days=30&lead=7&cover=14", None, MGR, raw=True)
    check("발주서 CSV", po.splitlines()[0].startswith("﻿품번"), True)
    check("CSV 합계줄", po.strip().splitlines()[-1].startswith("합계"), True)

    print("[일일 마감]")
    today = time.strftime("%Y-%m-%d")
    check("직원은 마감 불가", call("/api/closing", None, STAFF)[0], 403)
    _, cl, _ = call("/api/closing", None, MGR)
    check("예상 현금 = 개시＋현금매출−현금환불",
          cl["expected_cash"], cl["opening_cash"] + cl["cash_in"] - cl["cash_out"])
    st, done, _ = call("/api/closing", {"date": today, "opening_cash": 100000,
                                        "counted_cash": cl["expected_cash"] - 3000,
                                        "memo": "3천원 부족"}, MGR)
    check("마감 저장", st, 200, done)
    check("차액 계산", done["closing"]["diff"], -3000)
    check("마감자 기록", done["closing"]["uname"], "매니저")
    _, again, _ = call("/api/closing", {"date": today, "counted_cash": cl["expected_cash"]}, MGR)
    check("재마감 차액 0", again["closing"]["diff"], 0)
    _, hist, _ = call("/api/closings", None, MGR)
    check("같은 날은 한 줄", len([h for h in hist if h["d"] == today]), 1)

    print("[백업]")
    check("매니저는 백업 불가", call("/api/backups", {}, MGR)[0], 403)
    st, bk, _ = call("/api/backups", {}, OWNER)
    check("수동 백업", st, 200, bk)
    _, lst, _ = call("/api/backups", None, OWNER)
    check("백업 파일 존재", any(f["file"] == bk["file"] for f in lst["files"]), True)
    check("백업 파일 크기", lst["files"][0]["size"] > 0, True)
    bk_db = sqlite3.connect(os.path.join(lst["dir"], lst["files"][0]["file"]))
    check("백업본에 마감 포함",
          bk_db.execute("SELECT COUNT(*) FROM closings").fetchone()[0], 1)
    bk_db.close()

    print("[감사로그]")
    _, log, _ = call("/api/audit", None, OWNER)
    acts = {a["action"] for a in log}
    check("판매취소 기록", "판매취소" in acts, True)
    check("직원등록 기록", "직원등록" in acts, True)
    check("취소자 이름 기록", any(a["username"] == "매니저" for a in log), True)

    print("[로그아웃]")
    call("/api/logout", {}, STAFF)
    check("세션 폐기", call("/api/products", None, STAFF)[0], 401)

    print("\nHTTP 스모크 통과")
finally:
    srv.terminate()
    van.shutdown()
    time.sleep(0.5)
    shutil.rmtree(BK, ignore_errors=True)

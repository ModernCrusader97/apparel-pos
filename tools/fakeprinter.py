#!/usr/bin/env python3
"""가짜 감열 영수증 프린터. 실물 없이 ESC/POS 출력을 확인할 때 쓴다.

    python tools/fakeprinter.py          # 127.0.0.1:9100 대기
    POS_PRINTER=127.0.0.1:9100 python pos.py

받은 내용은 printed.txt 에 쌓인다. 표준 라이브러리만 쓴다.
"""
import argparse, datetime, socketserver

CP = "cp949"          # pos.py 의 POS_PRINTER_CP 와 맞출 것
OUT = "printed.txt"


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        buf = b""
        self.request.settimeout(2)
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            pass
        body = buf.split(b"\x1b@", 1)[-1].split(b"\x1dV", 1)[0]   # 초기화~절단 사이
        line = (f"\n{'=' * 44}\n[{datetime.datetime.now():%H:%M:%S}] {len(buf)}바이트"
                f" · 초기화 {buf[:2] == bytes([0x1b, 0x40])}"
                f" · 절단 {bytes([0x1d, 0x56]) in buf}\n"
                + body.decode(CP, "replace").rstrip() + "\n")
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(line)
        print(line, end="")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9100)
    a = ap.parse_args()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    print(f"가짜 프린터 대기: {a.host}:{a.port}  (출력은 {OUT} 에 쌓입니다)")
    socketserver.ThreadingTCPServer((a.host, a.port), Handler).serve_forever()

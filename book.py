# -*- coding: utf-8 -*-
"""KTX 코레일톡 예매/대기 매크로 (ADB + Windows OCR). A15 + S25 동시 실행."""

from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OCR_SCRIPT = ROOT / "ocr_worker.ps1"
ADB = "adb"
KORAIL_PKG = "com.korail.talk"
KORAIL_ACT = "com.korail.talk/com.korail.talk.MainActivity"

CARD_PIN = ""  # clone 후 입력: 카드 비밀번호 앞 2자리
AUTH_NO = ""  # clone 후 입력: 주민등록번호 앞 6자리
PHONE_MID = ""  # clone 후 입력: 휴대폰 중간 4자리
PHONE_END = ""  # clone 후 입력: 휴대폰 끝 4자리

TARGET_MONTH = "9월"
TARGET_DAY = "24일"
TARGET_TIME = "12:00"

BASE_W, BASE_H = 1080, 2340
LIST_TOP = 700

DEVICE_SPECS = [
    {
        "name": "A15",
        "ips": ("192.168.45.113:5555",),
        "markers": ("a15ks", "SM_A155N", "device:a15"),
    },
    {
        "name": "S25",
        "ips": ("192.168.45.226:5555",),
        "markers": ("pa1qksx", "SM_S931N", "device:pa1q"),
    },
]

_log_tls = threading.local()
_log_lock = threading.Lock()


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    name = getattr(_log_tls, "name", "")
    prefix = f"[{name}] " if name else ""
    line = f"[{ts}] {prefix}{msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(ROOT / "ktx.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    @property
    def norm(self) -> str:
        return re.sub(r"\s+", "", self.text)


class Adb:
    def __init__(self, name: str, serial: str) -> None:
        self.name = name
        self.serial = serial
        self.shot = ROOT / f"_screen_{name}.png"
        self.w, self.h = BASE_W, BASE_H
        self.refresh_xy = (900, 143)
        self._read_size()
        self.refresh_xy = self._detect_refresh()

    def sx(self, x: int | float) -> int:
        return int(round(x * self.w / BASE_W))

    def sy(self, y: int | float) -> int:
        return int(round(y * self.h / BASE_H))

    def xy(self, x: int | float, y: int | float) -> tuple[int, int]:
        return self.sx(x), self.sy(y)

    def run(self, *args: str, timeout: float = 20) -> str:
        cmd = [ADB, "-s", self.serial, *args]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "ignore").strip()
            raise RuntimeError(f"adb {' '.join(args)} failed: {err}")
        return r.stdout.decode("utf-8", "ignore")

    def _read_size(self) -> None:
        out = self.run("shell", "wm", "size")
        m = re.search(r"(\d+)x(\d+)", out)
        if m:
            self.w, self.h = int(m.group(1)), int(m.group(2))
        log(f"screen {self.w}x{self.h}")

    def _detect_refresh(self) -> tuple[int, int]:
        fallback = self.xy(900, 143)
        try:
            xml = self.run("exec-out", "uiautomator", "dump", "/dev/tty")
        except Exception as e:
            log(f"refresh dump fail: {e}")
            return fallback
        m = re.search(
            r'content-desc="새로고침"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml,
        )
        if not m:
            m = re.search(
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="새로고침"',
                xml,
            )
        if not m:
            log("refresh bounds missing, using scaled fallback")
            return fallback
        x1, y1, x2, y2 = map(int, m.groups())
        pt = ((x1 + x2) // 2, (y1 + y2) // 2)
        log(f"refresh {pt}")
        return pt

    def tap(self, x: int, y: int) -> None:
        x = max(1, min(self.w - 2, int(x)))
        y = max(1, min(self.h - 2, int(y)))
        log(f"tap ({x},{y})")
        self.run("shell", "input", "tap", str(x), str(y))

    def tap_base(self, x: int, y: int) -> None:
        self.tap(*self.xy(x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> None:
        self.run("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))

    def key(self, code: int) -> None:
        self.run("shell", "input", "keyevent", str(code))

    def type_digits(self, digits: str) -> None:
        try:
            self.run("shell", "input", "text", digits)
        except Exception:
            for ch in digits:
                if ch.isdigit():
                    self.key(7 + int(ch))
                    time.sleep(0.06)

    def stay_on(self) -> None:
        try:
            self.run("shell", "svc", "power", "stayon", "true")
        except Exception:
            pass
        try:
            self.run("shell", "settings", "put", "system", "screen_off_timeout", "1800000")
        except Exception:
            pass

    def wake(self) -> None:
        self.key(224)
        time.sleep(0.35)
        self.key(82)
        time.sleep(0.25)
        try:
            self.swipe(self.sx(540), self.sy(1900), self.sx(540), self.sy(700), 350)
        except Exception:
            pass
        time.sleep(0.4)

    def launch_korail(self) -> None:
        log("코레일톡 실행")
        try:
            self.run("shell", "am", "start", "-n", KORAIL_ACT)
        except Exception:
            self.run(
                "shell",
                "monkey",
                "-p",
                KORAIL_PKG,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            )
        time.sleep(2.5)

    def screenshot(self) -> Path:
        remote = f"/sdcard/tb_cap_{self.name}.png"
        self.run("shell", "screencap", "-p", remote)
        subprocess.run(
            [ADB, "-s", self.serial, "pull", "-q", remote, str(self.shot)],
            check=True,
            capture_output=True,
        )
        return self.shot


class OcrWorker:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(OCR_SCRIPT),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self.proc.stdout
        line = self._readline(timeout=40)
        if not line.startswith("READY:"):
            raise RuntimeError(f"OCR worker start failed: {line}")
        info = json.loads(line[6:])
        if not info.get("ok"):
            raise RuntimeError(f"OCR engine missing: {info}")
        log(f"OCR ready lang={info.get('lang')} available={info.get('available')}")

    def _readline(self, timeout: float = 30) -> str:
        assert self.proc.stdout
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"OCR worker exited: {err}")
            line = self.proc.stdout.readline()
            if line:
                return line.strip()
        raise TimeoutError("OCR worker timeout")

    def recognize(self, path: Path) -> tuple[str, list[Word]]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(str(path.resolve()) + "\n")
        self.proc.stdin.flush()
        line = self._readline(timeout=25)
        if not line.startswith("OCR:"):
            raise RuntimeError(f"bad OCR response: {line[:200]}")
        data = json.loads(line[4:])
        if "error" in data:
            raise RuntimeError(data["error"])
        raw = data.get("words") or []
        if isinstance(raw, dict):
            raw = [raw]
        words = [
            Word(text=w["t"], x=w["x"], y=w["y"], w=w["w"], h=w["h"])
            for w in raw
        ]
        return data.get("text") or "", words

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass


def joined(words: list[Word]) -> str:
    return re.sub(r"\s+", "", "".join(w.text for w in words))


def has(words: list[Word], needle: str) -> bool:
    return re.sub(r"\s+", "", needle) in joined(words)


def find_phrase(words: list[Word], needle: str) -> Word | None:
    n = re.sub(r"\s+", "", needle)
    if not n:
        return None
    for w in words:
        if n in w.norm or (len(w.norm) >= 2 and w.norm in n):
            if n in w.norm:
                return w
    for i in range(len(words)):
        acc = ""
        x1 = words[i].x
        y1 = words[i].y
        x2 = words[i].x + words[i].w
        y2 = words[i].y + words[i].h
        for j in range(i, min(i + 8, len(words))):
            w = words[j]
            if abs(w.cy - words[i].cy) > 50:
                break
            acc += w.norm
            x1 = min(x1, w.x)
            y1 = min(y1, w.y)
            x2 = max(x2, w.x + w.w)
            y2 = max(y2, w.y + w.h)
            if n in acc:
                return Word(n, x1, y1, x2 - x1, y2 - y1)
    return None


def bottom_words(words: list[Word], ymin: int) -> list[Word]:
    return [w for w in words if w.y >= ymin]


class Bot:
    def __init__(self, adb: Adb, ocr: OcrWorker) -> None:
        self.adb = adb
        self.ocr = ocr
        self.waitlist_done = False
        self.pay = {
            "scrolled": False,
            "card_tab": False,
            "pin": False,
            "auth": False,
            "agreed": False,
        }
        self.wait = {
            "special": False,
            "phone_chk": False,
            "phone": False,
            "privacy": False,
        }
        self.last_kind = ""
        self.same_kind_n = 0
        self._day24 = False
        self._hour12 = False

    def shot_ocr(self) -> tuple[str, list[Word]]:
        path = self.adb.screenshot()
        text, words = self.ocr.recognize(path)
        preview = re.sub(r"\s+", " ", text)[:180]
        log(f"OCR: {preview}")
        return text, words

    def click_word(self, w: Word, dx: int = 0, dy: int = 0) -> None:
        self.adb.tap(w.cx + dx, w.cy + dy)

    def click_phrase(self, words: list[Word], needle: str, dx: int = 0, dy: int = 0) -> bool:
        w = find_phrase(words, needle)
        if not w:
            return False
        self.click_word(w, dx, dy)
        return True

    def refresh(self) -> None:
        self.adb.tap(*self.adb.refresh_xy)

    def reset_flows_if_needed(self, kind: str) -> None:
        if kind != "payment":
            self.pay = {k: False for k in self.pay}
        if kind != "wait_form":
            self.wait = {k: False for k in self.wait}

    def classify(self, text: str, words: list[Word]) -> str:
        t = re.sub(r"\s+", "", text)
        sy = self.adb.sy
        if any(s in t for s in ("결제완료", "결제가완료", "발권완료", "예매가완료")):
            return "done_pay"
        if "대기신청이" in t or "신청이완료" in t or "예약대기가접수" in t:
            return "done_wait"
        if "서비스연결대기" in t or "사용자가많아" in t or "잠시기다리시면" in t:
            return "loading"
        if "목록을불러오고" in t or "불러오고있어요" in t:
            return "loading"
        if "꼭알아두세요" in t or "부정승차" in t:
            return "notice"
        if "이용안내" in t and ("장바구니" in t or "결제" in t):
            return "pay_popup"
        if "특실좌석포함" in t:
            return "wait_form"
        if "개인정보수집및이용동의" in t and find_phrase(bottom_words(words, sy(1800)), "동의"):
            if "특실" not in t:
                return "wait_consent"
        if find_phrase(bottom_words(words, sy(1950)), "바로예매") or find_phrase(words, "바로 예매"):
            if find_phrase(bottom_words(words, sy(1900)), "바로예매") or (
                find_phrase(words, "바로예매") and find_phrase(words, "좌석선택")
            ):
                return "book_sheet"
        if find_phrase(bottom_words(words, sy(1950)), "예약대기신청") and "특실좌석포함" not in t:
            return "wait_sheet"
        if t.startswith("결제") or (has(words, "결제할 티켓") or has(words, "카드 결제") or has(words, "자주쓰는 카드")):
            return "payment"
        if has(words, "출발 빠른") or has(words, "조회옵션") or has(words, "전체 열차"):
            return "list"
        return "unknown"

    def _list_top(self, words: list[Word]) -> int:
        header = find_phrase(words, "출발빠른") or find_phrase(words, "출발 빠른")
        if header:
            return header.y + header.h + 20
        return self.adb.sy(LIST_TOP)

    def _list_statuses(self, words: list[Word]) -> list[tuple[str, int]]:
        list_top = self._list_top(words)
        right_x = self.adb.sx(700)
        band_gap = self.adb.sy(42)
        right = [w for w in words if w.x >= right_x and w.y >= list_top]
        right.sort(key=lambda w: (w.y, w.x))
        bands: list[list[Word]] = []
        for w in right:
            if bands and w.y - bands[-1][0].y < band_gap:
                bands[-1].append(w)
            else:
                bands.append([w])
        statuses: list[tuple[str, int]] = []
        for band in bands:
            blob = joined(band)
            cy = sum(w.cy for w in band) // len(band)
            if "매진" in blob:
                statuses.append(("sold", cy))
            elif "대기" in blob or ("예약" in blob and "대기" in blob):
                statuses.append(("wait", cy))
            elif "자유석" in blob:
                statuses.append(("stand", cy))
            elif ("원" in blob and re.search(r"\d", blob)) or re.search(r"\d{1,3},\d{3}", blob):
                statuses.append(("price", cy))
        return statuses

    def do_list(self, words: list[Word]) -> None:
        statuses = self._list_statuses(words)
        log("  상태 " + ", ".join(f"{k}@{y}" for k, y in statuses))
        pair_gap = self.adb.sy(150)
        cards: list[tuple[tuple[str, int], tuple[str, int] | None]] = []
        i = 0
        while i < len(statuses):
            cur = statuses[i]
            nxt = statuses[i + 1] if i + 1 < len(statuses) else None
            if nxt and nxt[1] - cur[1] < pair_gap:
                cards.append((cur, nxt))
                i += 2
            else:
                cards.append((cur, None))
                i += 1
        book_y = None
        wait_y = None
        for general, special in cards:
            kind, y = general
            log(f"  카드 일반실={kind}@{y} 특실={special}")
            if kind == "price" and book_y is None:
                book_y = y
            elif kind == "wait" and wait_y is None:
                wait_y = y
            elif special and special[0] == "price" and book_y is None:
                book_y = special[1]
        mid_x = self.adb.sx(540)
        if book_y is not None:
            log("예매 가능 열차 클릭")
            self.adb.tap(mid_x, book_y)
            time.sleep(0.75)
            return
        if wait_y is not None and not self.waitlist_done:
            log("예약 대기 열차 클릭")
            self.adb.tap(mid_x, wait_y)
            time.sleep(0.75)
            return
        if not statuses:
            if self.same_kind_n >= 3:
                log("목록이 비어 새로고침")
                self.refresh()
                time.sleep(0.7)
                return
            log("목록 상태 없음 — 로딩으로 보고 대기")
            time.sleep(1.0)
            return
        log("매진만 있음 → 새로고침")
        self.refresh()
        time.sleep(0.7)

    def do_book_sheet(self, words: list[Word]) -> None:
        if self.click_phrase(words, "바로예매") or self.click_phrase(words, "바로 예매"):
            time.sleep(0.8)
            return
        self.adb.tap_base(810, 2180)
        time.sleep(0.8)

    def do_wait_sheet(self, words: list[Word]) -> None:
        if self.waitlist_done:
            self.refresh()
            time.sleep(0.5)
            return
        if self.click_phrase(words, "예약대기신청") or self.click_phrase(words, "예약 대기 신청"):
            time.sleep(0.8)
            return
        self.adb.tap_base(540, 2180)
        time.sleep(0.8)

    def do_pay_popup(self, words: list[Word]) -> None:
        cart = find_phrase(words, "장바구니")
        pays = [w for w in words if "결제" in w.norm]
        if cart and pays:
            right = [p for p in pays if p.cx > cart.cx]
            target = min(right, key=lambda p: abs(p.cy - cart.cy)) if right else pays[-1]
            self.click_word(target)
        elif not self.click_phrase(words, "결제"):
            self.adb.tap_base(780, 1180)
        time.sleep(0.9)

    def do_notice(self, words: list[Word]) -> None:
        if not self.click_phrase(words, "확인"):
            self.adb.tap_base(540, 2180)
        time.sleep(0.8)

    def do_payment(self, words: list[Word]) -> None:
        blob = joined(words)
        if not self.pay["scrolled"]:
            log("결제 화면 스크롤")
            self.adb.swipe(*self.adb.xy(540, 1700), *self.adb.xy(540, 500), 350)
            self.pay["scrolled"] = True
            time.sleep(0.45)
            return
        if "자주쓰는카드" not in blob and "자주쓰는" not in blob:
            if not self.click_phrase(words, "카드결제") and not self.click_phrase(words, "카드 결제"):
                pass
            time.sleep(0.3)
        if not self.pay["card_tab"]:
            if self.click_phrase(words, "자주쓰는카드") or self.click_phrase(words, "자주쓰는 카드"):
                self.pay["card_tab"] = True
                time.sleep(0.4)
                return
            self.adb.tap_base(810, 520)
            self.pay["card_tab"] = True
            time.sleep(0.4)
            return
        if not self.pay["pin"]:
            pin = find_phrase(words, "비밀번호")
            if pin:
                self.adb.tap(pin.cx, pin.cy + self.adb.sy(90))
            else:
                self.adb.tap_base(200, 980)
            time.sleep(0.35)
            self.adb.type_digits(CARD_PIN)
            self.pay["pin"] = True
            time.sleep(0.25)
            self.adb.key(4)
            time.sleep(0.2)
            return
        if not self.pay["auth"]:
            auth = find_phrase(words, "인증번호")
            if auth:
                self.adb.tap(auth.cx, auth.cy + self.adb.sy(90))
            else:
                box = find_phrase(words, "인증번호입력") or find_phrase(words, "인증번호 입력")
                if box:
                    self.click_word(box)
                else:
                    self.adb.tap_base(540, 1280)
            time.sleep(0.35)
            self.adb.type_digits(AUTH_NO)
            self.pay["auth"] = True
            time.sleep(0.25)
            self.adb.key(4)
            time.sleep(0.2)
            return
        if not self.pay["agreed"]:
            agree = find_phrase(words, "결제에동의") or find_phrase(words, "위내용을확인")
            if agree:
                self.adb.tap(min(self.adb.w - 60, agree.x + agree.w + self.adb.sx(80)), agree.cy)
            else:
                self.adb.tap_base(1010, 1980)
            self.pay["agreed"] = True
            time.sleep(0.35)
            return
        pay_btn = None
        for w in words:
            if "결제" in w.norm and w.y >= self.adb.sy(2050) and w.cx > self.adb.sx(500):
                pay_btn = w
                break
        if pay_btn:
            self.click_word(pay_btn)
        else:
            self.adb.tap_base(810, 2230)
        time.sleep(1.0)

    def do_wait_form(self, words: list[Word]) -> None:
        blob = joined(words)
        if not self.wait["special"]:
            w = find_phrase(words, "특실좌석포함") or find_phrase(words, "특실 좌석 포함")
            if w:
                self.adb.tap(max(self.adb.sx(60), w.x - self.adb.sx(80)), w.cy)
            else:
                self.adb.tap_base(90, 280)
            self.wait["special"] = True
            time.sleep(0.4)
            return
        if not self.wait["phone_chk"]:
            w = find_phrase(words, "휴대폰번호입력") or find_phrase(words, "안내받을휴대폰")
            if w:
                self.adb.tap(max(self.adb.sx(60), w.x - self.adb.sx(80)), w.cy)
            else:
                self.adb.tap_base(90, 470)
            self.wait["phone_chk"] = True
            time.sleep(0.5)
            return
        if not self.wait["phone"]:
            zeros = [w for w in words if w.norm in ("0000", "00000") or w.text == "0000"]
            if len(zeros) >= 2:
                zeros = sorted(zeros, key=lambda z: z.x)
                self.click_word(zeros[0])
                time.sleep(0.25)
                self.adb.type_digits(PHONE_MID)
                time.sleep(0.15)
                self.click_word(zeros[1])
                time.sleep(0.25)
                self.adb.type_digits(PHONE_END)
            else:
                w = find_phrase(words, "010")
                if w:
                    self.adb.tap(w.cx + self.adb.sx(220), w.cy)
                    time.sleep(0.25)
                    self.adb.type_digits(PHONE_MID)
                    time.sleep(0.15)
                    self.adb.tap(w.cx + self.adb.sx(480), w.cy)
                    time.sleep(0.25)
                    self.adb.type_digits(PHONE_END)
                else:
                    self.adb.tap_base(540, 620)
                    time.sleep(0.2)
                    self.adb.type_digits(PHONE_MID)
                    self.adb.tap_base(820, 620)
                    time.sleep(0.2)
                    self.adb.type_digits(PHONE_END)
            self.wait["phone"] = True
            time.sleep(0.2)
            self.adb.key(4)
            time.sleep(0.25)
            return
        if not self.wait["privacy"]:
            w = find_phrase(words, "개인정보수집") or find_phrase(words, "개인정보 수집")
            if w:
                self.adb.tap(max(self.adb.sx(60), w.x - self.adb.sx(80)), w.cy)
            else:
                self.adb.tap_base(90, 1760)
            self.wait["privacy"] = True
            time.sleep(0.6)
            return
        if "동의" in blob and find_phrase(bottom_words(words, self.adb.sy(1700)), "동의"):
            self.click_phrase(words, "동의")
            time.sleep(0.5)
            return
        if self.click_phrase(words, "신청"):
            self.waitlist_done = True
            time.sleep(1.0)
            return
        self.adb.tap_base(540, 2180)
        self.waitlist_done = True
        time.sleep(1.0)

    def do_wait_consent(self, words: list[Word]) -> None:
        if not self.click_phrase(words, "동의"):
            self.adb.tap_base(540, 2180)
        time.sleep(0.6)

    def step(self) -> str | None:
        text, words = self.shot_ocr()
        kind = self.classify(text, words)
        if kind == self.last_kind:
            self.same_kind_n += 1
        else:
            self.same_kind_n = 0
            self.last_kind = kind
        self.reset_flows_if_needed(kind)
        log(f"화면={kind}")
        if kind == "done_pay":
            return "booked"
        if kind == "done_wait":
            self.waitlist_done = True
            time.sleep(0.8)
            self.adb.key(4)
            return None
        if kind == "notice":
            self.do_notice(words)
        elif kind == "loading":
            log("서버 대기열 — 새로고침하지 않고 대기")
            time.sleep(1.2)
        elif kind == "pay_popup":
            self.do_pay_popup(words)
        elif kind == "book_sheet":
            self.do_book_sheet(words)
        elif kind == "wait_sheet":
            self.do_wait_sheet(words)
        elif kind == "wait_form":
            self.do_wait_form(words)
        elif kind == "wait_consent":
            self.do_wait_consent(words)
        elif kind == "payment":
            self.do_payment(words)
        elif kind == "list":
            self.do_list(words)
        else:
            log("알 수 없는 화면, 잠시 대기")
            if self.same_kind_n >= 4:
                self.refresh()
                self.same_kind_n = 0
            time.sleep(0.6)
        return None

    def _blob(self, words: list[Word]) -> str:
        return joined(words)

    def _is_target_query(self, text: str) -> bool:
        blob = re.sub(r"\s+", "", text)
        date_ok = TARGET_MONTH in blob and TARGET_DAY in blob
        time_ok = (
            TARGET_TIME in blob
            or "12시이후" in blob
            or "12:00시이후" in blob
            or "12시" in blob
        )
        return date_ok and time_ok

    def _is_train_list(self, kind: str, text: str) -> bool:
        blob = re.sub(r"\s+", "", text)
        return kind in ("list", "loading") or (
            "출발빠른" in blob and ("조회옵션" in blob or "전체열차" in blob)
        )

    def _is_home(self, text: str) -> bool:
        blob = re.sub(r"\s+", "", text)
        return "어디로떠날" in blob or ("가는날" in blob and "열차조회" in blob) or (
            "간편예매" in blob and "열차조회" in blob
        )

    def _is_date_picker(self, text: str, words: list[Word]) -> bool:
        blob = re.sub(r"\s+", "", text)
        week = sum(1 for d in ("일요일", "월요일") if d in blob)
        days = ["일", "월", "화", "수", "목", "금", "토"]
        if sum(1 for d in days if d in blob) >= 5:
            return True
        hour_hits = [w for w in words if "시" in w.norm and w.y > self.adb.sy(1400)]
        if len(hour_hits) >= 3:
            return True
        nums = [w for w in words if re.fullmatch(r"[1-9]|[12][0-9]|3[01]", w.norm)]
        return len(nums) >= 15

    def _tap_next_month(self, words: list[Word]) -> bool:
        if self.click_phrase(words, "다음달") or self.click_phrase(words, "다음 달"):
            return True
        for w in words:
            if ("월" in w.norm or "2026" in w.norm) and w.y < self.adb.sy(520):
                self.adb.tap(min(self.adb.w - 70, w.cx + self.adb.sx(220)), w.cy)
                return True
        return False

    def _tap_day_24(self, words: list[Word]) -> bool:
        cands = [
            w
            for w in words
            if w.norm in ("24", "24일") and self.adb.sy(420) < w.cy < self.adb.sy(1500)
        ]
        if not cands:
            return False
        cands.sort(key=lambda w: abs(w.cy - self.adb.sy(950)))
        self.click_word(cands[0])
        return True

    def _tap_hour_12(self, words: list[Word]) -> bool:
        cands = [
            w
            for w in words
            if ("12시" in w.norm or w.norm in ("12", "12:00")) and w.y > self.adb.sy(1200)
        ]
        if not cands:
            cands = [w for w in words if w.norm in ("12시", "12시이후")]
        if not cands:
            return False
        cands.sort(key=lambda w: w.y, reverse=True)
        self.click_word(cands[0])
        return True

    def _swipe_hours_left(self, words: list[Word]) -> None:
        hours = [w for w in words if "시" in w.norm]
        y = max(w.cy for w in hours) if hours else self.adb.sy(1750)
        log("12시가 안 보여 시간 줄을 왼쪽으로 슬라이드")
        self.adb.swipe(self.adb.sx(920), y, self.adb.sx(160), y, 280)
        time.sleep(0.45)

    def ensure_target_list(self, tries: int = 40) -> bool:
        log(f"목표 화면: {TARGET_MONTH} {TARGET_DAY} {TARGET_TIME}시 이후 열차 목록")
        for n in range(tries):
            text, words = self.shot_ocr()
            kind = self.classify(text, words)
            blob = self._blob(words)
            log(f"복구 {n + 1}/{tries} 화면={kind} target={self._is_target_query(text)}")
            if kind in ("book_sheet", "wait_sheet", "pay_popup", "payment"):
                log("예매 진행 화면이라 복구 중단하고 모니터링")
                return True
            if self._is_train_list(kind, text) and self._is_target_query(text):
                log("9월 24일 12:00 이후 목록 확인")
                try:
                    self.adb.refresh_xy = self.adb._detect_refresh()
                except Exception:
                    pass
                return True
            if "점검" in blob or "시스템점검" in blob:
                log("시스템 점검 중 — 대기 후 앱 재실행")
                time.sleep(8)
                self.adb.wake()
                self.adb.launch_korail()
                continue
            acted = False
            if kind == "notice":
                self.do_notice(words)
                acted = True
            elif self._is_date_picker(text, words):
                log("날짜/시간 선택 화면")
                if TARGET_MONTH not in blob:
                    acted = self._tap_next_month(words)
                elif not self._day24:
                    if self._tap_day_24(words):
                        self._day24 = True
                        acted = True
                elif not self._hour12:
                    if self._tap_hour_12(words):
                        self._hour12 = True
                        acted = True
                    else:
                        self._swipe_hours_left(words)
                        acted = True
                else:
                    acted = self.click_phrase(words, "확인") or self.click_phrase(words, "완료")
                    if not acted:
                        self.adb.tap_base(540, 2180)
                        acted = True
            elif self._is_home(text):
                self._day24 = False
                self._hour12 = False
                if self._is_target_query(text):
                    log("홈에서 날짜 확인됨 → 열차 조회")
                    acted = self.click_phrase(words, "열차조회") or self.click_phrase(words, "열차 조회")
                else:
                    log("가는날 클릭")
                    w = find_phrase(words, "가는날")
                    if w:
                        self.adb.tap(min(self.adb.w - 80, w.cx + self.adb.sx(250)), w.cy)
                        acted = True
                    else:
                        w = find_phrase(words, "2026년") or find_phrase(words, "월")
                        if w:
                            self.click_word(w)
                            acted = True
            elif "조회하기" in blob and self._is_target_query(text):
                acted = self.click_phrase(words, "조회하기")
            elif self.click_phrase(words, "승차권예매") or self.click_phrase(words, "승차권 예매"):
                acted = True
            elif "닫기" in blob and self.click_phrase(words, "닫기"):
                acted = True
            elif "확인" in blob and "결제" not in blob and self.click_phrase(words, "확인"):
                acted = True
            if self._is_train_list(kind, text) and not self._is_target_query(text):
                log("목록이지만 날짜가 다름 — 뒤로 가서 가는날 재선택")
                self.adb.key(4)
                acted = True
            if not acted:
                log("조회 화면이 아니라 앱을 다시 실행")
                self.adb.wake()
                self.adb.launch_korail()
            time.sleep(1.1)
        log("목표 목록이 아직 없음 — 모니터링은 계속 시도")
        return False

    def recover_to_list(self, tries: int = 28) -> bool:
        return self.ensure_target_list(tries)


def _adb_lines() -> list[str]:
    out = subprocess.check_output([ADB, "devices", "-l"], text=True, encoding="utf-8")
    log(out.strip())
    return [
        ln
        for ln in out.splitlines()[1:]
        if ln.strip() and re.search(r"\bdevice\b", ln) and "offline" not in ln
    ]


def selected_specs() -> list[dict]:
    only = None
    if "--only" in sys.argv:
        i = sys.argv.index("--only")
        if i + 1 < len(sys.argv):
            only = sys.argv[i + 1].strip().upper()
    if only:
        specs = [s for s in DEVICE_SPECS if s["name"].upper() == only]
        if not specs:
            raise RuntimeError(f"알 수 없는 기기: {only}")
        return specs
    return list(DEVICE_SPECS)


def find_devices() -> list[Adb]:
    specs = selected_specs()
    for spec in specs:
        for ip in spec["ips"]:
            r = subprocess.run([ADB, "connect", ip], capture_output=True, text=True, encoding="utf-8")
            log(f"connect {ip}: {(r.stdout or r.stderr or '').strip()}")
    lines = _adb_lines()
    tokens = {ln.split()[0]: ln for ln in lines}
    found: list[Adb] = []
    used: set[str] = set()
    for spec in specs:
        serial = None
        for ip in spec["ips"]:
            if ip in tokens and ip not in used:
                serial = ip
                break
        if not serial:
            for ln in lines:
                token = ln.split()[0]
                if token in used or ":" in token:
                    continue
                low = ln.lower()
                if any(m.lower() in low for m in spec["markers"]):
                    serial = token
                    break
        if serial:
            used.add(serial)
            log(f"found {spec['name']} serial={serial}")
            found.append(Adb(spec["name"], serial))
        else:
            log(f"{spec['name']} 없음 — 건너뜀")
    if not found:
        raise RuntimeError("연결된 대상 폰이 없습니다. 무선 ADB(5555)를 확인하세요.")
    return found


def test_ocr_samples(ocr: OcrWorker) -> None:
    samples = sorted(ROOT.glob("*.png"))
    for p in samples:
        if p.name.startswith("_"):
            continue
        try:
            text, words = ocr.recognize(p)
        except Exception as e:
            log(f"OCR fail {p.name}: {e}")
            continue
        preview = re.sub(r"\s+", " ", text)[:200]
        log(f"[sample {p.name}] {preview}")
        log(f"  words={len(words)} has_sold={'매진' in text} has_won={'원' in text} has_wait={'대기' in text}")


def wait_until(at: str) -> None:
    hh, mm = map(int, at.split(":"))
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    log(f"{target.strftime('%Y-%m-%d %H:%M')}까지 대기")
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000040)
    except Exception:
        pass
    while True:
        left = (target - datetime.now()).total_seconds()
        if left <= 0:
            break
        log(f"시작까지 {int(left)}초")
        time.sleep(min(30, max(1, left)))
    log("예약 시각 도달")


def run_device(adb: Adb, once: bool) -> None:
    _log_tls.name = adb.name
    adb.stay_on()
    adb.wake()
    adb.launch_korail()
    ocr = OcrWorker()
    try:
        log(f"모니터링 시작 serial={adb.serial}")
        bot = Bot(adb, ocr)
        bot.recover_to_list(28)
        while True:
            try:
                result = bot.step()
            except Exception as e:
                log(f"step error: {e}")
                time.sleep(0.8)
                continue
            if result == "booked":
                log("예매/결제 완료 — 이 기기만 종료, 다른 기기는 계속")
                break
            if once:
                log("한번만 실행 후 종료")
                break
    finally:
        ocr.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    once = "--once" in sys.argv
    if "--test-ocr" in sys.argv:
        ocr = OcrWorker()
        try:
            test_ocr_samples(ocr)
        finally:
            ocr.close()
        return
    at = None
    if "--at" in sys.argv:
        i = sys.argv.index("--at")
        if i + 1 < len(sys.argv):
            at = sys.argv[i + 1]
    if at:
        log(f"{at}에 코레일톡을 실행한 뒤 모니터링합니다. PC는 켜 두세요.")
        subprocess.run([ADB, "devices", "-l"], capture_output=True)
        wait_until(at)
    devices = find_devices()
    log("KTX 모니터링 시작: " + ", ".join(d.name for d in devices) + " (Ctrl+C 종료)")
    threads = [
        threading.Thread(target=run_device, args=(adb, once), name=adb.name, daemon=True)
        for adb in devices
    ]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.4)
    except KeyboardInterrupt:
        log("중지 요청")


if __name__ == "__main__":
    main()

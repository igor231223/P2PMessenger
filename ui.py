import json
import os
import sqlite3
import sys
import asyncio
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog,
    QHBoxLayout, QVBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QFrame, QStackedWidget,
    QSizePolicy,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSettings
from PyQt6.QtGui import QFont, QColor, QPainter, QBrush, QPen

from peer import PeerProtocol

# ── palette ───────────────────────────────────────────────────────────────────
BG       = "#17212B"
SIDEBAR  = "#0E1621"
INPUT_BG = "#151F2B"
TEXT     = "#FFFFFF"
TEXT2    = "#708899"
ACCENT   = "#5288C1"
GREEN    = "#4DCA5D"
RED      = "#E06C75"
ORANGE   = "#E5A044"
DIVIDER  = "#0A1520"
HOVER    = "#1C2D3E"
ACTIVE   = "#243447"

_DATA_DIR = os.path.join(os.path.expanduser('~'), '.p2pmessenger')
_INI      = os.path.join(_DATA_DIR, 'config.ini')
_DB_PATH  = os.path.join(_DATA_DIR, 'messages.db')
os.makedirs(_DATA_DIR, exist_ok=True)

DEFAULT_HOST      = "193.188.20.124"
DEFAULT_UDP_PORT  = 5555
DEFAULT_HTTP_PORT = 5556

_host      = DEFAULT_HOST
_udp_port  = DEFAULT_UDP_PORT
_http_port = DEFAULT_HTTP_PORT


def _http_base():
    return f"http://{_host}:{_http_port}"


def _rendezvous():
    return (_host, _udp_port)


def _load_server_cfg(s: QSettings):
    global _host, _udp_port, _http_port
    _host      = s.value("server/host",      DEFAULT_HOST)
    _udp_port  = int(s.value("server/udp_port",  DEFAULT_UDP_PORT))
    _http_port = int(s.value("server/http_port", DEFAULT_HTTP_PORT))


def _save_server_cfg(s: QSettings):
    s.setValue("server/host",      _host)
    s.setValue("server/udp_port",  _udp_port)
    s.setValue("server/http_port", _http_port)


# ── local message DB ──────────────────────────────────────────────────────────

class LocalDB:
    def __init__(self, my_nick):
        self._nick = my_nick
        conn = sqlite3.connect(_DB_PATH)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY,
                my_nick   TEXT NOT NULL,
                peer_nick TEXT NOT NULL,
                text      TEXT NOT NULL,
                is_mine   INTEGER NOT NULL,
                ts        TEXT NOT NULL
            )
        ''')
        conn.commit()
        conn.close()

    def save(self, peer_nick, text, is_mine):
        ts = datetime.now().strftime("%H:%M")
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            'INSERT INTO messages (my_nick, peer_nick, text, is_mine, ts) VALUES (?,?,?,?,?)',
            (self._nick, peer_nick, text, 1 if is_mine else 0, ts)
        )
        conn.commit()
        conn.close()

    def load(self, peer_nick):
        conn = sqlite3.connect(_DB_PATH)
        rows = conn.execute(
            'SELECT text, is_mine, ts FROM messages '
            'WHERE my_nick=? AND peer_nick=? ORDER BY id',
            (self._nick, peer_nick)
        ).fetchall()
        conn.close()
        return [(r[0], bool(r[1]), r[2]) for r in rows]

    def get_contacts(self):
        conn = sqlite3.connect(_DB_PATH)
        rows = conn.execute('''
            SELECT m.peer_nick, m.text, m.is_mine, m.ts
            FROM messages m
            INNER JOIN (
                SELECT peer_nick, MAX(id) AS mid
                FROM messages WHERE my_nick=?
                GROUP BY peer_nick
            ) lat ON m.peer_nick = lat.peer_nick AND m.id = lat.mid
            WHERE m.my_nick=?
            ORDER BY m.id DESC
        ''', (self._nick, self._nick)).fetchall()
        conn.close()
        return [
            (r[0], ('Вы: ' if r[2] else '') + r[1], r[3])
            for r in rows
        ]


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _http(method, path, data=None, params=None, timeout=8):
    url = _http_base() + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    body    = json.dumps(data).encode() if data else None
    headers = {'Content-Type': 'application/json'} if body else {}
    req     = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get('error', str(e))
        except Exception:
            err = str(e)
        return None, err
    except Exception as e:
        return None, str(e)


class HttpWorker(QThread):
    done   = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, method, path, data=None, params=None):
        super().__init__()
        self._m, self._p, self._d, self._q = method, path, data, params

    def run(self):
        r, e = _http(self._m, self._p, self._d, self._q)
        if e:
            self.failed.emit(e)
        else:
            self.done.emit(r)


# ── UI helpers ────────────────────────────────────────────────────────────────

class Avatar(QWidget):
    def __init__(self, letter, size=40, color=ACCENT, parent=None):
        super().__init__(parent)
        self.letter = letter.upper()
        self.color  = QColor(color)
        self.setFixedSize(size, size)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(self.color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, self.width(), self.height())
        p.setPen(QPen(QColor("white")))
        p.setFont(QFont("Segoe UI", self.width() // 3, QFont.Weight.Bold))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.letter)


_DLG_QSS = f"""
    QDialog  {{ background: {BG}; }}
    QLabel   {{ color: {TEXT}; font-family: 'Segoe UI'; font-size: 13px; }}
    QLineEdit {{
        background: {INPUT_BG};
        color: {TEXT};
        border: 1px solid #243447;
        border-radius: 10px;
        padding: 10px 14px;
        font-size: 13px;
        font-family: 'Segoe UI';
    }}
    QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
    QPushButton {{
        background: {ACCENT};
        color: white;
        border: none;
        border-radius: 10px;
        padding: 11px;
        font-size: 13px;
        font-family: 'Segoe UI';
        font-weight: bold;
    }}
    QPushButton:hover   {{ background: #6599D2; }}
    QPushButton:pressed {{ background: #3D6A9E; }}
"""


# ── Auth dialog ───────────────────────────────────────────────────────────────

class AuthDialog(QDialog):
    logged_in = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("P2P Messenger")
        self.setFixedSize(440, 430)
        self.setStyleSheet(_DLG_QSS)
        self._mode    = 'login'
        self._workers = []
        self._nick_timer = QTimer(singleShot=True, interval=600)
        self._nick_timer.timeout.connect(self._check_nick)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(40, 32, 40, 32)
        root.setSpacing(0)

        logo = QLabel("💬")
        logo.setFont(QFont("Segoe UI", 36))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("P2P Messenger")
        title.setFont(QFont("Segoe UI", 17, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub = QLabel("Зашифрованный · P2P · Без регистрации данных")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet(f"color: {TEXT2}; font-size: 11px;")
        root.addWidget(logo)
        root.addSpacing(4)
        root.addWidget(title)
        root.addWidget(sub)
        root.addSpacing(20)

        tabs = QHBoxLayout()
        tabs.setSpacing(0)
        self._btn_login = self._tab_btn("Войти",              lambda: self._set_mode('login'))
        self._btn_reg   = self._tab_btn("Зарегистрироваться", lambda: self._set_mode('register'))
        tabs.addWidget(self._btn_login)
        tabs.addWidget(self._btn_reg)
        root.addLayout(tabs)
        root.addSpacing(14)

        nick_row = QHBoxLayout()
        nick_row.setSpacing(8)
        self._nick = QLineEdit(placeholderText="Никнейм")
        self._nick.textChanged.connect(self._on_nick_changed)
        self._nick_lbl = QLabel("")
        self._nick_lbl.setFixedWidth(90)
        self._nick_lbl.setFont(QFont("Segoe UI", 9))
        self._nick_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        nick_row.addWidget(self._nick)
        nick_row.addWidget(self._nick_lbl)
        root.addLayout(nick_row)
        root.addSpacing(10)

        self._pw = QLineEdit(placeholderText="Пароль", echoMode=QLineEdit.EchoMode.Password)
        root.addWidget(self._pw)
        root.addSpacing(10)

        self._pw2 = QLineEdit(placeholderText="Повторите пароль", echoMode=QLineEdit.EchoMode.Password)
        root.addWidget(self._pw2)
        root.addSpacing(14)

        self._submit = QPushButton("Войти")
        self._submit.clicked.connect(self._on_submit)
        self._pw.returnPressed.connect(self._on_submit)
        self._pw2.returnPressed.connect(self._on_submit)
        root.addWidget(self._submit)
        root.addSpacing(8)

        self._err = QLabel("")
        self._err.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._err.setStyleSheet(f"color: {RED}; font-size: 11px;")
        self._err.setWordWrap(True)
        root.addWidget(self._err)
        root.addStretch()
        self._set_mode('login')

    def _tab_btn(self, text, fn):
        b = QPushButton(text)
        b.setCheckable(True)
        b.clicked.connect(fn)
        b.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT2};
                border: none; border-bottom: 2px solid transparent;
                border-radius: 0; padding: 8px 16px;
                font-family: 'Segoe UI'; font-size: 13px;
            }}
            QPushButton:checked {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; }}
            QPushButton:hover   {{ color: {TEXT}; }}
        """)
        return b

    def _set_mode(self, mode):
        self._mode = mode
        reg = (mode == 'register')
        self._btn_login.setChecked(not reg)
        self._btn_reg.setChecked(reg)
        self._pw2.setVisible(reg)
        self._nick_lbl.setVisible(reg)
        self._submit.setText("Зарегистрироваться" if reg else "Войти")
        self._err.setText("")
        if reg:
            self._on_nick_changed(self._nick.text())

    def _on_nick_changed(self, _):
        if self._mode != 'register':
            return
        self._nick_lbl.setText("…")
        self._nick_lbl.setStyleSheet(f"color: {TEXT2};")
        self._nick_timer.start()

    def _check_nick(self):
        nick = self._nick.text().strip()
        if len(nick) < 3:
            self._nick_lbl.setText("")
            return
        w = HttpWorker('GET', '/check_nick', params={'nick': nick})
        w.done.connect(lambda d: (
            self._nick_lbl.setText("✓ свободен"),
            self._nick_lbl.setStyleSheet(f"color: {GREEN};")
        ) if d.get('available') else (
            self._nick_lbl.setText("✗ занят"),
            self._nick_lbl.setStyleSheet(f"color: {RED};")
        ))
        w.failed.connect(lambda _: self._nick_lbl.setText(""))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_submit(self):
        nick, pw = self._nick.text().strip(), self._pw.text()
        self._err.setText("")
        if not nick or not pw:
            self._err.setText("Заполните все поля")
            return
        if self._mode == 'register':
            if len(nick) < 3:
                self._err.setText("Никнейм слишком короткий")
                return
            if len(pw) < 4:
                self._err.setText("Пароль слишком короткий")
                return
            if pw != self._pw2.text():
                self._err.setText("Пароли не совпадают")
                return
            ep, txt = '/register', "Регистрация…"
        else:
            ep, txt = '/login', "Вход…"
        self._submit.setEnabled(False)
        self._submit.setText(txt)
        w = HttpWorker('POST', ep, {'nickname': nick, 'password': pw})
        w.done.connect(self._on_auth_ok)
        w.failed.connect(self._on_auth_err)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_auth_ok(self, data):
        self._submit.setEnabled(True)
        self._submit.setText("Зарегистрироваться" if self._mode == 'register' else "Войти")
        self.logged_in.emit(data['nickname'], data['token'])
        self.accept()

    def _on_auth_err(self, msg):
        self._submit.setEnabled(True)
        self._submit.setText("Зарегистрироваться" if self._mode == 'register' else "Войти")
        self._err.setText(msg)


# ── Server settings dialog (all users) ────────────────────────────────────────

class ServerSettingsDialog(QDialog):
    def __init__(self, is_admin=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки сервера")
        self.setFixedSize(420, 380)
        self.setStyleSheet(_DLG_QSS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(16)

        lbl = QLabel("⚙  Настройки сервера")
        lbl.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {ORANGE if is_admin else TEXT};")
        lay.addWidget(lbl)

        lay.addWidget(QLabel("Хост:"))
        self._host = QLineEdit(_host)
        lay.addWidget(self._host)

        row = QHBoxLayout()
        row.setSpacing(12)

        left  = QVBoxLayout()
        right = QVBoxLayout()
        lbl_u = QLabel("UDP порт:")
        lbl_u.setStyleSheet(f"color: {TEXT2}; font-size: 11px;")
        self._udp = QLineEdit(str(_udp_port))
        lbl_h = QLabel("HTTP порт:")
        lbl_h.setStyleSheet(f"color: {TEXT2}; font-size: 11px;")
        self._htp = QLineEdit(str(_http_port))
        left.addWidget(lbl_u)
        left.addWidget(self._udp)
        right.addWidget(lbl_h)
        right.addWidget(self._htp)
        row.addLayout(left)
        row.addLayout(right)
        lay.addLayout(row)
        lay.addSpacing(6)

        btn = QPushButton("Сохранить")
        btn.clicked.connect(self._save)
        lay.addWidget(btn)

        self._err = QLabel("")
        self._err.setStyleSheet(f"color: {RED}; font-size: 11px;")
        lay.addWidget(self._err)

    def _save(self):
        try:
            udp = int(self._udp.text())
            htp = int(self._htp.text())
            assert 1 <= udp <= 65535 and 1 <= htp <= 65535
        except Exception:
            self._err.setText("Порты должны быть числами 1–65535")
            return
        self.accept()

    def values(self):
        return self._host.text().strip(), int(self._udp.text()), int(self._htp.text())


# ── Network thread ─────────────────────────────────────────────────────────────

class NetworkThread(QThread):
    message_received = pyqtSignal(str, str)   # peer_id, text
    peer_connected   = pyqtSignal(str)         # peer_id
    status_changed   = pyqtSignal(str)

    def __init__(self, my_id, token, rendezvous):
        super().__init__()
        self.my_id      = my_id
        self.token      = token
        self.rendezvous = rendezvous
        self.proto      = None
        self._loop      = None

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except (RuntimeError, asyncio.CancelledError):
            pass
        finally:
            self._loop.close()

    async def _async_main(self):
        _, self.proto = await self._loop.create_datagram_endpoint(
            lambda: PeerProtocol(
                self.my_id, self.token,
                on_message   = lambda t: self.message_received.emit(
                    self.proto._peer_id or '', t
                ),
                on_connected = lambda: self.peer_connected.emit(
                    self.proto._peer_id or ''
                ),
                on_status    = lambda s: self.status_changed.emit(s),
            ),
            local_addr=('0.0.0.0', 0),
        )
        self.proto.register(self.rendezvous)
        self.status_changed.emit('online')
        self._stop = asyncio.Event()
        await self._stop.wait()

    def connect_to_peer(self, target_id):
        if self.proto and self._loop:
            self._loop.call_soon_threadsafe(
                self.proto.request_connect, target_id, self.rendezvous
            )

    def send(self, text):
        if self.proto and self._loop:
            self._loop.call_soon_threadsafe(self.proto.send_message, text)

    def stop(self):
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)


# ── Message bubble ─────────────────────────────────────────────────────────────

class MessageBubble(QWidget):
    def __init__(self, text, is_mine, ts=None, parent=None):
        super().__init__(parent)
        ts = ts or datetime.now().strftime("%H:%M")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 2, 16, 2)

        bubble = QFrame()
        bubble.setMaximumWidth(520)
        bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        inner = QVBoxLayout(bubble)
        inner.setContentsMargins(12, 8, 12, 6)
        inner.setSpacing(4)

        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setFont(QFont("Segoe UI", 10))
        msg.setStyleSheet(f"color: {TEXT}; background: transparent;")
        msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        foot = QHBoxLayout()
        foot.setSpacing(4)
        foot.addStretch()
        if is_mine:
            lk = QLabel("🔒")
            lk.setFont(QFont("Segoe UI", 7))
            lk.setStyleSheet("background: transparent;")
            foot.addWidget(lk)
        ts_lbl = QLabel(ts)
        ts_lbl.setFont(QFont("Segoe UI", 7))
        ts_lbl.setStyleSheet(
            f"color: {'#8EB3D4' if is_mine else TEXT2}; background: transparent;"
        )
        foot.addWidget(ts_lbl)

        inner.addWidget(msg)
        inner.addLayout(foot)

        bl = "3px" if not is_mine else "12px"
        br = "3px" if is_mine     else "12px"
        bubble.setStyleSheet(f"""
            QFrame {{
                background: {'#2B5278' if is_mine else '#182533'};
                border-radius: 12px;
                border-bottom-left-radius:  {bl};
                border-bottom-right-radius: {br};
            }}
        """)

        if is_mine:
            outer.addStretch()
            outer.addWidget(bubble)
        else:
            outer.addWidget(bubble)
            outer.addStretch()

        self.setStyleSheet("background: transparent;")


# ── Chat area ──────────────────────────────────────────────────────────────────

class ChatArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(f"""
            QScrollArea {{ background: {BG}; border: none; }}
            QScrollBar:vertical {{
                background: {BG}; width: 6px; margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: #2C3E4F; border-radius: 3px; min-height: 24px;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{ height: 0; }}
        """)
        self._w = QWidget()
        self._w.setStyleSheet(f"background: {BG};")
        self._v = QVBoxLayout(self._w)
        self._v.setContentsMargins(0, 12, 0, 12)
        self._v.setSpacing(0)
        self._v.addStretch()
        self.setWidget(self._w)

    def add_message(self, text, is_mine, ts=None):
        self._v.addWidget(MessageBubble(text, is_mine, ts))
        QTimer.singleShot(30, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()
        ))

    def load_history(self, rows):
        for text, is_mine, ts in rows:
            self._v.addWidget(MessageBubble(text, is_mine, ts))
        QTimer.singleShot(50, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()
        ))


# ── Contact item ───────────────────────────────────────────────────────────────

class ContactItem(QWidget):
    clicked = pyqtSignal(str)

    _ST = {
        'online':     (GREEN,  '● в сети'),
        'relay':      (ORANGE, '● в сети (relay)'),
        'connecting': (ORANGE, '● подключение…'),
        'offline':    (TEXT2,  None),
    }

    def __init__(self, peer_id, last_msg='', last_ts='', parent=None):
        super().__init__(parent)
        self.peer_id  = peer_id
        self._active  = False
        self._preview = last_msg
        self._status  = 'offline'
        self.setFixedHeight(64)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 14, 0)
        lay.setSpacing(10)

        self._av = Avatar(peer_id[0], 42)

        name_lbl = QLabel(peer_id)
        name_lbl.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        name_lbl.setStyleSheet(f"color: {TEXT};")

        self._ts_lbl = QLabel(last_ts)
        self._ts_lbl.setFont(QFont("Segoe UI", 8))
        self._ts_lbl.setStyleSheet(f"color: {TEXT2};")
        self._ts_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name_row.addWidget(name_lbl, 1)
        name_row.addWidget(self._ts_lbl)

        self._sub_lbl = QLabel(self._clip(last_msg))
        self._sub_lbl.setFont(QFont("Segoe UI", 9))
        self._sub_lbl.setStyleSheet(f"color: {TEXT2};")

        info = QVBoxLayout()
        info.setSpacing(2)
        info.setContentsMargins(0, 0, 0, 0)
        info.addLayout(name_row)
        info.addWidget(self._sub_lbl)

        lay.addWidget(self._av)
        lay.addLayout(info, 1)
        self._refresh()

    @staticmethod
    def _clip(t, n=36):
        return (t[:n] + '…') if len(t) > n else t

    def _refresh(self):
        bg = ACTIVE if self._active else "transparent"
        self.setStyleSheet(f"ContactItem {{ background: {bg}; border-radius: 10px; }}")

    def set_active(self, v):
        self._active = v
        self._refresh()

    def set_status(self, key):
        self._status = key
        color, label = self._ST.get(key, (TEXT2, None))
        if label:
            self._sub_lbl.setText(label)
            self._sub_lbl.setStyleSheet(f"color: {color};")
        else:
            self._sub_lbl.setText(self._clip(self._preview))
            self._sub_lbl.setStyleSheet(f"color: {TEXT2};")

    def set_preview(self, text, ts=''):
        self._preview = text
        if ts:
            self._ts_lbl.setText(ts)
        if self._status == 'offline':
            self._sub_lbl.setText(self._clip(text))
            self._sub_lbl.setStyleSheet(f"color: {TEXT2};")

    def mousePressEvent(self, _):
        self.clicked.emit(self.peer_id)

    def enterEvent(self, _):
        if not self._active:
            self.setStyleSheet(f"ContactItem {{ background: {HOVER}; border-radius: 10px; }}")

    def leaveEvent(self, _):
        self._refresh()


# ── Connect dialog ─────────────────────────────────────────────────────────────

class ConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Новый чат")
        self.setFixedSize(360, 190)
        self.setStyleSheet(_DLG_QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(14)
        lbl = QLabel("Подключиться к пиру")
        lbl.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        lay.addWidget(lbl)
        self._f = QLineEdit(placeholderText="Никнейм (например, bob)")
        self._f.returnPressed.connect(self.accept)
        lay.addWidget(self._f)
        btn = QPushButton("Подключиться")
        btn.clicked.connect(self.accept)
        lay.addWidget(btn)

    def peer_id(self):
        return self._f.text().strip()


# ── Main window ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, my_id, token):
        super().__init__()
        self.my_id       = my_id
        self.token       = token
        self.current     = None
        self._contacts   = {}    # pid -> ContactItem
        self._chat_areas = {}    # pid -> ChatArea
        self._db         = LocalDB(my_id)
        self.net         = NetworkThread(my_id, token, _rendezvous())
        self.net.message_received.connect(self._on_message)
        self.net.peer_connected.connect(self._on_connected)
        self.net.status_changed.connect(self._on_net_status)
        self.setWindowTitle("P2P Messenger")
        self.setMinimumSize(900, 640)
        self.resize(1060, 720)
        self._build()
        for pid, lmsg, lts in self._db.get_contacts():
            self._ensure_contact(pid, lmsg, lts)
        self.net.start()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self):
        self.setStyleSheet(f"QMainWindow {{ background: {BG}; }}")
        root = QWidget()
        self.setCentralWidget(root)
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self._make_sidebar())
        rl.addWidget(self._make_chat_panel(), 1)

    def _make_sidebar(self):
        w = QWidget()
        w.setFixedWidth(290)
        w.setStyleSheet(f"background: {SIDEBAR};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        hdr = QWidget()
        hdr.setFixedHeight(58)
        hdr.setStyleSheet(f"background: {SIDEBAR}; border-bottom: 1px solid {DIVIDER};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(14, 0, 12, 0)

        av = Avatar(self.my_id[0], 34, ACCENT)
        me = QLabel(self.my_id)
        me.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        me.setStyleSheet(f"color: {TEXT};")

        self._dot = QLabel("●")
        self._dot.setFont(QFont("Segoe UI", 9))
        self._dot.setStyleSheet(f"color: {TEXT2};")

        def _icon_btn(txt, tip, fn, color=TEXT2):
            b = QPushButton(txt)
            b.setFixedSize(32, 32)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {color};
                    border: none; border-radius: 16px; font-size: 16px;
                }}
                QPushButton:hover {{ background: {HOVER}; }}
            """)
            b.clicked.connect(fn)
            return b

        gear = _icon_btn("⚙", "Настройки сервера", self._open_server_settings, ORANGE)

        add_btn = QPushButton("+")
        add_btn.setFixedSize(34, 34)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setToolTip("Новый чат")
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT}; color: white; border: none;
                border-radius: 17px; font-size: 20px; font-weight: bold;
            }}
            QPushButton:hover   {{ background: #6599D2; }}
            QPushButton:pressed {{ background: #3D6A9E; }}
        """)
        add_btn.clicked.connect(self._open_connect)

        hl.addWidget(av)
        hl.addSpacing(8)
        hl.addWidget(me)
        hl.addWidget(self._dot)
        hl.addStretch()
        hl.addWidget(gear)
        hl.addSpacing(4)
        hl.addWidget(add_btn)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: {SIDEBAR}; border: none; }}
            QScrollBar:vertical {{ background: {SIDEBAR}; width: 4px; }}
            QScrollBar::handle:vertical {{ background: #243447; border-radius: 2px; }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{ height: 0; }}
        """)
        self._cw = QWidget()
        self._cw.setStyleSheet(f"background: {SIDEBAR};")
        self._cl = QVBoxLayout(self._cw)
        self._cl.setContentsMargins(8, 8, 8, 8)
        self._cl.setSpacing(2)
        self._cl.addStretch()
        scroll.setWidget(self._cw)

        lay.addWidget(hdr)
        lay.addWidget(scroll)
        return w

    def _make_chat_panel(self):
        w = QWidget()
        w.setStyleSheet(f"background: {BG};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # header
        chdr = QWidget()
        chdr.setFixedHeight(58)
        chdr.setStyleSheet(f"background: {BG}; border-bottom: 1px solid {DIVIDER};")
        chl = QHBoxLayout(chdr)
        chl.setContentsMargins(20, 0, 20, 0)

        self._title = QLabel("Выберите контакт")
        self._title.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self._title.setStyleSheet(f"color: {TEXT};")
        self._sub = QLabel("")
        self._sub.setFont(QFont("Segoe UI", 9))
        self._sub.setStyleSheet(f"color: {TEXT2};")

        ti = QVBoxLayout()
        ti.setSpacing(1)
        ti.addWidget(self._title)
        ti.addWidget(self._sub)

        self._lock = QLabel("  🔒 e2e encrypted · direct")
        self._lock.setFont(QFont("Segoe UI", 9))
        self._lock.setStyleSheet(f"color: {GREEN};")
        self._lock.setVisible(False)

        chl.addLayout(ti)
        chl.addStretch()
        chl.addWidget(self._lock)

        # stacked: empty placeholder + per-contact chat areas
        empty = QWidget()
        empty.setStyleSheet(f"background: {BG};")
        el = QVBoxLayout(empty)
        ico  = QLabel("💬")
        ico.setFont(QFont("Segoe UI", 52))
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("Нажмите  +  чтобы начать зашифрованный чат")
        hint.setFont(QFont("Segoe UI", 12))
        hint.setStyleSheet(f"color: {TEXT2};")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addStretch()
        el.addWidget(ico)
        el.addWidget(hint)
        el.addStretch()

        self._stack = QStackedWidget()
        self._stack.addWidget(empty)   # index 0

        # input bar
        bar = QWidget()
        bar.setFixedHeight(72)
        bar.setStyleSheet(f"background: {BG}; border-top: 1px solid {DIVIDER};")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 14, 16, 14)
        bl.setSpacing(10)

        self._input = QLineEdit(placeholderText="Написать сообщение…")
        self._input.setFont(QFont("Segoe UI", 11))
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {INPUT_BG}; color: {TEXT};
                border: none; border-radius: 22px; padding: 10px 20px;
            }}
            QLineEdit::placeholder {{ color: {TEXT2}; }}
        """)
        self._input.returnPressed.connect(self._send)

        send_btn = QPushButton("➤")
        send_btn.setFixedSize(44, 44)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setFont(QFont("Segoe UI", 14))
        send_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT}; color: white; border: none; border-radius: 22px;
            }}
            QPushButton:hover   {{ background: #6599D2; }}
            QPushButton:pressed {{ background: #3D6A9E; }}
        """)
        send_btn.clicked.connect(self._send)

        self._retry_btn = QPushButton("🔄  Повторить подключение")
        self._retry_btn.setVisible(False)
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.setStyleSheet(f"""
            QPushButton {{
                background: #1C2D3E; color: {ORANGE};
                border: 1px solid {ORANGE}; border-radius: 22px;
                padding: 10px 20px; font-family: 'Segoe UI'; font-size: 12px;
            }}
            QPushButton:hover {{ background: #243447; }}
        """)
        self._retry_btn.clicked.connect(self._retry_connect)

        bl.addWidget(self._input)
        bl.addWidget(self._retry_btn)
        bl.addWidget(send_btn)

        lay.addWidget(chdr)
        lay.addWidget(self._stack, 1)
        lay.addWidget(bar)
        return w

    # ── helpers ───────────────────────────────────────────────────────────────

    def _ensure_contact(self, pid, last_msg='', last_ts=''):
        if pid in self._contacts:
            return
        item = ContactItem(pid, last_msg, last_ts)
        item.clicked.connect(self._select)
        self._cl.insertWidget(self._cl.count() - 1, item)
        self._contacts[pid] = item

        chat = ChatArea()
        self._stack.addWidget(chat)
        self._chat_areas[pid] = chat
        chat.load_history(self._db.load(pid))

    def _current_chat(self):
        return self._chat_areas.get(self.current)

    def _set_sub(self, text, color=None):
        self._sub.setText(text)
        self._sub.setStyleSheet(f"color: {color or TEXT2};")

    # ── actions ───────────────────────────────────────────────────────────────

    def _open_connect(self):
        dlg = ConnectDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pid = dlg.peer_id()
        if not pid:
            return
        self._ensure_contact(pid)
        self._select(pid)
        self._do_connect(pid)

    def _do_connect(self, pid):
        self._contacts[pid].set_status('connecting')
        self._set_sub("пробиваем NAT…")
        self._retry_btn.setVisible(False)
        self.net.connect_to_peer(pid)

    def _retry_connect(self):
        if self.current:
            self._do_connect(self.current)

    def _open_server_settings(self):
        global _host, _udp_port, _http_port
        is_admin = self.my_id.lower() == 'koyfui'
        dlg = ServerSettingsDialog(is_admin, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        _host, _udp_port, _http_port = dlg.values()
        s = QSettings(_INI, QSettings.Format.IniFormat)
        _save_server_cfg(s)

    def _select(self, pid):
        if self.current and self.current in self._contacts:
            self._contacts[self.current].set_active(False)
        self.current = pid
        self._contacts[pid].set_active(True)
        self._title.setText(pid)
        if pid in self._chat_areas:
            self._stack.setCurrentWidget(self._chat_areas[pid])
        else:
            self._stack.setCurrentIndex(0)

        # Restore header state for this peer
        proto = self.net.proto
        if proto and proto.cipher and proto._peer_id == pid:
            relay = proto._relay
            self._lock.setText(
                "  🔒 e2e encrypted · relay" if relay else "  🔒 e2e encrypted · direct"
            )
            self._lock.setStyleSheet(f"color: {ORANGE if relay else GREEN};")
            self._lock.setVisible(True)
            self._set_sub("")
            self._retry_btn.setVisible(False)
        else:
            self._lock.setVisible(False)
            # Auto-connect when switching to a contact
            self._do_connect(pid)

    def _send(self):
        text = self._input.text().strip()
        if not text or not self.current:
            return
        proto = self.net.proto
        if not (proto and proto.cipher):
            self._set_sub("соединение не установлено", RED)
            return
        self.net.send(text)
        ts = datetime.now().strftime("%H:%M")
        chat = self._current_chat()
        if chat:
            chat.add_message(text, is_mine=True)
        self._db.save(self.current, text, is_mine=True)
        if self.current in self._contacts:
            self._contacts[self.current].set_preview(f"Вы: {text}", ts)
        self._input.clear()

    # ── network slots ─────────────────────────────────────────────────────────

    def _on_message(self, peer_id, text):
        ts = datetime.now().strftime("%H:%M")
        pid = peer_id or self.current or ''
        if pid:
            self._ensure_contact(pid)
            self._chat_areas[pid].add_message(text, is_mine=False)
            self._db.save(pid, text, is_mine=False)
            self._contacts[pid].set_preview(text, ts)

    def _on_connected(self, peer_id):
        proto = self.net.proto
        relay = bool(proto and proto._relay)
        self._ensure_contact(peer_id)
        c = self._contacts[peer_id]
        c.set_status('relay' if relay else 'online')

        if self.current == peer_id:
            self._lock.setText(
                "  🔒 e2e encrypted · relay" if relay else "  🔒 e2e encrypted · direct"
            )
            self._lock.setStyleSheet(f"color: {ORANGE if relay else GREEN};")
            self._lock.setVisible(True)
            self._set_sub("")
            self._retry_btn.setVisible(False)

    def _on_net_status(self, s):
        proto = self.net.proto
        already_connected = bool(proto and proto.cipher)

        if s == 'online':
            self._dot.setStyleSheet(f"color: {GREEN};")
            return

        if already_connected:
            return

        if s == 'connecting':
            if self.current and self.current in self._contacts:
                self._contacts[self.current].set_status('connecting')
            self._set_sub("пробиваем NAT…")
            self._retry_btn.setVisible(False)

        elif s == 'relay':
            self._set_sub("прямое не удалось, пробуем relay…", ORANGE)

        elif s == 'timeout':
            if self.current and self.current in self._contacts:
                self._contacts[self.current].set_status('offline')
            self._set_sub("не удалось подключиться", RED)
            self._retry_btn.setVisible(True)

    def closeEvent(self, e):
        self.net.stop()
        self.net.wait(1000)
        super().closeEvent(e)


# ── entry point ────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("P2P Messenger")

    s = QSettings(_INI, QSettings.Format.IniFormat)
    _load_server_cfg(s)

    token    = s.value("auth/token",    "")
    nickname = s.value("auth/nickname", "")

    if token and nickname:
        result, err = _http('POST', '/verify_token', {'token': token})
        if err or not result:
            token = nickname = ""

    if not token:
        dlg = AuthDialog()
        ok  = False

        def _on_login(nick, tok):
            nonlocal nickname, token, ok
            nickname, token, ok = nick, tok, True

        dlg.logged_in.connect(_on_login)
        if dlg.exec() != QDialog.DialogCode.Accepted or not ok:
            sys.exit(0)
        s.setValue("auth/token",    token)
        s.setValue("auth/nickname", nickname)

    w = MainWindow(nickname, token)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

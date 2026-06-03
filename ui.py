import json
import os
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
    QSizePolicy, QSpinBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSettings, QSize
from PyQt6.QtGui import QFont, QColor, QPainter, QBrush, QPen

from peer import PeerProtocol

# ── palette ───────────────────────────────────────────────────────────────────
BG       = "#17212B"
SIDEBAR  = "#0E1621"
MSG_IN   = "#182533"
MSG_OUT  = "#2B5278"
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

ADMIN_NICK = "koyfui"

_INI = os.path.join(os.path.expanduser('~'), '.p2pmessenger', 'config.ini')
os.makedirs(os.path.dirname(_INI), exist_ok=True)
DEFAULT_HOST      = "193.188.20.124"
DEFAULT_UDP_PORT  = 5555
DEFAULT_HTTP_PORT = 5556

# ── server config (module-level, updated by koyfui) ──────────────────────────
_host      = DEFAULT_HOST
_udp_port  = DEFAULT_UDP_PORT
_http_port = DEFAULT_HTTP_PORT


def _http_base():
    return f"http://{_host}:{_http_port}"


def _rendezvous():
    return (_host, _udp_port)


def _load_server_cfg(s: QSettings):
    global _host, _udp_port, _http_port
    _host      = s.value("server/host",       DEFAULT_HOST)
    _udp_port  = int(s.value("server/udp_port",  DEFAULT_UDP_PORT))
    _http_port = int(s.value("server/http_port", DEFAULT_HTTP_PORT))


def _save_server_cfg(s: QSettings):
    s.setValue("server/host",       _host)
    s.setValue("server/udp_port",   _udp_port)
    s.setValue("server/http_port",  _http_port)


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
    done  = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, method, path, data=None, params=None):
        super().__init__()
        self._method = method
        self._path   = path
        self._data   = data
        self._params = params

    def run(self):
        result, err = _http(self._method, self._path, self._data, self._params)
        if err:
            self.failed.emit(err)
        else:
            self.done.emit(result)


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
        f = QFont("Segoe UI", self.width() // 3, QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.letter)


_BASE_DIALOG_QSS = f"""
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
    QLineEdit:focus  {{ border: 1px solid {ACCENT}; }}
    QSpinBox {{
        background: {INPUT_BG};
        color: {TEXT};
        border: 1px solid #243447;
        border-radius: 10px;
        padding: 8px 12px;
        font-size: 13px;
        font-family: 'Segoe UI';
    }}
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
    QPushButton[flat="true"] {{
        background: transparent;
        color: {ACCENT};
        font-weight: normal;
    }}
    QPushButton[flat="true"]:hover {{ color: #6599D2; }}
"""


# ── Auth dialog ───────────────────────────────────────────────────────────────

class AuthDialog(QDialog):
    logged_in = pyqtSignal(str, str)   # nickname, token

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("P2P Messenger")
        self.setFixedSize(440, 420)
        self.setStyleSheet(_BASE_DIALOG_QSS)
        self._mode    = 'login'
        self._workers = []
        self._nick_timer = QTimer(singleShot=True, interval=600)
        self._nick_timer.timeout.connect(self._check_nick)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(40, 36, 40, 36)
        root.setSpacing(0)

        # logo / title
        logo = QLabel("💬")
        logo.setFont(QFont("Segoe UI", 36))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("P2P Messenger")
        title.setFont(QFont("Segoe UI", 17, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub = QLabel("Зашифрованный · Без серверов · P2P")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet(f"color: {TEXT2}; font-size: 11px;")

        root.addWidget(logo)
        root.addSpacing(4)
        root.addWidget(title)
        root.addWidget(sub)
        root.addSpacing(22)

        # tab row
        tabs = QHBoxLayout()
        tabs.setSpacing(0)
        self._btn_login = self._tab_btn("Войти",          lambda: self._set_mode('login'))
        self._btn_reg   = self._tab_btn("Зарегистрироваться", lambda: self._set_mode('register'))
        tabs.addWidget(self._btn_login)
        tabs.addWidget(self._btn_reg)
        root.addLayout(tabs)
        root.addSpacing(16)

        # nick row
        nick_row = QHBoxLayout()
        nick_row.setSpacing(8)
        self._nick = QLineEdit(placeholderText="Никнейм")
        self._nick.textChanged.connect(self._on_nick_changed)
        self._nick_status = QLabel("")
        self._nick_status.setFixedWidth(90)
        self._nick_status.setFont(QFont("Segoe UI", 9))
        self._nick_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        nick_row.addWidget(self._nick)
        nick_row.addWidget(self._nick_status)
        root.addLayout(nick_row)
        root.addSpacing(10)

        self._pw = QLineEdit(placeholderText="Пароль", echoMode=QLineEdit.EchoMode.Password)
        root.addWidget(self._pw)
        root.addSpacing(10)

        self._pw2 = QLineEdit(placeholderText="Повторите пароль", echoMode=QLineEdit.EchoMode.Password)
        root.addWidget(self._pw2)
        root.addSpacing(16)

        self._submit = QPushButton("Войти")
        self._submit.clicked.connect(self._on_submit)
        self._pw.returnPressed.connect(self._on_submit)
        self._pw2.returnPressed.connect(self._on_submit)
        root.addWidget(self._submit)
        root.addSpacing(10)

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
            QPushButton:checked {{
                color: {ACCENT}; border-bottom: 2px solid {ACCENT};
            }}
            QPushButton:hover {{ color: {TEXT}; }}
        """)
        return b

    def _set_mode(self, mode):
        self._mode = mode
        is_reg = (mode == 'register')
        self._btn_login.setChecked(not is_reg)
        self._btn_reg.setChecked(is_reg)
        self._pw2.setVisible(is_reg)
        self._nick_status.setVisible(is_reg)
        self._submit.setText("Зарегистрироваться" if is_reg else "Войти")
        self._err.setText("")
        if is_reg:
            self._on_nick_changed(self._nick.text())

    def _on_nick_changed(self, text):
        if self._mode != 'register':
            return
        self._nick_status.setText("...")
        self._nick_status.setStyleSheet(f"color: {TEXT2};")
        self._nick_timer.start()

    def _check_nick(self):
        nick = self._nick.text().strip()
        if len(nick) < 3:
            self._nick_status.setText("")
            return
        w = HttpWorker('GET', '/check_nick', params={'nick': nick})
        w.done.connect(self._on_nick_result)
        w.failed.connect(lambda _: self._nick_status.setText(""))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_nick_result(self, data):
        if data.get('available'):
            self._nick_status.setText("✓ свободен")
            self._nick_status.setStyleSheet(f"color: {GREEN};")
        else:
            self._nick_status.setText("✗ занят")
            self._nick_status.setStyleSheet(f"color: {RED};")

    def _on_submit(self):
        nick = self._nick.text().strip()
        pw   = self._pw.text()
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
            self._submit.setEnabled(False)
            self._submit.setText("Регистрация...")
            w = HttpWorker('POST', '/register', {'nickname': nick, 'password': pw})
        else:
            self._submit.setEnabled(False)
            self._submit.setText("Вход...")
            w = HttpWorker('POST', '/login', {'nickname': nick, 'password': pw})

        w.done.connect(lambda d: self._on_auth_ok(d))
        w.failed.connect(lambda e: self._on_auth_err(e))
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


# ── Admin server settings (koyfui only) ───────────────────────────────────────

class ServerSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки сервера")
        self.setFixedSize(380, 260)
        self.setStyleSheet(_BASE_DIALOG_QSS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(12)

        lbl = QLabel("⚙ Настройки сервера  (только для koyfui)")
        lbl.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {ORANGE};")
        lay.addWidget(lbl)

        lay.addWidget(QLabel("Хост:"))
        self._host = QLineEdit(_host)
        lay.addWidget(self._host)

        row = QHBoxLayout()
        udp_lay = QVBoxLayout()
        udp_lay.addWidget(QLabel("UDP порт:"))
        self._udp = QSpinBox(minimum=1, maximum=65535, value=_udp_port)
        self._udp.setStyleSheet(f"color: {TEXT}; background: {INPUT_BG}; border-radius: 10px; padding: 8px;")
        udp_lay.addWidget(self._udp)

        http_lay = QVBoxLayout()
        http_lay.addWidget(QLabel("HTTP порт:"))
        self._http = QSpinBox(minimum=1, maximum=65535, value=_http_port)
        self._http.setStyleSheet(f"color: {TEXT}; background: {INPUT_BG}; border-radius: 10px; padding: 8px;")
        http_lay.addWidget(self._http)

        row.addLayout(udp_lay)
        row.addLayout(http_lay)
        lay.addLayout(row)
        lay.addSpacing(8)

        btn = QPushButton("Сохранить")
        btn.clicked.connect(self.accept)
        lay.addWidget(btn)

    def values(self):
        return self._host.text().strip(), self._udp.value(), self._http.value()


# ── Network thread ─────────────────────────────────────────────────────────────

class NetworkThread(QThread):
    message_received = pyqtSignal(str)
    peer_connected   = pyqtSignal()
    status_changed   = pyqtSignal(str)

    def __init__(self, my_id, token, rendezvous):
        super().__init__()
        self.my_id     = my_id
        self.token     = token
        self.rendezvous= rendezvous
        self.proto     = None
        self._loop     = None

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
                on_message  = lambda t: self.message_received.emit(t),
                on_connected= lambda:   self.peer_connected.emit(),
                on_status   = lambda s: self.status_changed.emit(s),
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
    def __init__(self, text, is_mine, parent=None):
        super().__init__(parent)
        ts    = datetime.now().strftime("%H:%M")
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
            lock = QLabel("🔒")
            lock.setFont(QFont("Segoe UI", 7))
            lock.setStyleSheet("background: transparent;")
            foot.addWidget(lock)
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
                border-bottom-left-radius: {bl};
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

    def add_message(self, text, is_mine):
        self._v.addWidget(MessageBubble(text, is_mine))
        QTimer.singleShot(30, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()
        ))


# ── Contact item ───────────────────────────────────────────────────────────────

class ContactItem(QWidget):
    clicked = pyqtSignal(str)

    def __init__(self, peer_id, parent=None):
        super().__init__(parent)
        self.peer_id = peer_id
        self._active = False
        self.setFixedHeight(64)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 16, 0)
        lay.setSpacing(12)

        self._av = Avatar(peer_id[0], 42)
        name = QLabel(peer_id)
        name.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        name.setStyleSheet(f"color: {TEXT};")

        self._status = QLabel("● offline")
        self._status.setFont(QFont("Segoe UI", 9))
        self._status.setStyleSheet(f"color: {TEXT2};")

        info = QVBoxLayout()
        info.setSpacing(2)
        info.addWidget(name)
        info.addWidget(self._status)

        lay.addWidget(self._av)
        lay.addLayout(info)
        lay.addStretch()
        self._refresh()

    def _refresh(self):
        bg = ACTIVE if self._active else "transparent"
        self.setStyleSheet(f"ContactItem {{ background: {bg}; border-radius: 10px; }}")

    def set_active(self, v):
        self._active = v
        self._refresh()

    def set_status(self, s):
        color = GREEN if s == 'online' else (ORANGE if s == 'connecting' else TEXT2)
        self._status.setText(f"● {s}")
        self._status.setStyleSheet(f"color: {color};")

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
        self.setFixedSize(360, 195)
        self.setStyleSheet(_BASE_DIALOG_QSS)

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
        self.my_id    = my_id
        self.token    = token
        self.current  = None
        self._contacts = {}
        self.net = NetworkThread(my_id, token, _rendezvous())
        self.net.message_received.connect(self._on_message)
        self.net.peer_connected.connect(self._on_connected)
        self.net.status_changed.connect(self._on_net_status)
        self.setWindowTitle("P2P Messenger")
        self.setMinimumSize(900, 640)
        self.resize(1060, 720)
        self._build()
        self.net.start()

    # ── build UI ──────────────────────────────────────────────────────────────

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
        hl  = QHBoxLayout(hdr)
        hl.setContentsMargins(14, 0, 12, 0)

        Avatar(self.my_id[0], 34, ACCENT, hdr)
        av = Avatar(self.my_id[0], 34, ACCENT)
        me = QLabel(self.my_id)
        me.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        me.setStyleSheet(f"color: {TEXT};")

        self._dot = QLabel("●")
        self._dot.setFont(QFont("Segoe UI", 9))
        self._dot.setStyleSheet(f"color: {TEXT2};")

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

        if self.my_id.lower() == ADMIN_NICK.lower():
            gear = QPushButton("⚙")
            gear.setFixedSize(32, 32)
            gear.setCursor(Qt.CursorShape.PointingHandCursor)
            gear.setToolTip("Настройки сервера")
            gear.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {ORANGE};
                    border: none; border-radius: 16px; font-size: 16px;
                }}
                QPushButton:hover {{ background: #1C2D3E; }}
            """)
            gear.clicked.connect(self._open_server_settings)
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

        chdr = QWidget()
        chdr.setFixedHeight(58)
        chdr.setStyleSheet(f"background: {BG}; border-bottom: 1px solid {DIVIDER};")
        chl  = QHBoxLayout(chdr)
        chl.setContentsMargins(20, 0, 20, 0)

        self._title = QLabel("Выберите контакт")
        self._title.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self._title.setStyleSheet(f"color: {TEXT};")
        self._sub   = QLabel("")
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

        self.chat = ChatArea()

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
        self._stack.addWidget(empty)
        self._stack.addWidget(self.chat)

        bar = QWidget()
        bar.setFixedHeight(72)
        bar.setStyleSheet(f"background: {BG}; border-top: 1px solid {DIVIDER};")
        bl  = QHBoxLayout(bar)
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

        send = QPushButton("➤")
        send.setFixedSize(44, 44)
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setFont(QFont("Segoe UI", 14))
        send.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT}; color: white; border: none; border-radius: 22px;
            }}
            QPushButton:hover   {{ background: #6599D2; }}
            QPushButton:pressed {{ background: #3D6A9E; }}
        """)
        send.clicked.connect(self._send)

        bl.addWidget(self._input)
        bl.addWidget(send)

        lay.addWidget(chdr)
        lay.addWidget(self._stack, 1)
        lay.addWidget(bar)
        return w

    # ── actions ───────────────────────────────────────────────────────────────

    def _open_connect(self):
        dlg = ConnectDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pid = dlg.peer_id()
        if not pid:
            return
        self._ensure_contact(pid)
        self.net.connect_to_peer(pid)
        self._select(pid)
        self._contacts[pid].set_status('connecting')
        self._sub.setText("пробиваем NAT…")

    def _open_server_settings(self):
        global _host, _udp_port, _http_port
        dlg = ServerSettingsDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        _host, _udp_port, _http_port = dlg.values()
        s = QSettings(_INI, QSettings.Format.IniFormat)
        _save_server_cfg(s)
        self._dot.setToolTip(f"UDP {_host}:{_udp_port}")

    def _ensure_contact(self, pid):
        if pid in self._contacts:
            return
        item = ContactItem(pid)
        item.clicked.connect(self._select)
        self._cl.insertWidget(self._cl.count() - 1, item)
        self._contacts[pid] = item

    def _select(self, pid):
        if self.current and self.current in self._contacts:
            self._contacts[self.current].set_active(False)
        self.current = pid
        self._contacts[pid].set_active(True)
        self._title.setText(pid)
        self._stack.setCurrentWidget(self.chat)
        encrypted = bool(self.net.proto and self.net.proto.cipher)
        self._lock.setVisible(encrypted)
        if not encrypted:
            self._sub.setText("")

    def _send(self):
        text = self._input.text().strip()
        if not text or not self.current:
            return
        if not (self.net.proto and self.net.proto.cipher):
            self._sub.setText("соединение ещё не установлено")
            self._sub.setStyleSheet(f"color: {RED};")
            return
        self.net.send(text)
        self.chat.add_message(text, is_mine=True)
        self._input.clear()

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_message(self, text):
        self.chat.add_message(text, is_mine=False)

    def _on_connected(self):
        relay = self.net.proto and self.net.proto._relay
        if relay:
            self._lock.setText("  🔒 e2e encrypted · relay")
            self._lock.setStyleSheet(f"color: {ORANGE};")
        else:
            self._lock.setText("  🔒 e2e encrypted · direct")
            self._lock.setStyleSheet(f"color: {GREEN};")
        self._lock.setVisible(True)
        self._sub.setText("")
        if self.current and self.current in self._contacts:
            self._contacts[self.current].set_status('online')

    def _on_net_status(self, s):
        if s == 'online':
            self._dot.setStyleSheet(f"color: {GREEN};")
        elif s == 'connecting':
            self._sub.setText("пробиваем NAT…")
            self._sub.setStyleSheet(f"color: {TEXT2};")
            if self.current and self.current in self._contacts:
                self._contacts[self.current].set_status('connecting')
        elif s == 'relay':
            self._sub.setText("прямое соединение не удалось, переключаемся на relay…")
            self._sub.setStyleSheet(f"color: {ORANGE};")
        elif s == 'timeout':
            self._sub.setText("не удалось подключиться")
            self._sub.setStyleSheet(f"color: {RED};")
            if self.current and self.current in self._contacts:
                self._contacts[self.current].set_status('offline')

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
            token    = ""
            nickname = ""

    if not token:
        dlg = AuthDialog()
        ok  = False

        def _on_login(nick, tok):
            nonlocal nickname, token, ok
            nickname = nick
            token    = tok
            ok       = True

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

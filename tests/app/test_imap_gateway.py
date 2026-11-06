"""IMAP 传输层验收：RFC 2971 ID 握手（163 企业邮箱专用邮箱场景）、
能力集探测、SELECT/EXAMINE 诊断文本（Unsafe Login vs 文件夹不存在）、
find_sent 路径。全部走本进程内的假 IMAP 服务器（socket 级），不接触
真实邮箱或 SMTP。
"""
from __future__ import annotations

import socket
import threading

import pytest

from cater.app.config import MailConfig
from cater.app.errors import MailAuthError, MailTransportError
from cater.app.mail.gateway import ImapGateway


class _Session:
    """脚本化假 IMAP 会话配置。"""

    def __init__(self, **kw):
        self.pre_caps = kw.get("pre_caps", "IMAP4REV1 LOGIN")
        self.login_status = kw.get("login_status", "OK")
        self.login_text = kw.get("login_text", "LOGIN completed")
        # 登录后未通告 CAPABILITY 时的应答：None=按 pre_caps；""=不回 untagged 行
        self.post_caps = kw.get("post_caps")
        self.capability_nil = kw.get("capability_nil", False)
        self.id_status = kw.get("id_status", "OK")   # OK / NO / BAD / DROP
        self.id_text = kw.get("id_text", "ID completed")
        # folder -> (status, text)；status != OK 时 text 为服务器诊断
        self.select_responses = kw.get("select_responses", {})
        self.default_select = kw.get(
            "default_select", ("OK", "[UIDVALIDITY 7] Select completed"))
        self.search_uids = kw.get("search_uids", (9,))


class FakeImapServer(threading.Thread):
    """最小 IMAP4 假服务器：记录命令顺序，按脚本应答。"""

    def __init__(self, session: _Session):
        super().__init__(daemon=True)
        self.session = session
        self.commands: list[tuple[str, str]] = []  # (COMMAND, 原始行)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._ready = threading.Event()

    def run(self) -> None:  # noqa: C901 - 脚本化协议分支
        self._ready.set()
        conn, _ = self._sock.accept()
        file = conn.makefile("rb")

        def send(line: str) -> None:
            conn.sendall(line.encode("ascii") + b"\r\n")

        send("* OK Fake IMAP ready")
        authed = False
        while True:
            raw = file.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            parts = line.split(None, 2)
            tag, cmd = parts[0], parts[1].upper()
            rest = parts[2] if len(parts) > 2 else ""
            self.commands.append((cmd, line))
            if cmd == "CAPABILITY":
                if authed and self.session.capability_nil:
                    # 登录后重查返回空能力集（NIL/无 untagged 行）
                    send(f"{tag} OK CAPABILITY completed")
                    continue
                caps = (self.session.post_caps if authed
                        and self.session.post_caps is not None
                        else self.session.pre_caps)
                if caps:
                    send(f"* CAPABILITY {caps}")
                send(f"{tag} OK CAPABILITY completed")
            elif cmd == "LOGIN":
                authed = True
                if self.session.post_caps:
                    send(f"* CAPABILITY {self.session.post_caps}")
                send(f"{tag} {self.session.login_status} {self.session.login_text}")
            elif cmd == "ID":
                if self.session.id_status == "DROP":
                    conn.close()
                    return
                send(f"{tag} {self.session.id_status} {self.session.id_text}")
            elif cmd in ("SELECT", "EXAMINE"):
                folder = rest.strip().strip('"')
                status, text = self.session.select_responses.get(
                    folder, self.session.default_select)
                send(f"{tag} {status} {text}")
            elif cmd == "UID":
                sub = rest.split(None, 1)[0].upper() if rest else ""
                if sub == "SEARCH":
                    uids = " ".join(str(u) for u in self.session.search_uids)
                    if uids:
                        send(f"* SEARCH {uids}")
                    send(f"{tag} OK SEARCH completed")
                else:
                    send(f"{tag} OK {sub or 'UID'} completed")
            elif cmd == "STATUS":
                send(f"* STATUS {rest} (UIDNEXT 10 MESSAGES 1)")
                send(f"{tag} OK STATUS completed")
            elif cmd == "LOGOUT":
                send(f"* BYE Logging out")
                send(f"{tag} OK LOGOUT completed")
                break
            else:
                send(f"{tag} OK {cmd} completed")
        try:
            conn.close()
        except OSError:
            pass

    def stop(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self.join(timeout=5)


ACCOUNT = "dedicated163@example.com"


class _Creds:
    def imap_password(self, account: str):
        return "secret-pw"

    def smtp_password(self, account: str):
        return "secret-pw"


def _gateway(server: FakeImapServer) -> ImapGateway:
    config = MailConfig(
        account=ACCOUNT, folder="INBOX", sent_folder="Sent",
        imap_host="127.0.0.1", imap_port=server.port,
        imap_ssl=False, imap_starttls=False,
    )
    return ImapGateway(config, _Creds(), timeout=5)


def _names(server: FakeImapServer) -> list[str]:
    return [name for name, _ in server.commands]


@pytest.fixture
def server():
    fixture = FakeImapServer(_Session())
    fixture.start()
    fixture._ready.wait(timeout=5)
    yield fixture
    fixture.stop()


# ---------------------------------------------------------------------------
# 163 式握手：LOGIN → CAPABILITY(重查) → ID → EXAMINE
# ---------------------------------------------------------------------------

def test_163_style_login_id_examine_order(server) -> None:
    server.session.post_caps = "IMAP4REV1 ID AUTH=PLAIN"
    result = _gateway(server).scan(
        ACCOUNT, "INBOX", last_uid=None, uid_validity=None, include_history=False
    )
    assert result.first_scan is True
    assert result.uid_validity == 7
    assert result.last_uid == 9
    names = _names(server)
    assert names.count("LOGIN") == 1            # 不重复登录
    assert names.index("LOGIN") < names.index("ID") < names.index("EXAMINE")
    id_lines = [raw for name, raw in server.commands if name == "ID"]
    assert len(id_lines) == 1
    assert '"name" "CATER"' in id_lines[0] and '"version"' in id_lines[0]
    # 真实静态元数据：绝不携带账号/密码
    assert ACCOUNT not in id_lines[0] and "secret-pw" not in id_lines[0]


def test_capability_without_id_path_unchanged(server) -> None:
    """Feishu 等不通告 ID 的服务器：行为保持 LOGIN → CAPABILITY → EXAMINE。"""
    server.session.post_caps = "IMAP4REV1 IDLE"
    result = _gateway(server).scan(
        ACCOUNT, "INBOX", last_uid=None, uid_validity=None, include_history=False
    )
    assert result.first_scan is True
    names = _names(server)
    assert "ID" not in names
    assert names.index("LOGIN") < names.index("EXAMINE")


def test_preauth_capability_advertising_id(server) -> None:
    """登录前通告即含 ID：直接握手，无需重查也可（此处仍有一次重查，允许）。"""
    server.session.pre_caps = "IMAP4REV1 ID"
    server.session.post_caps = "IMAP4REV1 ID"
    _gateway(server).scan(ACCOUNT, "INBOX", last_uid=None, uid_validity=None)
    names = _names(server)
    assert "ID" in names and names.index("ID") < names.index("EXAMINE")


# ---------------------------------------------------------------------------
# ID 失败必须显式失败，不得静默继续
# ---------------------------------------------------------------------------

def test_id_no_fails_clearly_and_stops(server) -> None:
    server.session.post_caps = "IMAP4REV1 ID"
    server.session.id_status = "NO"
    server.session.id_text = "Unsafe Login. ID required"
    with pytest.raises(MailTransportError, match="ID"):
        _gateway(server).scan(ACCOUNT, "INBOX", last_uid=None, uid_validity=None)
    names = _names(server)
    assert "EXAMINE" not in names and "SELECT" not in names  # 未静默继续选文件夹


def test_id_bad_fails_clearly(server) -> None:
    server.session.post_caps = "IMAP4REV1 ID"
    server.session.id_status = "BAD"
    server.session.id_text = "Unknown command"
    with pytest.raises(MailTransportError, match="ID"):
        _gateway(server).scan(ACCOUNT, "INBOX", last_uid=None, uid_validity=None)


def test_id_connection_drop_fails_clearly(server) -> None:
    server.session.post_caps = "IMAP4REV1 ID"
    server.session.id_status = "DROP"   # 服务器收到 ID 后直接断连
    with pytest.raises(MailTransportError, match="ID"):
        _gateway(server).scan(ACCOUNT, "INBOX", last_uid=None, uid_validity=None)


def test_id_nil_capabilities_proceeds_without_id(server) -> None:
    """CAPABILITY 应答为空（NIL/无 untagged 行）：视为未通告 ID，照常处理。"""
    server.session.capability_nil = True
    server.session.post_caps = "SHOULD-NOT-APPEAR"
    result = _gateway(server).scan(
        ACCOUNT, "INBOX", last_uid=None, uid_validity=None, include_history=False
    )
    assert result.first_scan is True
    assert "ID" not in _names(server)


# ---------------------------------------------------------------------------
# SELECT/EXAMINE 诊断：Unsafe Login 与 文件夹不存在 可区分
# ---------------------------------------------------------------------------

def test_select_no_unsafe_login_detail(server) -> None:
    server.session.post_caps = "IMAP4REV1 ID"
    server.session.select_responses = {
        "INBOX": ("NO", "Unsafe Login. Please contact kefu@163.com")
    }
    with pytest.raises(MailTransportError) as excinfo:
        _gateway(server).scan(ACCOUNT, "INBOX", last_uid=None, uid_validity=None)
    message = str(excinfo.value)
    assert "Unsafe Login" in message
    assert "INBOX" in message


def test_select_no_nonexistent_folder_detail(server) -> None:
    server.session.select_responses = {
        "INBOX": ("NO", "Mailbox doesn't exist: NOPE")
    }
    with pytest.raises(MailTransportError) as excinfo:
        _gateway(server).scan(ACCOUNT, "INBOX", last_uid=None, uid_validity=None)
    message = str(excinfo.value)
    assert "Mailbox doesn't exist" in message
    assert "Unsafe Login" not in message


def test_select_error_text_is_bounded_and_sanitized(server) -> None:
    long_text = "x" * 500 + "\r\nPWD=leak-attempt"
    server.session.select_responses = {"INBOX": ("NO", long_text)}
    with pytest.raises(MailTransportError) as excinfo:
        _gateway(server).scan(ACCOUNT, "INBOX", last_uid=None, uid_validity=None)
    message = str(excinfo.value)
    assert len(message) < 300
    assert "\r" not in message and "\n" not in message


# ---------------------------------------------------------------------------
# find_sent 路径同样握手
# ---------------------------------------------------------------------------

def test_find_sent_163_flow(server) -> None:
    server.session.post_caps = "IMAP4REV1 ID"
    server.session.search_uids = (42,)
    found = _gateway(server).find_sent(ACCOUNT, "Sent", "<sent-1@example.com>")
    assert found is True
    names = _names(server)
    assert names.count("LOGIN") == 1
    assert names.index("LOGIN") < names.index("ID") < names.index("EXAMINE")
    assert names.index("EXAMINE") < names.index("UID")


def test_find_sent_no_match_returns_false(server) -> None:
    server.session.search_uids = ()
    assert _gateway(server).find_sent(ACCOUNT, "Sent", "<sent-2@example.com>") is False


def test_find_sent_id_rejected_fails(server) -> None:
    server.session.post_caps = "IMAP4REV1 ID"
    server.session.id_status = "NO"
    server.session.id_text = "Unsafe Login. ID required"
    with pytest.raises(MailTransportError, match="ID"):
        _gateway(server).find_sent(ACCOUNT, "Sent", "<sent-3@example.com>")
    assert "EXAMINE" not in _names(server)


def test_find_sent_select_no_includes_detail(server) -> None:
    server.session.select_responses = {
        "Sent": ("NO", "Mailbox doesn't exist: Sent")
    }
    with pytest.raises(MailTransportError) as excinfo:
        _gateway(server).find_sent(ACCOUNT, "Sent", "<sent-4@example.com>")
    assert "Mailbox doesn't exist" in str(excinfo.value)


def test_auth_failure_remains_mail_auth_error(server) -> None:
    """登录失败仍是 MailAuthError（ID 握手不介入、不改变认证语义）。"""
    server.session.login_status = "NO"
    server.session.login_text = "Authentication failed"
    with pytest.raises(MailAuthError):
        _gateway(server).scan(ACCOUNT, "INBOX", last_uid=None, uid_validity=None)
    names = _names(server)
    assert "ID" not in names and "EXAMINE" not in names

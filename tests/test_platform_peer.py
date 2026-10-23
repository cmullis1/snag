"""snag.platform.peer: which Windows user owns the other end of a local TCP connection. Real sockets
and real processes of the user running the tests; another user's can't be made in a test, so that
answer is the guard's tests' (tests/test_server_guard.py), with the lookup faked."""
import socket
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the Windows TCP table and process tokens")

if sys.platform == "win32":
    from snag.platform import peer
    from snag.platform.peer import owning_pid, same_user_peer


@pytest.fixture
def listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    yield server
    server.close()


def test_a_connection_from_this_process_belongs_to_this_user(listener):
    with socket.create_connection(listener.getsockname()) as client:
        accepted, _ = listener.accept()
        with accepted:
            assert owning_pid(client.getsockname(), listener.getsockname()) == peer.os.getpid()
            assert same_user_peer(client.getsockname(), listener.getsockname()) is True


def test_a_connection_from_another_process_of_this_user_is_found_and_accepted(listener):
    host, port = listener.getsockname()
    child = subprocess.Popen([sys.executable, "-c",
                              f"import socket, time; s = socket.create_connection(({host!r}, {port})); time.sleep(30)"])
    try:
        accepted, address = listener.accept()
        with accepted:
            # A venv's python.exe runs the real interpreter as a child of its own: that one connects.
            assert owning_pid(address, listener.getsockname()) not in (peer.os.getpid(), 0)
            assert same_user_peer(address, listener.getsockname()) is True
    finally:
        child.kill()
        child.wait(10)


def test_a_port_with_no_connection_cannot_be_told(listener):
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    with pytest.raises(OSError, match="no TCP connection"):
        same_user_peer(("127.0.0.1", port), listener.getsockname())


def test_a_process_that_cannot_be_opened_cannot_be_told(monkeypatch, listener):
    """System processes (and other users') refuse to be opened: that fails closed."""
    with socket.create_connection(listener.getsockname()) as client:
        accepted, _ = listener.accept()
        with accepted:
            monkeypatch.setattr(peer, "owning_pid", lambda local, remote: 4)  # the System process
            with pytest.raises(OSError):
                same_user_peer(client.getsockname(), listener.getsockname())


def test_only_ipv4_loopback_is_looked_up(listener):
    with pytest.raises(OSError):
        same_user_peer(("::1", 5000), listener.getsockname())


def test_the_lookup_is_cheap(listener):
    with socket.create_connection(listener.getsockname()) as client:
        accepted, _ = listener.accept()
        with accepted:
            started = time.perf_counter()
            for _ in range(20):
                same_user_peer(client.getsockname(), listener.getsockname())
            assert (time.perf_counter() - started) / 20 < 0.05

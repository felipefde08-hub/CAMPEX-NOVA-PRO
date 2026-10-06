from __future__ import annotations

import socket

from campex_node import desktop_launcher


def test_a_second_node_on_the_same_data_folder_does_not_start(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_NODE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(desktop_launcher, "_bootstrap_log", lambda message: None)
    (tmp_path / desktop_launcher.PORT_FILE_NAME).write_text("8791", encoding="ascii")
    opened = []
    monkeypatch.setattr(desktop_launcher.webbrowser, "open", opened.append)
    # Another Node holds the lock.
    monkeypatch.setattr(desktop_launcher, "_acquire_instance_lock", lambda path, wait_seconds=5.0: None)

    assert desktop_launcher.main(["--port", "8787"]) == 0

    # The running Node's panel opens instead of a second Node on another port.
    assert opened == ["http://127.0.0.1:8791"]


def test_instance_lock_is_exclusive_until_released(tmp_path):
    path = tmp_path / desktop_launcher.LOCK_FILE_NAME
    first = desktop_launcher._acquire_instance_lock(path, wait_seconds=0)

    assert first is not None
    assert desktop_launcher._acquire_instance_lock(path, wait_seconds=0) is None
    first.close()
    second = desktop_launcher._acquire_instance_lock(path, wait_seconds=0)
    assert second is not None
    second.close()


def test_port_taken_by_another_program_moves_to_the_next_one():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]

        port = desktop_launcher._select_port("127.0.0.1", taken)

    assert port != taken
    assert taken < port < taken + desktop_launcher.PORT_RANGE_SIZE


def test_port_file_round_trip(tmp_path):
    path = tmp_path / desktop_launcher.PORT_FILE_NAME

    assert desktop_launcher._read_port(path) is None
    desktop_launcher._write_port(path, 8790)
    assert desktop_launcher._read_port(path) == 8790
    path.write_text("lixo", encoding="ascii")
    assert desktop_launcher._read_port(path) is None

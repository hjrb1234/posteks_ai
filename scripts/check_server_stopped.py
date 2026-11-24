"""Dependency-free guard used before changing an installed environment."""
from __future__ import annotations

import socket
import sys


def assert_server_stopped(host: str = '127.0.0.1', port: int = 8080) -> None:
    try:
        connection = socket.create_connection((host, port), timeout=0.4)
    except ConnectionRefusedError:
        return
    except OSError as exc:
        # A timeout or blocked local connection cannot prove the service stopped.
        raise ValueError('Не удалось проверить порт сервера. Проверьте локальное подключение и остановите POSTEKS AI перед обновлением.') from exc
    else:
        connection.close()
        raise ValueError(f'Порт {port} занят. Остановите POSTEKS AI (Ctrl+C) до установки/обновления; затем повторите install.bat.')


def main() -> int:
    try:
        assert_server_stopped()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

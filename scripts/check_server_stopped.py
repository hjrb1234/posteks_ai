"""Dependency-free guard used before changing an installed environment."""
from __future__ import annotations

import errno
import socket
import sys


def assert_server_stopped(host: str = '127.0.0.1', port: int = 8080) -> None:
    busy = f'Порт {port} занят. Остановите POSTEKS AI (Ctrl+C) до установки/обновления; затем повторите install.bat.'
    try:
        connection = socket.create_connection((host, port), timeout=2.0)
    except ConnectionRefusedError:
        return
    except OSError:
        # Windows can time out on a loopback connection even when no service
        # responds. Confirm the exact TCP endpoint can be bound exclusively.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if sys.platform == 'win32':
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((host, port))
        except OSError as exc:
            winerror = getattr(exc, 'winerror', None)
            if exc.errno in {errno.EADDRINUSE, 10048} or winerror == 10048:
                raise ValueError(busy) from exc
            code = winerror if winerror is not None else exc.errno
            raise ValueError(
                f'Не удалось проверить порт {port} (код ОС: {code}). '
                'Остановите POSTEKS AI перед обновлением и проверьте, доступен ли локальный порт.'
            ) from exc
    else:
        connection.close()
        raise ValueError(busy)


def main() -> int:
    try:
        assert_server_stopped()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

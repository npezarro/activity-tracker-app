"""One copy at a time: two copies would capture everything twice. The first copy
listens on a localhost port; a second copy asks it to show its window and exits."""
import socket
import threading

PORT = 47613


def claim(on_show):
    """True if this is the only copy (and starts listening); False if one is running."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", PORT))
    except OSError:
        srv.close()
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=2) as c:
                c.sendall(b"show\n")
        except OSError:
            pass
        return False
    srv.listen(4)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    if conn.recv(16).startswith(b"show"):
                        on_show()
            except OSError:
                return
    threading.Thread(target=serve, name="single-instance", daemon=True).start()
    return True

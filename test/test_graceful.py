
import unittest
import time
import socket
import framework
from thor.http import HttpServer

class TestGraceful(framework.ClientServerTestCase):
    def create_server(self, server_side):
        server = HttpServer(framework.test_host, 0, loop=self.loop)
        assert server.tcp_server.sock is not None
        test_port = server.tcp_server.sock.getsockname()[1]
        server_side(server)

        def stop():
             # Cleanup if needed (though loop stop handles it normally)
             if server.tcp_server.sock:
                server.tcp_server.sock.close()

        return (stop, test_port)

    def create_client(self, host, port, client_side):
        def run_client(client_side1):
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect((host, port))
            client_side1(client)
            client.close()

        self.move_to_thread(target=run_client, args=(client_side,))

    def test_http_graceful(self):
        stop_time = 0.0
        start_time = time.time()
        
        def server_side(server):
            def trigger():
                # Trigger shutdown while client is connected
                server.graceful_shutdown()
                
            self.loop.schedule(0.2, trigger)
            
            def on_stop():
                nonlocal stop_time
                stop_time = time.time()
                self.loop.stop()
                
            server.on('stop', on_stop)

        def client_side(client_conn):
            # Connect and stay connected
            client_conn.send(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            # Sleep longer than the schedule trigger (0.2)
            time.sleep(1.0)
            client_conn.close()

        self.go([server_side], [client_side])
        
        duration = stop_time - start_time
        # Shutdown triggered at 0.2. Client sleeps 1.0.
        # Stop should happen after ~1.0.
        # If it happened at ~0.2, it failed.
        self.assertTrue(duration > 0.8, f"Server stopped too early: {duration:.2f}s (expected > 0.8s)")

if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Multi-Forwarder v3.0 - Enhanced Edition
Improvements:
- Added proper logging with configurable levels
- Status monitoring thread (prints every 30s)
- Watchdog for stale connections (configurable timeout)
- Better error handling and resource cleanup
- Metrics tracking (bytes, uptime, viewer count)
- Configuration validation
- Class-based handlers (no more lambda closures)
"""
import configparser
import logging
import os
import socket
import sys
import threading
import time
import select
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from remoteConnection import RemoteConnection

# Configure logging - file only, nothing to console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('forwarder.log')
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class StreamMetrics:
    """Track metrics for a stream - thread-safe access"""
    bytes_received: int = 0
    bytes_sent: int = 0
    messages_received: int = 0
    connect_time: Optional[float] = None
    last_data_time: Optional[float] = None
    reconnect_count: int = 0
    _lock: threading.RLock = None
    
    def __post_init__(self):
        """Initialize lock after dataclass creation"""
        if self._lock is None:
            object.__setattr__(self, '_lock', threading.RLock())
    
    def uptime(self) -> float:
        """Return uptime in seconds, or 0 if not connected"""
        with self._lock:
            if self.connect_time:
                return time.time() - self.connect_time
            return 0.0
    
    def time_since_data(self) -> Optional[float]:
        """Return seconds since last data, or None if no data yet"""
        with self._lock:
            if self.last_data_time:
                return time.time() - self.last_data_time
            return None


class ForwardingServer:
    """Handles local viewer connections and broadcasts data"""
    
    def __init__(self, port: int, name: str):
        self.port = port
        self.name = name
        self.clients = set()
        self.lock = threading.Lock()
        self.running = True
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.metrics = StreamMetrics()

    def start(self):
        """Start the forwarding server"""
        try:
            self.server.bind(('0.0.0.0', self.port))
            self.server.listen(20)
            logger.info(f"[{self.name}] Forwarding server listening on 0.0.0.0:{self.port}")
            threading.Thread(target=self._accept, daemon=True, name=f"Accept-{self.name}").start()
            # Start viewer health check thread
            threading.Thread(target=self._check_viewer_health, daemon=True, name=f"HealthCheck-{self.name}").start()
        except OSError as e:
            logger.error(f"[{self.name}] Failed to bind port {self.port}: {e}")
            raise

    def _accept(self):
        """Accept new viewer connections"""
        self.server.settimeout(1.0)
        consecutive_errors = 0
        while self.running:
            try:
                client, addr = self.server.accept()
                consecutive_errors = 0  # Reset error counter on success
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                
                # Platform-specific keepalive settings
                if hasattr(socket, 'TCP_KEEPIDLE'):
                    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 20)
                if hasattr(socket, 'TCP_KEEPINTVL'):
                    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                if hasattr(socket, 'TCP_KEEPCNT'):
                    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)

                with self.lock:
                    self.clients.add(client)

                logger.info(f"[{self.name}] Viewer connected from {addr} (total: {len(self.clients)})")
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    consecutive_errors += 1
                    logger.error(f"[{self.name}] Accept error: {e} (attempt {consecutive_errors})")
                    # Don't break on error - continue trying to accept connections
                    if consecutive_errors > 10:
                        logger.critical(f"[{self.name}] Too many accept errors, giving up")
                        break
                    time.sleep(0.1)  # Brief backoff to avoid rapid error loops
                else:
                    break

    def broadcast(self, data: bytes):
        """Broadcast data to all connected viewers.

        The old implementation called ``sendall`` directly from the receive
        thread, which allowed one slow/blocked client to stall the entire
        loop.  Use ``select`` to limit the amount of time spent waiting for a
        socket to become writable and treat sockets that are not writable
        within the timeout as dead/slow.
        """
        if not data:
            return

        dead = []
        sent_count = 0

        with self.lock:
            clients = list(self.clients)

        # ask the kernel which sockets are writable (1s timeout)
        try:
            writable, _, _ = select.select(clients, [], [], 1.0)
        except Exception as e:
            # if select itself fails, just fall back to the original loop
            logger.debug(f"[{self.name}] select failed in broadcast: {e}")
            writable = clients

        slow = set(clients) - set(writable)
        if slow:
            logger.warning(f"[{self.name}] {len(slow)} slow viewer(s) detected, closing")
            dead.extend(slow)

        for c in writable:
            try:
                c.sendall(data)
                sent_count += 1
            except (BrokenPipeError, ConnectionResetError, OSError):
                dead.append(c)
                logger.info(f"[{self.name}] Viewer disconnected during send")

        if dead:
            with self.lock:
                for d in dead:
                    self.clients.discard(d)
                    try:
                        d.close()
                    except Exception:
                        pass

        # Update metrics - thread safe
        with self.metrics._lock:
            if sent_count > 0:
                self.metrics.bytes_sent += len(data) * sent_count
            # Always update bytes_received when we get data to broadcast
            self.metrics.bytes_received += len(data)
            self.metrics.messages_received += 1
            logger.debug(f"[{self.name}] RX: {len(data)} bytes (total: {self.metrics.bytes_received} bytes)")

    def viewer_count(self) -> int:
        """Return current number of connected viewers"""
        with self.lock:
            return len(self.clients)

    def _check_viewer_health(self):
        """Periodically check viewer connections for dead sockets"""
        import select
        while self.running:
            time.sleep(5)  # Check every 5 seconds
            if not self.running:
                break
            
            dead = []
            with self.lock:
                clients_copy = list(self.clients)
            
            if not clients_copy:
                continue
            
            # Use select to check for socket activity (closed connections or errors)
            try:
                # Set a small timeout for select
                readable, _, exceptional = select.select(clients_copy, [], clients_copy, 0.5)
                
                # Readable sockets might have data OR be closed - check which
                for c in readable:
                    try:
                        # Try to receive 1 byte - if connection is closed, get 0 bytes
                        # Since select said readable, this won't block
                        c.settimeout(0.1)  # Very short timeout just for this peek
                        try:
                            data = c.recv(1)
                            if not data:
                                # Empty recv = connection closed by peer
                                dead.append(c)
                                logger.info(f"[{self.name}] Detected closed viewer (FIN received)")
                        finally:
                            c.settimeout(2.0)  # Restore original timeout
                    except socket.timeout:
                        # This is OK - just means no data readily available
                        pass
                    except (OSError, socket.error):
                        dead.append(c)
                
                # Check exceptional sockets (socket errors)
                for c in exceptional:
                    if c not in dead:
                        dead.append(c)
                        logger.info(f"[{self.name}] Detected viewer socket error")
                
            except Exception as e:
                logger.debug(f"[{self.name}] Health check error: {e}")
            
            # Clean up dead connections
            if dead:
                with self.lock:
                    for d in dead:
                        if d in self.clients:
                            self.clients.discard(d)
                            try:
                                d.close()
                            except:
                                pass
                logger.info(f"[{self.name}] Cleaned up {len(dead)} dead viewer(s), {len(self.clients)} remaining")

    def stop(self):
        """Stop the forwarding server and disconnect all viewers"""
        logger.info(f"[{self.name}] Stopping forwarding server")
        self.running = False
        
        with self.lock:
            for c in list(self.clients):
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except:
                    pass
                try:
                    c.close()
                except:
                    pass
            self.clients.clear()
        
        try:
            self.server.close()
        except:
            pass


class StreamHandler:
    """Handles a single stream connection with proper closure-free callbacks"""
    
    def __init__(self, name: str, rc: RemoteConnection, fwd: ForwardingServer, 
                 config: dict, watchdog_timeout: float = 21600):
        self.name = name
        self.rc = rc
        self.fwd = fwd
        self.config = config
        self.watchdog_timeout = watchdog_timeout
        self.watchdog_thread = None
        self.running = True
        
        # Set up callbacks
        self.rc.on_connect = self._on_connect
        self.rc.on_disconnect = self._on_disconnect
        self.rc.on_message = self._on_message
        
    def _on_connect(self):
        """Called when connection is established"""
        with self.fwd.metrics._lock:
            now = time.time()
            # delta before we reset so logs show how long we were idle
            last_delta = None
            if self.fwd.metrics.last_data_time is not None:
                last_delta = now - self.fwd.metrics.last_data_time
            self.fwd.metrics.connect_time = now
            self.fwd.metrics.reconnect_count += 1
            # clear stale timestamp so watchdog doesn’t immediately fire
            self.fwd.metrics.last_data_time = now
        logger.info(f"[{self.name}] Connected to {self.config['remote_host']}:{self.config['remote_port']} (idle {last_delta}s)")
        
        # Start watchdog if not already running
        if self.watchdog_thread is None or not self.watchdog_thread.is_alive():
            self.watchdog_thread = threading.Thread(
                target=self._watchdog, 
                daemon=True, 
                name=f"Watchdog-{self.name}"
            )
            self.watchdog_thread.start()
    
    def _on_disconnect(self, exc: Exception):
        """Called when connection is lost"""
        logger.warning(f"[{self.name}] Disconnected: {exc}")
        with self.fwd.metrics._lock:
            self.fwd.metrics.connect_time = None
            # clear last-data to avoid watchdog confusion on next connect
            self.fwd.metrics.last_data_time = None
    
    def _on_message(self, data: bytes):
        """Called when data is received"""
        with self.fwd.metrics._lock:
            self.fwd.metrics.last_data_time = time.time()
        logger.debug(f"[{self.name}] received {len(data)} bytes, forwarding to {self.fwd.viewer_count()} viewers")
        self.fwd.broadcast(data)
    
    def _watchdog(self):
        """Monitor for stale connections and force reconnect if needed"""
        while self.running:
            # Sleep in small increments to be responsive to shutdown
            for _ in range(10):  # Check every 10 seconds instead of 60
                if not self.running:
                    break
                time.sleep(1)
            
            if not self.running:
                break
                
            delta = self.fwd.metrics.time_since_data()
            if delta and delta > self.watchdog_timeout:
                logger.warning(
                    f"[{self.name}] No data for {delta/3600:.1f}h, forcing reconnect"
                )
                try:
                    self.rc.force_disconnect_and_reconnect()
                except Exception as e:
                    logger.error(f"[{self.name}] Watchdog reconnect failed: {e}")
    
    def stop(self):
        """Stop the stream handler"""
        self.running = False
        
        # Wait for watchdog thread to exit
        if self.watchdog_thread and self.watchdog_thread.is_alive():
            logger.debug(f"[{self.name}] Waiting for watchdog thread to exit")
            self.watchdog_thread.join(timeout=5.0)
            if self.watchdog_thread.is_alive():
                logger.warning(f"[{self.name}] Watchdog thread did not exit cleanly")


def load_config(file="connections.ini") -> list:
    """Load and validate configuration from INI file"""
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    
    if not Path(file).exists():
        logger.error(f"Configuration file '{file}' not found")
        return []
    
    cfg.read(file)
    conns = []
    used_ports = set()
    
    for section in cfg.sections():
        if not section.startswith("connection."):
            continue
        
        try:
            name = section[11:]
            remote_host = cfg.get(section, "remote_host")
            remote_port = cfg.getint(section, "remote_port")
            forward_port = cfg.getint(section, "forward_port")
            keyword = cfg.get(section, "keyword", fallback=None)
            watchdog_timeout = cfg.getint(section, "watchdog_timeout", fallback=600)  # 10 minutes default
            
            # Validate watchdog timeout
            if watchdog_timeout <= 0:
                logger.warning(f"[{name}] Invalid watchdog_timeout {watchdog_timeout}, using default 600")
                watchdog_timeout = 600
            if watchdog_timeout < 60:
                logger.warning(f"[{name}] watchdog_timeout {watchdog_timeout}s is very aggressive (< 60s)")
            
            # Validate keyword
            if keyword:
                keyword = keyword.strip()
                if len(keyword) != 4:
                    logger.warning(f"[{name}] Keyword must be exactly 4 characters (got {len(keyword)}), ignoring")
                    keyword = None
            
            # Check for port conflicts
            if forward_port in used_ports:
                logger.error(f"[{name}] Port {forward_port} already in use by another stream")
                continue
            used_ports.add(forward_port)
            
            # Validate ports
            if not (1 <= remote_port <= 65535) or not (1 <= forward_port <= 65535):
                logger.error(f"[{name}] Invalid port number")
                continue
            
            conns.append({
                "name": name,
                "remote_host": remote_host,
                "remote_port": remote_port,
                "forward_port": forward_port,
                "keyword": keyword,
                "watchdog_timeout": watchdog_timeout
            })
            
        except (ValueError, configparser.NoOptionError) as e:
            logger.error(f"Invalid configuration in section {section}: {e}")
    
    return conns


def status_monitor(handlers: list, interval: float = 30.0):
    """Print status for all streams periodically - refreshes in place"""
    
    first_run = True
    
    while True:
        try:
            # Build status output
            lines = []
            lines.append("=" * 110)
            lines.append(f"Status Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append("=" * 110)
            
            for handler in handlers:
                m = handler.fwd.metrics
                viewers = handler.fwd.viewer_count()
                
                # Get metrics under lock
                with m._lock:
                    bytes_rx = m.bytes_received
                    msgs_rx = m.messages_received
                    uptime = m.uptime()
                    delta = m.time_since_data()
                
                # Connection status
                if handler.rc.connected:
                    status = f"UP ({uptime/3600:.1f}h)"
                else:
                    status = "DOWN"
                
                # Last data time
                if delta is not None:
                    if delta < 60:
                        last_data = f"{delta:.0f}s ago"
                    elif delta < 3600:
                        last_data = f"{delta/60:.0f}m ago"
                    else:
                        last_data = f"{delta/3600:.1f}h ago"
                else:
                    last_data = "never"
                
                # Format output
                lines.append(f"[{handler.name:15s}] {status:12s} | "
                            f"Viewers: {viewers:2d} | "
                            f"Last: {last_data:10s} | "
                            f"RX: {bytes_rx/1024/1024:8.2f} MB | "
                            f"Msgs: {msgs_rx:8,d}")
            
            lines.append("=" * 110)
            lines.append("")  # Empty line at end
            
            # Print status
            if first_run:
                # First time, just print normally
                print("\n".join(lines))
                first_run = False
            else:
                # Move cursor to top and redraw
                # ANSI escape: \033[H moves cursor to home (top-left)
                # \033[J clears from cursor to end of screen
                sys.stdout.write("\033[H\033[J")
                sys.stdout.write("\n".join(lines))
                sys.stdout.flush()
        
        except Exception as e:
            logger.error(f"Error in status_monitor: {e}", exc_info=True)
        
        # Sleep after printing so first report appears immediately
        time.sleep(interval)


def main():
    """Main entry point"""
    # Clear screen at startup
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    
    print("""
╔══════════════════════════════════════════════════════════════╗
║         Multi-Forwarder v3.0 - Enhanced Edition              ║
║  Rock-solid TCP forwarding with monitoring and watchdog      ║
╚══════════════════════════════════════════════════════════════╝
""")
    
    logger.info("Forwarder starting up...")
    logger.info(f"Logs will be written to: {os.path.abspath('forwarder.log')}")
    
    # Load configuration
    connections = load_config()
    if not connections:
        logger.error("No valid connections found in configuration")
        return
    
    logger.info(f"Loaded {len(connections)} connection(s)")
    
    # Create handlers for each stream
    handlers = []
    
    for conn in connections:
        try:
            # Create forwarding server
            fwd = ForwardingServer(conn["forward_port"], conn["name"])
            fwd.start()
            
            # Create remote connection
            rc = RemoteConnection(
                host=conn["remote_host"],
                port=conn["remote_port"],
                magic_keyword=conn["keyword"],
                timeout=2.0,  # 2-second socket timeout for fast disconnection detection
                keepalive_timeout=120,  # 2 minutes - detect stale connections faster
                auto_reconnect=True,
                reconnect_delay=1.0  # 1 second - faster recovery when remote comes back
            )
            
            # Create handler (no closures!)
            handler = StreamHandler(
                name=conn["name"],
                rc=rc,
                fwd=fwd,
                config=conn,
                watchdog_timeout=conn.get("watchdog_timeout", 600)  # From config, default 10 minutes
            )
            
            handlers.append(handler)
            
            # Start connection
            rc.connect()
            
        except Exception as e:
            logger.error(f"[{conn['name']}] Failed to start: {e}")
    
    if not handlers:
        logger.error("No streams started successfully")
        return
    
    # Start status monitor
    status_thread = threading.Thread(
        target=status_monitor, 
        args=(handlers, 5.0),  # Update every 5 seconds instead of 30
        daemon=True,
        name="StatusMonitor"
    )
    status_thread.start()
    
    # Brief pause to let connections establish
    time.sleep(2)
    
    # Clear screen and show initial status
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    
    # Print connection info (will be overwritten by status monitor)
    print("="*80)
    print("Multi-Forwarder v3.0 Running")
    print("="*80)
    print("\nConnect viewers:")
    for h in handlers:
        print(f"  nc 127.0.0.1 {h.config['forward_port']:5d}  # {h.name}")
    print("\nStatus updates every 5 seconds")
    print(f"Logs: {os.path.abspath('forwarder.log')}")
    print("Press Ctrl+C to shutdown")
    print("="*80)
    print("\nWaiting for first status update...")
    
    # Main loop
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        # Clear screen for clean shutdown message
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        
        print("\n" + "="*80)
        print("Shutting down gracefully...")
        print("="*80)
        
        # Stop all handlers and connections in correct order:
        # 1. Stop handlers (stops watchdog, prevents new reconnects)
        # 2. Disconnect remote connections
        # 3. Stop forwarding servers
        for handler in handlers:
            handler.stop()
        
        for handler in handlers:
            handler.rc.disconnect()
        
        for handler in handlers:
            handler.fwd.stop()
        
        logger.info("Clean shutdown complete")
        print("\nGoodbye!\n")


if __name__ == "__main__":
    main()

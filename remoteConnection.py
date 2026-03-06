#!/usr/bin/env python3
"""
RemoteConnection - Enhanced Edition
Improvements:
- Better timeout handling with non-blocking sockets
- Proper thread cleanup and joining
- Added force_disconnect_and_reconnect() for watchdog
- Exponential backoff with max delay
- Better error messages and logging
- Thread-safe state management
"""
import socket
import threading
import time
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class RemoteConnection:
    """Manages a TCP connection with automatic reconnection"""
    
    def __init__(
        self,
        host: str,
        port: int,
        *,
        magic_keyword: Optional[str] = None,
        timeout: float = 10.0,
        keepalive_timeout: float = 300.0,
        buffer_size: int = 65536,
        auto_reconnect: bool = True,
        reconnect_delay: float = 3.0,
        max_reconnect_delay: float = 60.0
    ):
        """
        Initialize a remote connection manager.
        
        Args:
            host: Remote host to connect to
            port: Remote port to connect to
            magic_keyword: Optional 4-character handshake string
            timeout: Socket timeout for connect/send/recv operations
            keepalive_timeout: Max seconds without data before considering connection stale
            buffer_size: Receive buffer size
            auto_reconnect: Whether to automatically reconnect on disconnect
            reconnect_delay: Initial delay between reconnect attempts
            max_reconnect_delay: Maximum delay between reconnect attempts
        """
        if magic_keyword is not None and len(magic_keyword) != 4:
            raise ValueError("magic_keyword must be exactly 4 characters")

        self.host = host
        self.port = port
        self.timeout = timeout
        self.keepalive_timeout = keepalive_timeout
        self.buffer_size = buffer_size
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.magic_keyword = magic_keyword.encode("latin1") if magic_keyword else None

        self.socket: Optional[socket.socket] = None
        self.connected = False
        self.running = False
        self._last_receive_time = 0.0
        self._reconnect_active = False
        self._force_reconnect = False

        # Callbacks
        self.on_connect: Optional[Callable[[], None]] = None
        self.on_disconnect: Optional[Callable[[Exception], None]] = None
        self.on_message: Optional[Callable[[bytes], None]] = None

        self._receive_thread: Optional[threading.Thread] = None
        self._reconnect_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()  # Use RLock for recursive locking

    def connect(self) -> bool:
        """
        Attempt to connect to the remote host.
        
        Returns:
            True if connection successful, False otherwise
        """
        with self._lock:
            if self.connected:
                logger.debug(f"Already connected to {self.host}:{self.port}")
                return True

            target = f"{self.host}:{self.port}"
            
            # Debug: Check if magic keyword is set
            if self.magic_keyword:
                logger.info(f"Magic keyword configured: {self.magic_keyword} (type: {type(self.magic_keyword)})")
            else:
                logger.info(f"No magic keyword configured for {target}")

            try:
                # Create socket
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                
                # Platform-specific TCP keepalive tuning for fast crash detection
                if hasattr(socket, 'TCP_KEEPIDLE'):
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)  # Start after 60s
                if hasattr(socket, 'TCP_KEEPINTVL'):
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)  # Every 10s
                if hasattr(socket, 'TCP_KEEPCNT'):
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)  # 3 attempts
                
                self.socket.settimeout(self.timeout)
                
                # Connect
                logger.debug(f"Connecting to {target}...")
                self.socket.connect((self.host, self.port))

                # Mark as connected
                self.connected = True
                self.running = True
                self._last_receive_time = time.time()
                self._force_reconnect = False

                logger.info(f"Connected to {target}")

                # Send magic keyword if configured
                if self.magic_keyword:
                    try:
                        logger.info(f"Sending magic keyword '{self.magic_keyword.decode('latin1')}' to {target}")
                        self.socket.sendall(self.magic_keyword)
                        logger.info(f"Magic keyword sent successfully to {target}")
                    except Exception as e:
                        logger.error(f"Failed to send magic keyword to {target}: {e}")
                        # Continue anyway, connection is established

                # Wait for previous receive thread to exit if needed
                if self._receive_thread and self._receive_thread.is_alive():
                    logger.debug(f"Waiting for previous receive thread to exit for {target}")
                    self._receive_thread.join(timeout=2.0)
                
                # Start receive thread
                self._receive_thread = threading.Thread(
                    target=self._receive_loop, 
                    daemon=True,
                    name=f"Recv-{target}"
                )
                self._receive_thread.start()

                # Call connect callback
                if self.on_connect:
                    try:
                        self.on_connect()
                    except Exception as e:
                        logger.error(f"Error in on_connect callback: {e}", exc_info=True)

                return True

            except ConnectionRefusedError:
                logger.debug(f"Connection refused: {target}")
            except socket.timeout:
                logger.debug(f"Connection timeout: {target}")
            except OSError as e:
                logger.debug(f"Connection failed to {target}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error connecting to {target}: {e}", exc_info=True)

            # Schedule reconnect if enabled
            if self.auto_reconnect and not self._reconnect_active:
                self._start_reconnect_thread()
            
            return False

    def _receive_loop(self):
        """Main receive loop - runs in separate thread"""
        target = f"{self.host}:{self.port}"
        logger.debug(f"Receive loop started for {target}")
        consecutive_recv_timeouts = 0
        
        try:
            while True:
                # Check connection state under lock
                with self._lock:
                    if not self.running or not self.connected:
                        break
                    if self._force_reconnect:
                        logger.info(f"Force reconnect requested for {target}")
                        break
                
                # Check keepalive timeout at start of each iteration
                idle_time = time.time() - self._last_receive_time
                if idle_time > self.keepalive_timeout:
                    logger.warning(f"Keepalive timeout ({idle_time:.0f}s > {self.keepalive_timeout}s) for {target}")
                    self._handle_disconnect(ConnectionError("Keepalive timeout"))
                    break

                try:
                    # Receive with timeout (short timeout for faster disconnection detection)
                    data = self.socket.recv(self.buffer_size)
                    
                    if not data:
                        # Remote closed connection (empty recv = FIN from peer)
                        logger.info(f"Remote closed connection: {target}")
                        self._handle_disconnect(ConnectionError("Remote closed connection"))
                        break
                    
                    # Data received successfully - reset timeout counter
                    consecutive_recv_timeouts = 0
                    
                    # Update last receive time
                    self._last_receive_time = time.time()
                    
                    # Log data received for debugging
                    logger.debug(f"Received {len(data)} bytes from {target}")
                    
                    # Call message callback
                    if self.on_message:
                        try:
                            self.on_message(data)
                        except Exception as e:
                            logger.error(f"Error in on_message callback: {e}", exc_info=True)
                
                except socket.timeout:
                    # Socket timeout is expected - but too many consecutive ones indicate stale connection
                    consecutive_recv_timeouts += 1
                    
                    # 5 consecutive timeouts = 5+ seconds with no data AND no keepalive response
                    # Combined with keepalive_timeout (300s), this catches hung connections earlier
                    if consecutive_recv_timeouts > 5:
                        idle = time.time() - self._last_receive_time
                        logger.warning(f"Stale connection detected for {target} ({idle:.0f}s idle, {consecutive_recv_timeouts} timeouts)")
                        # Don't disconnect yet, let keepalive_timeout handle it
                        consecutive_recv_timeouts = 0  # Reset counter to avoid spam
                    
                    continue
                
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    # Connection error
                    logger.debug(f"Connection error in receive loop for {target}: {e}")
                    self._handle_disconnect(e)
                    break
                
                except Exception as e:
                    # Unexpected error
                    logger.error(f"Unexpected error in receive loop for {target}: {e}", exc_info=True)
                    self._handle_disconnect(e)
                    break
        
        finally:
            logger.debug(f"Receive loop exiting for {target}")
            # Ensure disconnect is called if we're still marked as connected
            if self.connected:
                self._handle_disconnect(ConnectionError("Receive loop exited"))

    def _handle_disconnect(self, exc: Optional[Exception] = None):
        """
        Handle disconnection - cleanup and trigger callbacks.
        
        Args:
            exc: Exception that caused the disconnect, if any
        """
        with self._lock:
            if not self.connected:
                return  # Already disconnected
            
            # Mark as disconnected
            self.connected = False
            self.running = False
            
            # Close socket
            if self.socket:
                try:
                    self.socket.close()
                except:
                    pass
                self.socket = None

            reason = str(exc) if exc else "Unknown"
            logger.debug(f"Disconnected from {self.host}:{self.port}: {reason}")

            # Call disconnect callback
            if self.on_disconnect:
                try:
                    self.on_disconnect(exc or ConnectionError(reason))
                except Exception as e:
                    logger.error(f"Error in on_disconnect callback: {e}", exc_info=True)

            # Schedule reconnect if enabled and not forced
            if self.auto_reconnect and not self._reconnect_active:
                self._start_reconnect_thread()

    def _start_reconnect_thread(self):
        """Start the reconnect thread"""
        with self._lock:
            if self._reconnect_active:
                return
            
            self._reconnect_active = True
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop,
                daemon=True,
                name=f"Reconnect-{self.host}:{self.port}"
            )
            self._reconnect_thread.start()

    def _reconnect_loop(self):
        """Reconnection loop with exponential backoff"""
        delay = self.reconnect_delay
        target = f"{self.host}:{self.port}"
        
        logger.debug(f"Reconnect loop started for {target}")
        
        try:
            while self.auto_reconnect and not self.connected:
                logger.info(f"Reconnecting to {target} in {delay:.1f}s...")
                # Sleep in small increments to be responsive to shutdown
                remaining = delay
                while remaining > 0 and self.auto_reconnect and not self.connected:
                    sleep_time = min(remaining, 1.0)
                    time.sleep(sleep_time)
                    remaining -= sleep_time
                
                # Check again in case auto_reconnect was disabled during sleep
                if not self.auto_reconnect or self.connected:
                    break
                
                # Try to connect
                if self.connect():
                    logger.info(f"Reconnected successfully to {target}")
                    break
                
                # Exponential backoff
                delay = min(delay * 1.5, self.max_reconnect_delay)
        
        finally:
            with self._lock:
                self._reconnect_active = False
            logger.debug(f"Reconnect loop exiting for {target}")

    def send(self, data: bytes) -> bool:
        """
        Send data to the remote host.
        
        Args:
            data: Bytes to send
            
        Returns:
            True if send successful, False otherwise
        """
        with self._lock:
            if not self.connected or not self.socket:
                logger.debug(f"Cannot send: not connected to {self.host}:{self.port}")
                return False
            
            try:
                self.socket.sendall(data)
                return True
            except Exception as e:
                logger.debug(f"Send failed to {self.host}:{self.port}: {e}")
                self._handle_disconnect(e)
                return False

    def force_disconnect_and_reconnect(self):
        """
        Force a disconnect and immediate reconnect.
        Used by watchdog when connection appears stale.
        """
        logger.info(f"Forcing disconnect and reconnect for {self.host}:{self.port}")
        
        with self._lock:
            if not self.connected:
                # Not connected, just try to connect
                logger.debug("Not connected, triggering connect")
                # Unlock before calling connect since it will try to acquire the lock
                pass  # Will call connect outside lock below
            else:
                # Set flag to break out of receive loop
                self._force_reconnect = True
                self.running = False
                
                # Close socket to unblock recv()
                if self.socket:
                    try:
                        self.socket.shutdown(socket.SHUT_RDWR)
                    except:
                        pass
                    try:
                        self.socket.close()
                    except:
                        pass
                    self.socket = None
                
                self.connected = False
        
        # Wait for receive thread to exit (outside lock)
        if self._receive_thread and self._receive_thread.is_alive():
            logger.debug(f"Waiting for receive thread to exit for {self.host}:{self.port}")
            self._receive_thread.join(timeout=5.0)
            if self._receive_thread.is_alive():
                logger.warning(f"Receive thread did not exit cleanly for {self.host}:{self.port}")
        
        # Trigger reconnect if auto-reconnect is enabled
        if self.auto_reconnect:
            time.sleep(1)  # Brief delay
            if self.connect():
                logger.info(f"Successfully reconnected after force_disconnect for {self.host}:{self.port}")
            else:
                logger.warning(f"Failed to reconnect after force_disconnect for {self.host}:{self.port}")
        else:
            logger.debug(f"Auto-reconnect disabled, not retrying connection for {self.host}:{self.port}")

    def disconnect(self, wait: bool = True):
        """
        Disconnect from the remote host.
        
        Args:
            wait: If True, wait for threads to exit
        """
        logger.info(f"Disconnecting from {self.host}:{self.port}")
        
        # Disable auto-reconnect first
        self.auto_reconnect = False
        self.running = False
        
        with self._lock:
            # Close socket
            if self.socket:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except:
                    pass
                try:
                    self.socket.close()
                except:
                    pass
                self.socket = None
            
            self.connected = False
        
        # Wait for threads to exit
        if wait:
            # Receive thread
            if self._receive_thread and self._receive_thread.is_alive():
                logger.debug(f"Waiting for receive thread to exit for {self.host}:{self.port}")
                self._receive_thread.join(timeout=10.0)
                if self._receive_thread.is_alive():
                    logger.warning(f"Receive thread did not exit cleanly for {self.host}:{self.port}")
            
            # Reconnect thread
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                logger.debug(f"Waiting for reconnect thread to exit for {self.host}:{self.port}")
                self._reconnect_thread.join(timeout=10.0)
                if self._reconnect_thread.is_alive():
                    logger.warning(f"Reconnect thread did not exit cleanly for {self.host}:{self.port}")
        
        logger.debug(f"Disconnected from {self.host}:{self.port}")

    def __repr__(self):
        """String representation"""
        status = "connected" if self.connected else "disconnected"
        return f"RemoteConnection({self.host}:{self.port}, {status})"

# Multi-Forwarder

A lightweight, rock-solid, multi-stream TCP forwarding daemon written in pure Python.  
Designed for reliable, low-latency forwarding of binary or text protocols from multiple remote sources to local viewer ports — with zero cross-talk.

Perfect for feeding live telemetry, market data, industrial protocols, or any high-volume binary streams into tools like `netcat`, Wireshark, or custom parsers.

### Features
- Multiple independent streams defined in a simple INI file
- Automatic reconnect with exponential back-off
- Optional 4-byte magic keyword handshake (e.g., `TEST`, `Tes2`)
- Keep-alive timeout detection
- One-to-many broadcasting per stream (multiple viewers per port supported)
- Clean unidirectional flow: source → viewers only (no back-channel consumption)
- Graceful shutdown on Ctrl+C
- Zero external dependencies

### File Structure
├── multiRemoteClient.py      Main forwarding daemon (fixed & stable)
├── remoteConnection.py       Connection handler with reconnect logic
├── connections.ini           Configuration (edit this!)
└── README.md                 This file


### connections.ini Example
```ini
[connection.DATA_TYPE1]
remote_host = 127.0.0.1
remote_port = 5000
forward_port = 8001
keyword = TEST          ; optional 4-char magic bytes sent on connect

[connection.DATA_TYPE2]
remote_host = 192.168.1.100
remote_port = 5001
forward_port = 8002
keyword = TES2
```

remote_host:remote_port → where your data is coming from
forward_port → local port viewers connect to (nc 127.0.0.1 8001)
keyword → optional exactly-4-character string sent immediately after connect (useful for protocol handshakes)

# Quick Start
1. Start your data sources (example with netcat)
```
nc -lk 127.0.0.1 5000
nc -lk 127.0.0.1 5001
```

2. In another terminal — start the forwarder
```
python multiRemoteClient.py
```

3. View the streams
```
nc 127.0.0.1 8001    # receives only from 5000
nc 127.0.0.1 8002    # receives only from 5001
```

You can open as many nc viewers as you want on the same port — all receive identical data.

# Status Monitoring
The forwarder prints live status every 30 seconds:

[DATA_TYPE1] 2025-04-05 12:34:56 | LAST DATA: 2025-04-05 12:34:54 (2s ago)


# History
### Revision 5 (December 02, 2025) – "The Immortal One"

Added force_disconnect_and_reconnect() in remoteConnection.py\
Watchdog now properly triggers full disconnect path → on_disconnect fires → reconnect actually works\
Fixed wrong host/port in "UP" log message (now uses per-instance values)\
Stale feeds now die and resurrect perfectly without blocking forever on recv()

### Revision 4 (November 30, 2025) – "No More Cross-Kills"

Made StreamHandler take explicit rc and fwd references (eliminated last closure bug)\
Watchdog now only kills its own connection — no more murdering all streams when one goes stale\
Fixed disconnect() to preserve auto_reconnect=True unless explicitly told otherwise

### Revision 3 (November 28, 2025) – "The Class Revolution"

Switched from closure hell to per-connection StreamHandler class\
Eliminated all late-binding bugs forever\
Correct per-stream names, last-seen times, and status printing\
Added stale-data watchdog (6-hour no-data → force reconnect)

### Revision 2 (November 25, 2025) – "Broadcast Restored"

Re-added missing fwd.broadcast(data) call (viewers were getting no data)\
Fixed self_lock → self.lock typo\
Per-connection status printer now prints correct name and real delta (not +30s)

### Revision 1 (November 22, 2025) – "The Great Closure Massacre"

Removed data-eating _cleaner thread\
Fixed late-binding closure that made all streams use the last ForwardingServer\
Eliminated cross-talk between streams\
Multiple viewers per port now work perfectly

Requirements

Python 3.6+
No pip packages required

License
Unlicensed / Free for any use. Do whatever you want with it.
Built for speed, stability, and sanity. Enjoy your clean streams.
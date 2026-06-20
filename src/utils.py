"""
Utility functions for the agent.
"""

import time
import os
import uuid

def uuid7() -> str:
    """
    Generate a UUIDv7 (approximate).
    Format: 48-bit timestamp (ms) | 4-bit version (7) | 12-bit fractional | 2-bit variant | 62-bit random.
    """
    # Milliseconds since epoch
    ms = int(time.time() * 1000)
    
    # 48 bits for timestamp
    # 4 bits for version (0x7)
    # 12 bits for sub-ms (we can just use random or leave 0)
    # 2 bits for variant (0x2)
    # 62 bits for random
    
    # Simple construction for 3.12 compatibility
    # [48 bits timestamp][4 bits version][74 bits random with variant]
    timestamp_hex = f"{ms:012x}"
    v7_hex = timestamp_hex[:8] + "-" + timestamp_hex[8:] + "-7" + os.urandom(8).hex()[:3] + "-a" + os.urandom(8).hex()[:3] + "-" + os.urandom(8).hex()[:12]
    
    # Better yet, let's use a more formal approach if needed, 
    # but the API probably just wants a v7-shaped string.
    # Let's try to be a bit more accurate.
    
    rand_bytes = os.urandom(10)
    # ms (6 bytes) | rand (10 bytes)
    # byte 6: high 4 bits version 7
    # byte 8: high 2 bits variant 2
    
    b = bytearray(ms.to_bytes(6, 'big') + rand_bytes)
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    
    return str(uuid.UUID(bytes=bytes(b)))

"""
SMS System for AI BlackBox Flight Recorder.

Uses TG NeoGate AMI (Asterisk Manager Interface) for sending/receiving SMS.
Multi-gateway: one AMI client per enabled gateway, owned by AMIConnectionManager.
"""

_manager = None
_sms_router = None
_message_store = None


async def start_sms_system():
    """Initialize and start the SMS subsystem (one AMI client per gateway)."""
    from .manager import AMIConnectionManager
    from .message_store import MessageStore
    from .router import SMSRouter

    global _manager, _sms_router, _message_store
    _message_store = MessageStore()
    _manager = AMIConnectionManager()
    await _manager.start()  # connects one client per enabled gateway
    # Router registers its inbound callback via manager.set_sms_callback.
    _sms_router = SMSRouter(_manager, _message_store)
    print(f"[SMS] System started — {len(_manager.clients())} gateway(s) connected")


async def stop_sms_system():
    """Shut down the SMS subsystem."""
    global _manager, _sms_router, _message_store
    if _manager is not None:
        await _manager.stop()
    _manager = None
    _sms_router = None
    _message_store = None
    print("[SMS] System stopped")


def get_ami_client(gateway_id=None):
    """Return an AMI client.

    With `gateway_id`, returns that gateway's client; otherwise the default
    (first enabled) gateway's client. None-safe if the system isn't started.
    Back-compat: existing no-arg callers get the default client.
    """
    if _manager is None:
        return None
    if gateway_id:
        return _manager.get(gateway_id)
    return _manager.default()


def get_manager():
    """Return the AMIConnectionManager (or None if not started)."""
    return _manager


def get_message_store():
    return _message_store


def get_router():
    return _sms_router

"""
SMS System for AI BlackBox Flight Recorder.

Uses TG200 AMI (Asterisk Manager Interface) for sending/receiving SMS.
"""

_ami_client = None
_sms_router = None
_message_store = None


async def start_sms_system():
    """Initialize and start the SMS subsystem."""
    global _ami_client, _sms_router, _message_store
    from .ami_client import AMISMSClient
    from .message_store import MessageStore
    from .router import SMSRouter
    from Orchestrator.config import (
        ASTERISK_AMI_HOST, ASTERISK_AMI_PORT, ASTERISK_AMI_USER, ASTERISK_AMI_SECRET,
    )

    _message_store = MessageStore()
    _ami_client = AMISMSClient(
        host=ASTERISK_AMI_HOST, port=ASTERISK_AMI_PORT,
        username=ASTERISK_AMI_USER, secret=ASTERISK_AMI_SECRET,
    )
    await _ami_client.connect()
    _sms_router = SMSRouter(_ami_client, _message_store)
    print("[SMS] System started — AMI connected, listening for incoming SMS")


async def stop_sms_system():
    """Shut down the SMS subsystem."""
    global _ami_client
    if _ami_client:
        await _ami_client.disconnect()
        _ami_client = None
    print("[SMS] System stopped")


def get_ami_client():
    return _ami_client


def get_message_store():
    return _message_store


def get_router():
    return _sms_router

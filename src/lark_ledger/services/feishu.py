"""Feishu integration facade (pure re-exports).

The public import surface is unchanged; implementations live in
``feishu_client``, ``feishu_crypto`` and ``message_processor``.
"""

from lark_ledger.services.feishu_client import (
    FeishuClient,
    _feishu_error_details,
    _media_fingerprint,
    _safe_export_filename,
    _write_export_temp_file,
    logger,
)
from lark_ledger.services.feishu_crypto import decrypt_event, verify_signature
from lark_ledger.services.message_processor import MAX_POST_IMAGES, MessageProcessor

__all__ = [
    "FeishuClient",
    "MAX_POST_IMAGES",
    "MessageProcessor",
    "_feishu_error_details",
    "_media_fingerprint",
    "_safe_export_filename",
    "_write_export_temp_file",
    "decrypt_event",
    "logger",
    "verify_signature",
]

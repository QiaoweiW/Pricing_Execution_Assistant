from datetime import datetime


def make_batch_number(user_initials: str = "WAN") -> str:
    """Mirror VBAFE PreUploadMacro=DefaultBatchNumber. Format: WAN0619261643"""
    return f"{user_initials}{datetime.now():%m%d%y%H%M}"

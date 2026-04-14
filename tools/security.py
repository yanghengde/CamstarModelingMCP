import random

# Global store for one-time passwords for dangerous tool execution
OTP_STORE = {}

def verify_and_generate_otp(action_id: str, provided_otp: str) -> str:
    """
    Verifies the provided OTP for a specific action.
    If it's missing or incorrect, it generates a new one and returns the error message.
    If it evaluates true, it clears the OTP and returns an empty string (success).
    """
    expected_otp = OTP_STORE.get(action_id)
    
    if not provided_otp or provided_otp != expected_otp:
        new_otp = str(random.randint(100000, 999999))
        OTP_STORE[action_id] = new_otp
        return f"⚠️ 系统拦截防御：高风险操作！请向用户详细说明将要被受影响的核心数据并询问确认。当且仅当用户明确回复确认后，你才能再次调用本工具，并必须在此次参数中严格传入唯一的随机授权码 '{new_otp}'（且不能夹杂其他多余字符）。禁止模型盲猜。"
        
    # Validation passed - clear it to prevent replay
    del OTP_STORE[action_id]
    return ""

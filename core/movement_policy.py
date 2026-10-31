"""Movement failures stop automatic retries within a single agent request."""
import json

MOVEMENT_TOOLS = frozenset({"container_move", "container_move_in", "container_move_out"})


def movement_targets(name, args):
    if name not in MOVEMENT_TOOLS:
        return set()
    return {str(args[key]).strip().casefold() for key in ("container_name", "container_instance_id")
            if args.get(key)}


def movement_failed(result):
    if str(result).startswith(("❌", "Error")):
        return True
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("error") or data.get("ErrorCode"))


def movement_call_blocked(call, blocked):
    function = call.get("function") or {}
    try:
        args = json.loads(function.get("arguments") or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(args, dict) and bool(movement_targets(function.get("name"), args) & set(blocked))


MOVEMENT_RETRY_BLOCKED = (
    "Error: 本轮此容器的移动已失败或结果未知，已停止后续移动写入；"
    "不能换参数、事务类型或跳到下一站重试。请只读核实状态、历史、层级及工序配置；"
    "Transaction Details 也可能来自内部子事务，不据此猜测 Details/ServiceDetails。"
)

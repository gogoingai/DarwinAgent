"""纯准入层规则（锚定零成本选址）：只被准入/预检调用，不进作答路径。
kernel/validation.py 是答案路径共享文件——放那里会作废全部答案检查点（v21 锚定事故）。"""

import ast

from darwinagent.operators.sandbox import DATA_CAPABILITIES as DATA_CAPABILITY_NAMES


def loop_carried_capability_errors(source):
    """循环内逐行能力调用（历史预算故障的静态病灶）：for 体内调用 traverse/nodes/
    search 等数据能力＝每行一次图遍历/扫描——并发负载下超时。必须批量化：一次传全量 id。"""
    tree = ast.parse(source)
    errors = []

    def walk(node, in_loop):
        for child in ast.iter_child_nodes(node):
            loop = in_loop or isinstance(child, (ast.For, ast.AsyncFor, ast.While))
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in DATA_CAPABILITY_NAMES
                and in_loop
            ):
                errors.append(
                    f"能力调用 {child.func.id} 在循环体内（逐行调用）——"
                    f"批量化：一次调用传全量 id/条件"
                )
            walk(child, loop)

    walk(tree, False)
    return errors

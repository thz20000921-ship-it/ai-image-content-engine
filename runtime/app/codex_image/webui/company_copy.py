"""Share title and numbering constraints with the browser, without vision calls."""
import json
from pathlib import Path


def default_policy() -> dict:
    return json.loads((Path(__file__).parent / 'static/company-copy-rules.json').read_text(encoding='utf-8'))


def copy_policy(master: str, total: int, selected: list[int], policy: dict | None = None) -> tuple[str, str]:
    policy = policy or default_policy()
    if type(total) is not int or not 1 <= total <= 12 or not selected or any(
        type(page) is not int or not 1 <= page <= total for page in selected
    ) or selected != sorted(set(selected)):
        raise ValueError('本套页序无效')
    clean = master.split('\n\n' + policy['marker'], 1)[0].rstrip()
    rows = []
    for index, page in enumerate(selected, 1):
        if page == 1:
            rows.append(f'参考第1页 → 输出第{index}页；页面角色以本次规划为准。')
        else:
            rows.append(f'参考第{page}页 → 输出第{index}页；正文编号仅按已确认文案执行。')
    rules = '\n'.join(policy['rules'])
    mapping = '\n'.join(rows)
    ids = '、'.join(map(str, selected))
    context = f"""{policy['marker']}
{rules}
【本次保留页清单：程序计数，不能沿用参考图旧数字】
参考素材{total}张，本次兼容入口输出{len(selected)}张；参考页码：{ids}。不从图片张数推断正文事实数量。
{mapping}"""
    return clean, context

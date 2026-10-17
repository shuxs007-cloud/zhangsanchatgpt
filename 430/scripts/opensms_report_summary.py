"""Keep business counts/errors, omit bulky per-number Markdown tables."""
import re
from collections import Counter


def compact_phone_tables(lines):
    source = "\n".join(lines).splitlines()
    result = []
    index = 0
    while index < len(source):
        line = source[index]
        if line.lstrip().startswith("|"):
            end = index
            while end < len(source) and source[end].lstrip().startswith("|"):
                end += 1
            table = source[index:end]
            headers = [re.sub(r"[*`\s]", "", cell) for cell in line.strip().strip("|").split("|")]
            data = [row for row in table[1:] if not re.fullmatch(r"[\s|:\-]+", row)]
            cells = [[value.strip().strip('`') for value in row.strip().strip('|').split('|')] for row in data]
            id_columns = [i for i, cell in enumerate(headers)
                          if re.search(r"(?:电话|手机号|卡号|号码|ICCID|SIMNumber)", cell, re.I)
                          and not re.search(r"(?:数量|人数|统计|占比|号码数)", cell)]
            detail = any(i < len(row) and re.search(r"(?<!\d)\d{10,22}[Ff]?(?!\d)", row[i])
                         for row in cells for i in id_columns)
            if detail and len(table) > 1:
                result.append(f"明细记录：{len(data)} 条。逐号身份、结果和原因保留在对应工作簿及原始回执。")
                reason_columns = [i for i, cell in enumerate(headers) if re.search(r"(?:原因|结果|状态|处理说明)", cell)]
                if reason_columns:
                    i = reason_columns[-1]
                    reasons = Counter(re.sub(r"(?<!\d)\d{10,22}[Ff]?(?!\d)", "[号码]", row[i])[:100]
                                      for row in cells if i < len(row))
                    top = reasons.most_common(8)
                    result.append("分类统计：" + "；".join(f"{reason} {count}条" for reason, count in top)
                                  + (f"；其它原因 {sum(reasons.values()) - sum(n for _, n in top)}条" if len(reasons) > 8 else "") + "。")
                result.append("")
            else:
                result.extend(table)
            index = end
            continue
        if line.startswith("#"):
            line = line.replace("逐号结果", "结果汇总").replace("逐行清单", "数量汇总").replace("号码明细", "数量汇总")
        result.append(line)
        index += 1
    return result

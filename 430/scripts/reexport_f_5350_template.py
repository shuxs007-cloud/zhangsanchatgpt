from pathlib import Path
import hashlib,json
import openpyxl
from opensms_export_task_f_deadlines import export_deadlines,DEFAULT_DB

source=Path('/Volumes/zs01/chatgpt/OpenSMS/20261002/20261002T000817+0800/OpenSMS_系统缺激活日期_F库已有_20261002T000817+0800.xlsx')
assert hashlib.sha256(source.read_bytes()).hexdigest()=='d673ebe833b3facbddcebc17e107873a60390f4dacd857621f55a8ff82007d46'
w=openpyxl.load_workbook(source,read_only=True,data_only=True)
s=w.active
it=s.iter_rows(values_only=True); headers=list(next(it))
cols=[headers.index(k) for k in ['F库查询ICCID','F库截止日','F库激活日期']]
rows=[[r[i] for i in cols] for r in it];w.close()
assert len(rows)==5350 and len({r[0] for r in rows})==5350
out=Path('/Users/zhangsan/Downloads/430/outputs/f_5350_user_template_20261002')
result=export_deadlines(DEFAULT_DB,Path('/Users/zhangsan/Downloads/431/截止日导入模板_1790871905094.xlsx'),out/'系统缺激活日期_F库已有_5350条_截止日导入.xlsx',out/'核验回执.json',selected_rows=rows,extra_receipt={'sourceDifference':str(source),'sourceRunId':'20261002T000817+0800'})
print(json.dumps(result,ensure_ascii=False))

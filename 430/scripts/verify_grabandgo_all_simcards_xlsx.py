#!/usr/bin/env python3

import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    if len(sys.argv) != 3:
        raise SystemExit("用法: verify_grabandgo_all_simcards_xlsx.py OUTPUT.xlsx SOURCE.json")
    xlsx_path = Path(sys.argv[1]).resolve()
    source_path = Path(sys.argv[2]).resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    expected_rows = source["rows"]
    expected_sims = [str(row["fields"]["lblSimCardNumber"]) for row in expected_rows]

    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(xlsx_path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet_names = [node.attrib["name"] for node in workbook.findall(".//x:sheet", ns)]
        data_sheet = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))

    rows = data_sheet.findall(".//x:sheetData/x:row", ns)
    cells_by_ref = {
        cell.attrib.get("r"): cell
        for cell in data_sheet.findall(".//x:sheetData/x:row/x:c", ns)
    }
    sim_cells = [cells_by_ref.get(f"E{row_number}") for row_number in range(2, len(expected_sims) + 2)]
    sim_cells_present = all(cell is not None for cell in sim_cells)
    text_type_count = sum(1 for cell in sim_cells if cell is not None and cell.attrib.get("t") == "str")
    formula_count = sum(1 for cell in sim_cells if cell is not None and cell.find("x:f", ns) is not None)
    exact_count = 0
    for expected, cell in zip(expected_sims, sim_cells):
        if cell is None:
            continue
        value = cell.findtext("x:v", default="", namespaces=ns)
        formula = cell.findtext("x:f", default="", namespaces=ns)
        if value == expected and formula == f'"{expected}"':
            exact_count += 1

    result = {
        "status": "passed" if (
            sheet_names == ["抓取汇总", "全部SIM数据"]
            and len(rows) == len(expected_rows) + 1
            and sim_cells_present
            and text_type_count == len(expected_sims)
            and formula_count == len(expected_sims)
            and exact_count == len(expected_sims)
        ) else "failed",
        "sheetNames": sheet_names,
        "sourceRowCount": len(expected_rows),
        "xlsxDataRowCount": len(rows) - 1,
        "simCellsPresent": sim_cells_present,
        "simCellsTextTypeCount": text_type_count,
        "simCellsFormulaCount": formula_count,
        "simCellsExactMatchCount": exact_count,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

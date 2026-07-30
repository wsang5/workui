"""Deprecated local lark-cli backend for PE Prompt Reviewer.

The maintained runtime is now ``spark-app/server.js`` (Node.js / Express).
This file remains only as a migration reference. Set
``ALLOW_LEGACY_PY_SERVER=1`` if you intentionally need to run it.
"""

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import csv
import io
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import qc_sampling

if os.environ.get("ALLOW_LEGACY_PY_SERVER") != "1":
    raise SystemExit(
        "lark_server.py is deprecated. Use spark-app/server.js instead. "
        "Set ALLOW_LEGACY_PY_SERVER=1 to run the legacy Python service."
    )


class ConflictError(Exception):
    """Raised when a QC row changed after the client loaded it."""


APP_ROOT = Path(__file__).resolve().parent
os.chdir(APP_ROOT)
DEFAULT_ROW_COUNT = 623
CONFIG_PATH = Path("lark_sources.json")
HOST = "0.0.0.0"
PORT = 8766
MAX_READ_COLUMN = "BZ"
READ_ROW_CHUNK_SIZE = 40
BASE_RECORD_PAGE_SIZE = 200
ROWS_CACHE_TTL_SECONDS = 120
LABEL_TAXONOMY_CACHE_TTL_SECONDS = 3600
SPATIAL_TARGET_URL = "https://bytedance.larkoffice.com/sheets/ALdDsY7EKh2y7wtZ2LvcTuOunAm"
SPATIAL_TARGET_SHEET_ID = "2ca77f"
LABEL_TAXONOMY_URL = "https://bytedance.larkoffice.com/sheets/UpKjsLQVqhtSuatGfGaca4Fgncb"
IMAGE_LABEL_SHEET_ID = "51bb80"
PE_LABEL_SHEET_ID = "3un0fe"
I2I_IMAGE_LABEL_SHEET_ID = "IkimLl"
I2I_PE_LABEL_SHEET_ID = "bqiYJJ"
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_CACHE_LOCK = threading.RLock()
_WRITE_LOCK = threading.Lock()
_ROWS_CACHE = {}
_LABEL_TAXONOMY_CACHE = {"expires_at": 0, "payload": None}
_QC_CONFIG = dict(qc_sampling.DEFAULT_POOL_CONFIG)
QC_LEASE_TTL_SECONDS = 5 * 60
_QC_LEASES = {}
QC_CHANGE_LOG_SHEET_NAME = "质检变更日志"
QC_CHANGE_LOG_HEADERS = ["操作时间", "操作人", "操作类型", "prompt_id", "原始行号", "评测人", "变更项", "字段列", "原值", "新值", "备注", "来源"]


def local_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "你的Mac局域网IP"


def run_lark(args):
    completed = subprocess.run(
        ["lark-cli", *args, "--as", "user"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def csv_text(rows):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    return buf.getvalue()


def cells_payload(values):
    return [[{"value": cell} for cell in row] for row in values]


def cells_get_values(result):
    """Normalize lark-cli +cells-get / legacy +read value payloads to a 2D value array."""
    data = result.get("data") or {}
    legacy = data.get("valueRange") or {}
    if "values" in legacy:
        return legacy.get("values") or []
    ranges = data.get("ranges") or []
    if not ranges:
        return []
    return [[cell_to_text(cell.get("value", "")) if isinstance(cell, dict) else cell_to_text(cell) for cell in row] for row in (ranges[0].get("cells") or [])]


def empty_sheet_source():
    return {"type": "sheet", "title": "飞书表格", "url": "", "sheetId": "", "rowCount": DEFAULT_ROW_COUNT}


def empty_base_source():
    return {"type": "base", "title": "飞书多维表格", "url": "", "baseToken": "", "tableId": "", "viewId": "", "rowCount": DEFAULT_ROW_COUNT}


def source_type(source):
    explicit = (source or {}).get("type") or (source or {}).get("sourceType")
    if explicit in {"sheet", "base"}:
        return explicit
    url = str((source or {}).get("url") or "")
    if "/base/" in url:
        return "base"
    return "sheet"


def normalize_source(source):
    source = source or {}
    normalized = empty_base_source() if source_type(source) == "base" else empty_sheet_source()
    normalized.update({key: value for key, value in source.items() if value not in (None, "")})
    normalized["type"] = source_type(normalized)
    normalized["rowCount"] = max(3, int(normalized.get("rowCount") or DEFAULT_ROW_COUNT))
    return normalized


def load_source_config():
    if CONFIG_PATH.exists():
        try:
            payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}

    sources = [normalize_source(item) for item in payload.get("sources", []) if (item or {}).get("url")]

    by_url = {}
    for source in sources:
        by_url[source["url"]] = source
    sources = list(by_url.values())

    active_url = payload.get("activeUrl")
    if active_url not in by_url:
        active_url = sources[0]["url"] if sources else ""
    return {"activeUrl": active_url, "sources": sources}


def save_source_config(config):
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_source_key(source):
    """Return a stable key for caches that depend on the selected source."""
    source = normalize_source(source)
    if source_type(source) == "base":
        return ("base", source.get("baseToken", ""), source.get("tableId", ""), source.get("viewId", ""))
    return ("sheet", source.get("url", ""), source.get("sheetId", ""), int(source.get("rowCount") or DEFAULT_ROW_COUNT))


def clear_runtime_caches(source=None):
    """Invalidate in-memory caches after source changes or successful writes."""
    with _CACHE_LOCK:
        if source is None:
            _ROWS_CACHE.clear()
        else:
            _ROWS_CACHE.pop(cache_source_key(source), None)


def active_source():
    config = load_source_config()
    if not config["sources"]:
        raise ValueError("尚未配置任何飞书数据源，请在页面顶部粘贴飞书表格或多维表格链接后加载。")
    return next((source for source in config["sources"] if source["url"] == config["activeUrl"]), config["sources"][0])


def source_from_payload(payload):
    """写入接口按页面携带的数据源定位，避免多人切换 activeUrl 后写错表。"""
    payload = payload or {}
    source_url = payload.get("sourceUrl") or ""
    source_sheet_id = payload.get("sourceSheetId") or ""
    if source_url:
        config = load_source_config()
        source = next((item for item in config["sources"] if item["url"] == source_url), None)
        if source:
            if source_sheet_id and source_type(source) == "sheet" and not source.get("sheetId"):
                source = {**source, "sheetId": source_sheet_id}
            return source
        if "/base/" in source_url:
            parsed = parse_base_url(source_url)
            return normalize_source({"type": "base", "url": source_url, **parsed})
        return normalize_source({"type": "sheet", "url": source_url, "sheetId": source_sheet_id})
    return active_source()


def parse_base_url(url):
    parsed = urlparse(str(url))
    parts = [part for part in parsed.path.split("/") if part]
    token = ""
    if "base" in parts:
        index = parts.index("base")
        if index + 1 < len(parts):
            token = parts[index + 1]
    query = parse_qs(parsed.query)
    table_id = (query.get("table") or [""])[0]
    view_id = (query.get("view") or [""])[0]
    return {"baseToken": token, "tableId": table_id, "viewId": view_id}


def list_from_payload(payload, *keys):
    candidates = [payload, (payload or {}).get("data") if isinstance(payload, dict) else None]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, list):
                return value
        for value in candidate.values():
            if isinstance(value, dict):
                for key in keys:
                    nested = value.get(key)
                    if isinstance(nested, list):
                        return nested
    return []


def first_field(item, names, default=""):
    if not isinstance(item, dict):
        return default
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return value
    return default


def parse_sheet_id_from_url(url):
    """从 /sheets/ 链接的 ?sheet=<id> 取子表 id；取不到返回空。"""
    query = parse_qs(urlparse(str(url)).query)
    return (query.get("sheet") or [""])[0]


def inspect_sheet_source(url):
    result = run_lark(["sheets", "+info", "--url", url])
    spreadsheet = result["data"]["spreadsheet"]["spreadsheet"]
    sheets = result["data"]["sheets"]["sheets"]
    if not sheets:
        raise ValueError("该飞书表格没有可读取的 sheet")
    wanted_sheet_id = parse_sheet_id_from_url(url)
    sheet = next((item for item in sheets if item.get("sheet_id") == wanted_sheet_id), sheets[0])
    title = spreadsheet.get("title") or "未命名飞书表格"
    sheet_title = sheet.get("title") or ""
    if sheet_title and len(sheets) > 1:
        title = f"{title} / {sheet_title}"
    return {
        "type": "sheet",
        "title": title,
        "url": url,
        "sheetId": sheet["sheet_id"],
        "rowCount": int((sheet.get("grid_properties") or {}).get("row_count") or DEFAULT_ROW_COUNT),
    }


def inspect_base_source(url):
    parsed = parse_base_url(url)
    if not parsed.get("baseToken"):
        raise ValueError("无法从多维表格链接中解析 base token，请粘贴 /base/ 开头的链接。")
    base_token = parsed["baseToken"]
    base_result = run_lark(["base", "+base-get", "--base-token", base_token, "--format", "json"])
    table_id = parsed.get("tableId") or ""
    tables = []
    if not table_id:
        table_result = run_lark(["base", "+table-list", "--base-token", base_token, "--format", "json", "--limit", "100"])
        tables = list_from_payload(table_result, "items", "tables", "records")
        if not tables:
            raise ValueError("该多维表格没有可读取的数据表")
        table_id = first_field(tables[0], ["table_id", "tableId", "id", "name"])
    base_data = base_result.get("data") if isinstance(base_result, dict) else {}
    base_info = base_data.get("base") if isinstance(base_data, dict) and isinstance(base_data.get("base"), dict) else base_data
    table_title = ""
    for table in tables:
        if first_field(table, ["table_id", "tableId", "id", "name"]) == table_id:
            table_title = first_field(table, ["name", "title", "table_name", "tableName"])
            break
    title = first_field(base_info or {}, ["name", "title"], "未命名多维表格")
    if table_title:
        title = f"{title} / {table_title}"
    return {
        "type": "base",
        "title": title,
        "url": url,
        "baseToken": base_token,
        "tableId": table_id,
        "viewId": parsed.get("viewId") or "",
        "rowCount": DEFAULT_ROW_COUNT,
    }


def inspect_source(url):
    text = str(url or "").strip()
    if "/base/" in text:
        return inspect_base_source(text)
    if "/sheets/" in text or "/spreadsheets/" in text:
        return inspect_sheet_source(text)
    raise ValueError("当前页面支持加载飞书表格 /sheets/ 或多维表格 /base/ 链接；wiki/docx 文档不能作为评测数据源。")


def add_or_select_source(url=None, active_url=None, remove_url=None):
    config = load_source_config()
    if remove_url:
        config["sources"] = [item for item in config["sources"] if item["url"] != remove_url]
        if config["activeUrl"] == remove_url:
            config["activeUrl"] = config["sources"][0]["url"] if config["sources"] else ""
    elif url:
        source = inspect_source(url.strip())
        existing_index = next((index for index, item in enumerate(config["sources"]) if item["url"] == source["url"]), None)
        if existing_index is None:
            config["sources"].append(source)
        else:
            config["sources"][existing_index] = source
        config["activeUrl"] = source["url"]
    elif active_url:
        if not any(item["url"] == active_url for item in config["sources"]):
            raise ValueError("未找到这个已保存的数据源")
        config["activeUrl"] = active_url
    else:
        raise ValueError("请提供飞书表格链接")

    save_source_config(config)
    clear_runtime_caches()
    return config


def cell_to_text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("link") or item.get("url") or item.get("name") or item.get("value") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "link", "url", "name", "value"):
            if value.get(key) not in (None, ""):
                return str(value.get(key))
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def cell_image_tokens(value):
    """从单元格里提取飞书内嵌图片的 fileToken（embed-image 类型）。"""
    tokens = []

    def collect(item):
        if isinstance(item, dict):
            token = item.get("fileToken") or item.get("file_token")
            if token and token not in tokens:
                tokens.append(str(token))
        elif isinstance(item, list):
            for sub in item:
                collect(sub)

    collect(value)
    return tokens


def column_letter(index):
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def column_index(letters):
    if not letters:
        return None
    value = 0
    for char in letters:
        value = value * 26 + (ord(char.upper()) - 64)
    return value - 1


def normalize_header(value):
    return cell_to_text(value).strip()


def normalize_header_key(value):
    return normalize_header(value).replace(" ", "").replace("_", "").replace("：", "").replace(":", "").lower()


def strip_suffix_case_insensitive(value, suffix):
    """大小写不敏感地剥离固定后缀；用于兼容 xx-PE内容 这类实验组字段。"""
    text = normalize_header(value)
    if text.lower().endswith(suffix.lower()):
        return text[: -len(suffix)].strip()
    return ""


def grouped_prompt_base_name(header):
    """识别实验组 prompt 列，兼容 xx-PE内容 / xx-pe内容 / xx-内容。"""
    return (
        strip_suffix_case_insensitive(header, "-PE内容")
        or strip_suffix_case_insensitive(header, "-pe内容")
        or strip_suffix_case_insensitive(header, "-内容")
    )


def grouped_annotation_base_name(header):
    """识别新 i2i 模版中的实验组标注记录列，兼容 xx-标注记录 / xx-PE标注记录。"""
    return strip_suffix_case_insensitive(header, "-PE标注记录") or strip_suffix_case_insensitive(header, "-标注记录")


def grouped_machine_annotation_base_name(header):
    """识别实验组机器标注记录列，如 xx-机器标注记录。"""
    return strip_suffix_case_insensitive(header, "-机器标注记录")


def grouped_adjustment_flag_base_name(header):
    """识别实验组调整标识列，如 xx-调整标识。"""
    return strip_suffix_case_insensitive(header, "-调整标识")


def prompt_base_name(header):
    compact = header.replace(" ", "").replace("_", "").lower()
    lower = header.lower()
    if compact in {"promptid", "promptcn"}:
        return ""
    if (
        "标注记录" in header
        or header.endswith("-问题总结")
        or header.endswith("-问题分析")
        or header.endswith("-问题标签")
        or header.endswith("-PE问题标签")
        or header.endswith("-图像效果标签")
        or header.endswith("-结果总结")
        or header.endswith("-调整标识")
    ):
        return ""
    grouped_base = grouped_prompt_base_name(header)
    if grouped_base:
        return grouped_base
    if lower.startswith("prompt-") and not lower.endswith("-结果"):
        return header
    if compact in {"peprompt", "人工prompt"}:
        return header
    return ""


def result_base_name(header):
    lower = header.lower()
    if lower.startswith("prompt-") and lower.endswith("-结果"):
        return header[:-3]
    grouped_base = strip_suffix_case_insensitive(header, "-结果")
    if grouped_base and "prompt" not in normalize_header_key(grouped_base):
        return grouped_base
    if "prompt" in lower and header.endswith("结果"):
        return header[:-2]
    return ""


def issue_base_name(header, suffix):
    if suffix == "-问题标签":
        return strip_suffix_case_insensitive(header, "-PE问题标签") or strip_suffix_case_insensitive(header, suffix)
    return strip_suffix_case_insensitive(header, suffix)


def build_issue_groups(headers, prompt_bases=None):
    """按第二行列名发现 xx-图像效果标签 / xx-PE问题标签 / xx-问题总结 字段组。"""
    prompt_bases = prompt_bases or []
    pe_labels_by_base = {}
    image_labels_by_base = {}
    summaries_by_base = {}
    for index, header in enumerate(headers):
        image_label_base = issue_base_name(header, "-图像效果标签")
        if image_label_base:
            image_labels_by_base[image_label_base] = {"index": index, "header": header}
            continue
        label_base = issue_base_name(header, "-问题标签")
        if label_base:
            pe_labels_by_base[label_base] = {"index": index, "header": header}
            continue
        summary_base = issue_base_name(header, "-问题总结") or issue_base_name(header, "-问题分析") or issue_base_name(header, "-结果总结")
        if summary_base:
            summaries_by_base[summary_base] = {"index": index, "header": header}

    discovered = sorted(set(summaries_by_base) | set(pe_labels_by_base) | set(image_labels_by_base))
    bases = [base for base in prompt_bases if base in discovered]
    bases.extend(base for base in discovered if base not in bases)
    if not bases:
        return []

    groups = []
    for position, base in enumerate(bases):
        pe_label = pe_labels_by_base.get(base, {"index": None, "header": f"{base}-PE问题标签"})
        image_label = image_labels_by_base.get(base, {"index": None, "header": f"{base}-图像效果标签"})
        summary = summaries_by_base.get(base, {"index": None, "header": f"{base}-问题分析"})
        groups.append(
            {
                "id": f"issue-{position}",
                "labelTitle": pe_label["header"],
                "imageLabelTitle": image_label["header"],
                "summaryTitle": summary["header"],
                "labelColumn": column_letter(pe_label["index"]) if pe_label["index"] is not None else "",
                "imageLabelColumn": column_letter(image_label["index"]) if image_label["index"] is not None else "",
                "summaryColumn": column_letter(summary["index"]) if summary["index"] is not None else "",
            }
        )
    return groups


def is_three_column_observation(headers):
    """仅当表头中有 3 个 prompt-*-结果 列时启用三列观测页专用映射。"""
    return sum(1 for header in headers if header.lower().startswith("prompt-") and header.lower().endswith("-结果")) == 3


def build_three_column_mapping(headers):
    """三列观测表固定映射：D/E/F 为 prompt，K/L/J 为结果，标注记录按第二行列名匹配。"""
    annotation_by_header = {header: index for index, header in enumerate(headers) if header.endswith("标注记录")}
    prompt_specs = [
        {"prompt": 3, "result": 10, "annotatable": True},
        {"prompt": 4, "result": 11, "annotatable": True},
        {"prompt": 5, "result": 9, "annotation": None, "annotatable": False},
    ]
    groups = []
    for position, spec in enumerate(prompt_specs):
        prompt_header = headers[spec["prompt"]] if spec["prompt"] < len(headers) else f"prompt-{position + 1}"
        result_header = headers[spec["result"]] if spec["result"] < len(headers) else f"{prompt_header}-结果"
        annotation_index = annotation_by_header.get(f"{prompt_header}-标注记录")
        groups.append(
            {
                "id": f"prompt-{position}",
                "label": prompt_header or f"prompt-{position + 1}",
                "resultLabel": result_header or f"{prompt_header or f'prompt-{position + 1}'} 结果",
                "promptColumn": column_letter(spec["prompt"]),
                "resultColumn": column_letter(spec["result"]),
                "annotationColumn": column_letter(annotation_index) if annotation_index is not None else "",
                "annotatable": spec["annotatable"] and annotation_index is not None,
            }
        )
    return {
        "mode": "three-column",
        "headers": headers,
        "groups": groups,
        "byHeader": {header: index for index, header in enumerate(headers) if header},
        "summaryColumn": "R",
        "machineColumn": "S",
        "historyProblemColumn": "V",
    }


def build_column_mapping(headers):
    by_header = {header: index for index, header in enumerate(headers) if header}
    result_by_base = {}
    annotation_columns = []
    machine_annotation_by_base = {}
    adjustment_flag_by_base = {}
    prompt_columns = []
    seen_prompt_bases = set()

    for index, header in enumerate(headers):
        base = prompt_base_name(header)
        if base:
            if base in seen_prompt_bases:
                annotation_columns.append({"index": index, "header": header, "base": base})
                continue
            seen_prompt_bases.add(base)
            prompt_columns.append({"index": index, "header": header, "base": base, "label": base if grouped_prompt_base_name(header) else header, "grouped": bool(grouped_prompt_base_name(header))})
            continue

        result_base = result_base_name(header)
        if result_base:
            result_by_base[result_base] = index
            continue

        machine_base = grouped_machine_annotation_base_name(header)
        if machine_base:
            machine_annotation_by_base[machine_base] = index
            continue

        adjustment_base = grouped_adjustment_flag_base_name(header)
        if adjustment_base:
            adjustment_flag_by_base[adjustment_base] = index
            continue

        if "标注记录" in header:
            annotation_columns.append({"index": index, "header": header, "base": grouped_annotation_base_name(header)})

    if any(prompt.get("grouped") for prompt in prompt_columns):
        prompt_columns = [
            prompt
            for prompt in prompt_columns
            if prompt.get("grouped") or normalize_header_key(prompt["header"]) != "peprompt"
        ]
        prompt_bases = {prompt["base"] for prompt in prompt_columns}
        for base, result_index in result_by_base.items():
            if base not in prompt_bases:
                prompt_columns.append(
                    {
                        "index": result_index,
                        "promptIndex": None,
                        "header": base,
                        "base": base,
                        "label": base,
                        "grouped": True,
                        "resultOnly": True,
                    }
                )

    def prompt_display_rank(prompt):
        """特定观测表展示顺序：step100 在 step60 前，人工列保持最后。"""
        header = prompt["header"].lower()
        if "人工" in prompt["header"] or "human" in header:
            return (10, prompt["index"])
        if "step100" in header:
            return (0, prompt["index"])
        if "step60" in header:
            return (1, prompt["index"])
        return (2, prompt["index"])

    prompt_columns = sorted(prompt_columns, key=prompt_display_rank)

    groups = []
    non_human_count = 0
    for position, prompt in enumerate(prompt_columns):
        header = prompt.get("label") or prompt["header"]
        source_header = prompt["header"]
        header_lower = header.lower()
        prompt_index = prompt.get("promptIndex", prompt["index"])
        annotatable = prompt_index is not None and "人工" not in header and "人工" not in source_header and "human" not in header_lower
        annotation_column = ""
        if annotatable:
            if not annotation_column:
                matching = next((item for item in annotation_columns if item.get("base") == prompt["base"] or prompt["base"] in item["header"] or item["header"] in {f"{prompt['base']}标注记录", f"{prompt['base']}-标注记录", f"{prompt['base']}-PE标注记录"}), None)
                if matching:
                    annotation_column = column_letter(matching["index"])
            if not annotation_column and non_human_count < len(annotation_columns):
                annotation_column = column_letter(annotation_columns[non_human_count]["index"])
            non_human_count += 1

        result_index = result_by_base.get(prompt["base"])
        result_header = headers[result_index] if result_index is not None and result_index < len(headers) else ""
        machine_annotation_index = machine_annotation_by_base.get(prompt["base"])
        adjustment_flag_index = adjustment_flag_by_base.get(prompt["base"])
        groups.append(
            {
                "id": f"prompt-{position}",
                "label": header,
                "resultLabel": result_header or f"{header}结果",
                "promptColumn": column_letter(prompt_index) if prompt_index is not None else "",
                "resultColumn": column_letter(result_index) if result_index is not None else "",
                "annotationColumn": annotation_column,
                "machineAnnotationColumn": column_letter(machine_annotation_index) if machine_annotation_index is not None else "",
                "adjustmentColumn": column_letter(adjustment_flag_index) if adjustment_flag_index is not None else "",
                "annotatable": annotatable and bool(annotation_column),
            }
        )

    return {
        "mode": "default",
        "headers": headers,
        "groups": groups,
        "issueGroups": build_issue_groups(headers, [prompt["base"] for prompt in prompt_columns]),
        "byHeader": by_header,
        "summaryColumn": "P",
        "machineColumn": "Q",
    }


AB_EVAL_FEATURE_HEADERS = ["prompt_cn", "模型A", "模型B", "考点1", "考点2", "考点3", "考点4", "考点5"]

# 质检模式自动建列（写在 Prompt 列右侧空列）。
# 仅保留主表当前状态；选项级历史流水写入「质检变更日志」子表。
QC_COLUMNS = ["质检结论", "质检意见", "质检人", "质检时间", "质检阶段", "返工轮次", "质检变更记录"]


def header_matches_any(headers_keyset, name):
    """特征列命中：精确归一匹配，或对“考点N”做前缀模糊匹配。"""
    key = normalize_header_key(name)
    if key in headers_keyset:
        return True
    if key.startswith("考点"):
        return any(hk.startswith(key) for hk in headers_keyset)
    return False


def ab_eval_match(headers):
    keyset = {normalize_header_key(h) for h in headers if h}
    hits = sum(1 for name in AB_EVAL_FEATURE_HEADERS if header_matches_any(keyset, name))
    return hits / len(AB_EVAL_FEATURE_HEADERS)


def build_ab_eval_mapping(headers):
    """A/B 评测模板：prompt_cn 原始输入 + 模型A/B 两图 + 考点1~5 结论 + 备注A/B + 评测人。"""
    def col_of(names, prefix=False):
        keyed = {normalize_header_key(h): i for i, h in enumerate(headers) if h}
        for name in names:
            key = normalize_header_key(name)
            if key in keyed:
                return column_letter(keyed[key])
            if prefix:
                hit = next((i for k, i in keyed.items() if k.startswith(key)), None)
                if hit is not None:
                    return column_letter(hit)
        return ""

    criteria = []
    for index, header in enumerate(headers):
        if normalize_header_key(header).startswith("考点"):
            criteria.append({"id": f"c{len(criteria) + 1}", "title": header, "column": column_letter(index)})

    writable = [item["column"] for item in criteria]
    remark_a = col_of(["评测备注-模型A", "评测备注模型A", "备注-模型A"])
    remark_b = col_of(["评测备注-模型B", "评测备注模型B", "备注-模型B"])
    writable += [c for c in (remark_a, remark_b) if c]

    qc_verdict = col_of(["质检结论"])
    qc_reviewer = col_of(["质检人"])
    qc_time = col_of(["质检时间"])
    qc_stage = col_of(["质检阶段", "qcStage"])
    rework_round = col_of(["返工轮次", "reworkRound"])
    qc_change_log = col_of(["质检变更记录", "质检修改记录", "质检操作日志", "qcChangeLog"])
    qc_comment = col_of(["质检意见"])  # 兼容旧表，非新方案必需列
    qc_checked = col_of(["已检"])
    writable += [c for c in (qc_verdict, qc_reviewer, qc_time, qc_stage, rework_round, qc_change_log, qc_comment, qc_checked) if c]

    return {
        "mode": "ab-eval",
        "headers": headers,
        "promptIdColumn": col_of(["prompt_id"]),
        "promptCnColumn": col_of(["prompt_cn", "原始输入", "原始prompt"]),
        "descriptionColumn": col_of(["description", "说明"]),
        "uploadFlagColumn": col_of(["上传标识"]),
        "modelAColumn": col_of(["模型A"]),
        "modelBColumn": col_of(["模型B"]),
        "criteria": criteria,
        "remarkAColumn": remark_a,
        "remarkBColumn": remark_b,
        "reviewerColumn": col_of(["评测人"]),
        "qcVerdictColumn": qc_verdict,
        "qcCommentColumn": qc_comment,
        "qcReviewerColumn": qc_reviewer,
        "qcTimeColumn": qc_time,
        "qcStageColumn": qc_stage,
        "reworkRoundColumn": rework_round,
        "qcChangeLogColumn": qc_change_log,
        "qcCheckedColumn": qc_checked,
        "writableColumns": writable,
        "verdictOptions": ["模型A", "模型B", "无法区分"],
        "qcVerdictOptions": [qc_sampling.PASS, qc_sampling.FAIL],
        "supportsQC": True,
    }


# 模板注册表：新增模板 = 加一项（feature 命中率 + header_row + build_mapping）+ 前端加一个 render<mode>Detail 分支。
TEMPLATES = [
    {
        "id": "ab-eval",
        "name": "A/B 评测模板",
        "headerRow": 1,
        "match": ab_eval_match,
        "build": build_ab_eval_mapping,
    },
    {
        "id": "pe-observation",
        "name": "PE prompt 观测模板",
        "headerRow": 2,
        "match": lambda headers: 1.0,  # 兜底模板，始终可用
        "build": build_column_mapping,
    },
]

TEMPLATE_MATCH_THRESHOLD = 0.8


def detect_template_for_source(source):
    """对每个专用模板用其声明的表头行算命中率，≥阈值取最高；都不达标回退 PE 观测模板。"""
    headers_cache = {}

    def headers_at(row):
        if row not in headers_cache:
            headers_cache[row] = read_headers(source, header_row=row)
        return headers_cache[row]

    best = None
    best_score = TEMPLATE_MATCH_THRESHOLD - 1e-9
    for template in TEMPLATES:
        if template["id"] == "pe-observation":
            continue
        score = template["match"](headers_at(template["headerRow"]))
        if score > best_score:
            best = template
            best_score = score
    if best:
        return best, headers_at(best["headerRow"])
    fallback = next(t for t in TEMPLATES if t["id"] == "pe-observation")
    return fallback, headers_at(fallback["headerRow"])


def value_at(padded, index):
    if index is None or index >= len(padded):
        return ""
    return cell_to_text(padded[index]).strip()


def first_header_value(values_by_header, padded, names, fallback_index=None):
    for name in names:
        value = values_by_header.get(name, "")
        if value:
            return value
    return value_at(padded, fallback_index)


def first_header_value_by_name(values_by_header, values_by_key, padded, names, fallback_index=None):
    """按第二行列名取值：先精确匹配，再做去空格/下划线/冒号后的标准化匹配。"""
    for name in names:
        if name in values_by_header:
            return values_by_header[name]
    for name in names:
        key = normalize_header_key(name)
        if key in values_by_key:
            return values_by_key[key]
    return value_at(padded, fallback_index)


def header_column_by_name(headers, names, fallback_column):
    exact = {header: index for index, header in enumerate(headers) if header}
    keyed = {normalize_header_key(header): index for index, header in enumerate(headers) if header}
    for name in names:
        if name in exact:
            return column_letter(exact[name])
    for name in names:
        key = normalize_header_key(name)
        if key in keyed:
            return column_letter(keyed[key])
    return fallback_column


def read_headers(source, header_row=2):
    if source_type(source) == "base":
        return read_base_field_names(source)
    header_result = run_lark(
        [
            "sheets",
            "+cells-get",
            "--url",
            source["url"],
            "--sheet-id",
            source["sheetId"],
            "--range",
            f"A{header_row}:{MAX_READ_COLUMN}{header_row}",
        ]
    )
    return [normalize_header(value) for value in ((cells_get_values(header_result) or [[]])[0] or [])]


def read_base_field_names(source):
    """多维表格用字段名当作表头，保持创建顺序，使列映射引擎可直接复用。"""
    names = []
    offset = 0
    while True:
        result = run_lark(
            [
                "base",
                "+field-list",
                "--base-token",
                source["baseToken"],
                "--table-id",
                source["tableId"],
                "--format",
                "json",
                "--limit",
                "100",
                "--offset",
                str(offset),
            ]
        )
        items = list_from_payload(result, "items", "fields")
        if not items:
            break
        for item in items:
            names.append(normalize_header(first_field(item, ["field_name", "name", "title"])))
        if len(items) < 100:
            break
        offset += len(items)
    return names


def read_base_records(source):
    """自动翻页读取多维表格记录，返回 [(record_id, fields_dict)]。"""
    records = []
    offset = 0
    view_args = ["--view-id", source["viewId"]] if source.get("viewId") else []
    while True:
        result = run_lark(
            [
                "base",
                "+record-list",
                "--base-token",
                source["baseToken"],
                "--table-id",
                source["tableId"],
                *view_args,
                "--format",
                "json",
                "--limit",
                str(BASE_RECORD_PAGE_SIZE),
                "--offset",
                str(offset),
            ]
        )
        items = list_from_payload(result, "items", "records")
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            record_id = first_field(item, ["record_id", "recordId", "id"])
            fields = item.get("fields")
            records.append((record_id, fields if isinstance(fields, dict) else {}))
        if len(items) < BASE_RECORD_PAGE_SIZE:
            break
        offset += len(items)
    return records


def iter_source_cells(source, headers, data_start_row=3):
    """统一产出 (row_key, cells)：sheet 为 (excel_row, list)，base 为 (record_id, list)。"""
    if source_type(source) == "base":
        for record_id, fields in read_base_records(source):
            cells = [fields.get(header, "") for header in headers]
            yield record_id, cells
        return
    for excel_row, cells in read_data_rows(source, start_row=data_start_row):
        yield excel_row, cells


def read_data_rows(source, start_row=3, end_row=None, chunk_size=READ_ROW_CHUNK_SIZE):
    """分块读取数据行，规避飞书单次读取 10MB 限制；返回 (excel_row, cells)。"""
    end_row = end_row or source["rowCount"]
    if start_row > end_row:
        return []
    rows = []
    current = start_row
    while current <= end_row:
        chunk_end = min(end_row, current + chunk_size - 1)
        data_range = f"A{current}:{MAX_READ_COLUMN}{chunk_end}"
        try:
            result = run_lark(
                [
                    "sheets",
                    "+cells-get",
                    "--url",
                    source["url"],
                    "--sheet-id",
                    source["sheetId"],
                    "--range",
                    data_range,
                ]
            )
        except RuntimeError as exc:
            if "90221" in str(exc) and current < chunk_end:
                mid = (current + chunk_end) // 2
                rows.extend(read_data_rows(source, current, mid, max(1, chunk_size // 2)))
                rows.extend(read_data_rows(source, mid + 1, chunk_end, max(1, chunk_size // 2)))
                current = chunk_end + 1
                continue
            raise
        values = cells_get_values(result)
        for offset, cells in enumerate(values):
            rows.append((current + offset, cells))
        current = chunk_end + 1
    return rows


def read_rows():
    source = active_source()
    template, headers = detect_template_for_source(source)
    mapping = template["build"](headers)
    data_start_row = template["headerRow"] + 1
    if mapping.get("mode") == "ab-eval":
        return read_ab_eval_rows(source, headers, mapping, data_start_row)
    return read_pe_rows(source, headers, mapping, data_start_row)


def read_rows_cached(force_refresh=False):
    source = active_source()
    cache_key = cache_source_key(source)
    now = time.time()
    if not force_refresh:
        with _CACHE_LOCK:
            cached = _ROWS_CACHE.get(cache_key)
            if cached and cached["expires_at"] > now:
                return cached["payload"], True

    payload = read_rows()
    with _CACHE_LOCK:
        _ROWS_CACHE[cache_key] = {"expires_at": now + ROWS_CACHE_TTL_SECONDS, "payload": payload}
    return payload, False


def read_ab_eval_rows(source, headers, mapping, data_start_row):
    is_base = source_type(source) == "base"
    rows = []
    for offset, cells in iter_source_cells(source, headers, data_start_row=data_start_row):
        padded = list(cells) + [""] * (len(headers) - len(cells))
        if not any(cell_to_text(c).strip() for c in padded):
            continue
        criteria = [
            {**item, "verdict": value_at(padded, column_index(item["column"]))}
            for item in mapping["criteria"]
        ]

        def cell_raw(column):
            index = column_index(column)
            if index is None or index >= len(padded):
                return None
            return padded[index]

        model_a_raw = cell_raw(mapping["modelAColumn"])
        model_b_raw = cell_raw(mapping["modelBColumn"])
        rows.append(
            {
                "id": f"base-{offset}" if is_base else f"excel-row-{offset}",
                "excelRow": offset,
                "recordId": offset if is_base else "",
                "sourceType": "base" if is_base else "sheet",
                "mode": "ab-eval",
                "templateId": "ab-eval",
                "promptId": value_at(padded, column_index(mapping["promptIdColumn"])),
                "c": value_at(padded, column_index(mapping["promptCnColumn"])),
                "description": value_at(padded, column_index(mapping["descriptionColumn"])),
                "uploadFlag": value_at(padded, column_index(mapping["uploadFlagColumn"])),
                "modelA": cell_to_text(model_a_raw).strip(),
                "modelB": cell_to_text(model_b_raw).strip(),
                "modelAImages": cell_image_tokens(model_a_raw),
                "modelBImages": cell_image_tokens(model_b_raw),
                "promptGroups": [
                    {
                        "id": "modelA",
                        "label": "模型A",
                        "prompt": "",
                        "result": cell_to_text(model_a_raw).strip(),
                        "resultLabel": "模型A 作业",
                        "imageTokens": cell_image_tokens(model_a_raw),
                        "annotatable": False,
                    },
                    {
                        "id": "modelB",
                        "label": "模型B",
                        "prompt": "",
                        "result": cell_to_text(model_b_raw).strip(),
                        "resultLabel": "模型B 作业",
                        "imageTokens": cell_image_tokens(model_b_raw),
                        "annotatable": False,
                    },
                ],
                "criteria": criteria,
                "remarkA": value_at(padded, column_index(mapping["remarkAColumn"])),
                "remarkB": value_at(padded, column_index(mapping["remarkBColumn"])),
                "remarkColumnA": mapping["remarkAColumn"],
                "remarkColumnB": mapping["remarkBColumn"],
                "verdictOptions": mapping["verdictOptions"],
                "reviewer": value_at(padded, column_index(mapping["reviewerColumn"])),
                "qcVerdict": value_at(padded, column_index(mapping.get("qcVerdictColumn", ""))),
                "qcComment": value_at(padded, column_index(mapping.get("qcCommentColumn", ""))),
                "qcReviewer": value_at(padded, column_index(mapping.get("qcReviewerColumn", ""))),
                "qcTime": value_at(padded, column_index(mapping.get("qcTimeColumn", ""))),
                "qcStage": value_at(padded, column_index(mapping.get("qcStageColumn", ""))),
                "reworkRound": value_at(padded, column_index(mapping.get("reworkRoundColumn", ""))),
                "qcChangeLog": value_at(padded, column_index(mapping.get("qcChangeLogColumn", ""))),
                "qcChecked": bool(
                    value_at(padded, column_index(mapping.get("qcVerdictColumn", "")))
                    or value_at(padded, column_index(mapping.get("qcCheckedColumn", "")))
                ),
                "qcVerdictColumn": mapping.get("qcVerdictColumn", ""),
                "qcCommentColumn": mapping.get("qcCommentColumn", ""),
                "qcReviewerColumn": mapping.get("qcReviewerColumn", ""),
                "qcTimeColumn": mapping.get("qcTimeColumn", ""),
                "qcStageColumn": mapping.get("qcStageColumn", ""),
                "reworkRoundColumn": mapping.get("reworkRoundColumn", ""),
                "qcChangeLogColumn": mapping.get("qcChangeLogColumn", ""),
                "qcCheckedColumn": mapping.get("qcCheckedColumn", ""),
                "qcVerdictOptions": mapping.get("qcVerdictOptions", []),
            }
        )
    return {"rows": rows, "columnMapping": mapping}


def read_pe_rows(source, headers, mapping, data_start_row):
    is_base = source_type(source) == "base"
    rows = []
    for offset, cells in iter_source_cells(source, headers, data_start_row=data_start_row):
        padded = list(cells) + [""] * (len(headers) - len(cells))
        text_values = [cell_to_text(padded[index]).strip() for index in range(2, len(headers))]
        if not any(text_values):
            continue
        values_by_header = {}
        values_by_key = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            value = cell_to_text(padded[index]).strip()
            if header not in values_by_header:
                values_by_header[header] = value
            header_key = normalize_header_key(header)
            if header_key not in values_by_key:
                values_by_key[header_key] = value
        prompt_groups = []
        for group in mapping["groups"]:
            prompt_index = column_index(group["promptColumn"])
            result_index = column_index(group["resultColumn"])
            annotation_index = column_index(group["annotationColumn"])
            machine_annotation_index = column_index(group.get("machineAnnotationColumn", ""))
            adjustment_index = column_index(group.get("adjustmentColumn", ""))
            prompt_groups.append(
                {
                    **group,
                    "prompt": cell_to_text(padded[prompt_index]).strip() if prompt_index is not None and prompt_index < len(padded) else "",
                    "result": cell_to_text(padded[result_index]).strip() if result_index is not None and result_index < len(padded) else "",
                    "annotations": cell_to_text(padded[annotation_index]).strip() if annotation_index is not None and annotation_index < len(padded) else "",
                    "machineAnnotations": cell_to_text(padded[machine_annotation_index]).strip() if machine_annotation_index is not None and machine_annotation_index < len(padded) else "",
                    "adjustmentFlag": cell_to_text(padded[adjustment_index]).strip() if adjustment_index is not None and adjustment_index < len(padded) else "",
                }
            )
        issue_groups = []
        for group in mapping.get("issueGroups", []):
            label_index = column_index(group["labelColumn"])
            image_label_index = column_index(group.get("imageLabelColumn", ""))
            summary_index = column_index(group["summaryColumn"])
            issue_groups.append(
                {
                    **group,
                    "labels": cell_to_text(padded[label_index]).strip() if label_index is not None and label_index < len(padded) else "",
                    "imageLabels": cell_to_text(padded[image_label_index]).strip() if image_label_index is not None and image_label_index < len(padded) else "",
                    "summary": cell_to_text(padded[summary_index]).strip() if summary_index is not None and summary_index < len(padded) else "",
                }
            )
        rows.append(
            {
                "id": f"base-{offset}" if is_base else f"excel-row-{offset}",
                "excelRow": offset,
                "recordId": offset if is_base else "",
                "sourceType": "base" if is_base else "sheet",
                "mode": mapping.get("mode", "default"),
                "promptId": first_header_value_by_name(values_by_header, values_by_key, padded, ["prompt_id", "promptid", "Prompt ID"], 0),
                "c": first_header_value_by_name(values_by_header, values_by_key, padded, ["prompt_cn", "原始输入", "原始prompt"], 2),
                "d": prompt_groups[0]["prompt"] if prompt_groups else "",
                "e": prompt_groups[1]["prompt"] if len(prompt_groups) > 1 else "",
                "f": first_header_value_by_name(values_by_header, values_by_key, padded, ["标签", "tag"], 5),
                "i": prompt_groups[1]["result"] if len(prompt_groups) > 1 else "",
                "j": prompt_groups[0]["result"] if prompt_groups else "",
                "k": first_header_value_by_name(values_by_header, values_by_key, padded, ["整体表现", "整体表现维度", "整体表现评测结果"], 10),
                "l": first_header_value_by_name(values_by_header, values_by_key, padded, ["指令遵循", "指令遵循维度", "指令遵循评测结果", "指令遵循维度评测结果"], 11),
                "m": first_header_value_by_name(values_by_header, values_by_key, padded, ["一致性", "一致性维度", "一致性评测结果", "一致性维度评测结果"], 12),
                "n": first_header_value_by_name(values_by_header, values_by_key, padded, ["美感", "美感维度", "美感评测结果"], 13),
                "o": first_header_value_by_name(values_by_header, values_by_key, padded, ["结构", "结构维度", "结构评测结果"], 14),
                "p": first_header_value_by_name(values_by_header, values_by_key, padded, ["PE问题总结", "PE 问题总结"], 15),
                "q": first_header_value_by_name(values_by_header, values_by_key, padded, ["机器标注"], 16),
                "r": prompt_groups[0]["annotations"] if prompt_groups else "",
                "promptGroups": prompt_groups,
                "issueGroups": issue_groups,
            }
        )
    return {"rows": rows, "columnMapping": mapping}


def first_url(value):
    match = URL_RE.search(cell_to_text(value) or "")
    return match.group(0) if match else ""


def image_formula(url):
    """将图片链接写成飞书表格可渲染的 IMAGE 公式。"""
    return {"type": "formula", "text": f'=IMAGE("{str(url).replace(chr(34), chr(34) + chr(34))}")'}


def spreadsheet_token_from_url(url):
    """从飞书表格链接中提取 spreadsheet token，用于原生图片接口。"""
    return urlparse(url).path.rstrip("/").split("/")[-1]


def prepare_cell_image(url):
    """下载并压缩图片，输出 values_image 接口需要的字节数组。"""
    if not url:
        return None
    with tempfile.TemporaryDirectory(prefix="pe-cell-image-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        source_path = tmp_path / "source.img"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 PE Prompt Reviewer"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            source_path.write_bytes(response.read())

        for max_size in (900, 720, 520, 360):
            output_path = tmp_path / f"cell-{max_size}.jpg"
            subprocess.run(
                [
                    "/usr/bin/sips",
                    "-Z",
                    str(max_size),
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    "70",
                    str(source_path),
                    "--out",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            data = output_path.read_bytes()
            if len(data) <= 180_000:
                return list(data)
    raise ValueError("图片压缩后仍然过大，无法写入飞书单元格图片")


def write_cell_image(column, row_number, url):
    """将图片以单元格图片形式写入指定位置，触发于空间关系汇总表生成后。"""
    if not url:
        return {"ok": False, "reason": "empty_url"}
    image_bytes = prepare_cell_image(url)
    payload = {
        "range": f"{SPATIAL_TARGET_SHEET_ID}!{column}{row_number}:{column}{row_number}",
        "image": image_bytes,
        "name": f"spatial-{column}{row_number}.jpg",
    }
    run_lark(
        [
            "api",
            "POST",
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token_from_url(SPATIAL_TARGET_URL)}/values_image",
            "--data",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ]
    )
    return {"ok": True, "bytes": len(image_bytes)}


def issue_label_groups(headers):
    """从第二行表头中找出 prompt-*-问题标签，并返回对应 prompt base。"""
    groups = []
    for index, header in enumerate(headers):
        title = normalize_header(header)
        lower = title.lower()
        if lower.startswith("prompt-") and lower.endswith("-问题标签"):
            groups.append({"base": title[: -len("-问题标签")], "labelIndex": index, "labelTitle": title})
    return groups


def result_index_for_base(headers, base):
    target = f"{base}-结果"
    for index, header in enumerate(headers):
        if normalize_header(header) == target:
            return index
    base_key = normalize_header_key(base)
    for index, header in enumerate(headers):
        title = normalize_header(header)
        if title.lower().startswith("prompt-") and title.lower().endswith("-结果") and normalize_header_key(title).startswith(base_key):
            return index
    return None


def fallback_result_index(headers):
    for index, header in enumerate(headers):
        title = normalize_header(header)
        if title.lower().startswith("prompt-") and title.lower().endswith("-结果") and "人工" not in title:
            return index
    for index, header in enumerate(headers):
        if normalize_header(header).endswith("结果"):
            return index
    return None


def human_result_index(headers):
    """定位人工 Prompt 的生成结果列，用于空间关系汇总表里的 GT 图。"""
    preferred = ["prompt-人工-结果", "人工prompt结果", "人工 Prompt 结果", "人工Prompt生成结果"]
    for name in preferred:
        for index, header in enumerate(headers):
            if normalize_header_key(header) == normalize_header_key(name):
                return index
    for index, header in enumerate(headers):
        title = normalize_header(header)
        if "人工" in title and title.endswith("结果"):
            return index
    return None


def collect_spatial_relation_cases():
    """遍历所有配置表，收集问题标签/总结中包含空间关系的结果图。"""
    config = load_source_config()
    records = []
    debug = []
    for source in config["sources"]:
        if source_type(source) == "base":
            debug.append({"title": source.get("title"), "matched": 0, "skipped": "base 数据源暂不支持空间关系汇总"})
            continue
        row_count = int(source.get("rowCount") or DEFAULT_ROW_COUNT)
        result = run_lark(
            [
                "sheets",
                "+cells-get",
                "--url",
                source["url"],
                "--sheet-id",
                source["sheetId"],
                "--range",
                f"A2:{MAX_READ_COLUMN}{row_count}",
            ]
        )
        values = cells_get_values(result)
        if not values:
            debug.append({"title": source.get("title"), "matched": 0})
            continue
        headers = [normalize_header(item) for item in values[0]]
        groups = issue_label_groups(headers)
        gt_index = human_result_index(headers)
        legacy_indexes = [
            index
            for index, header in enumerate(headers)
            if normalize_header_key(header) in {"pe问题总结", "pe问题标签", "问题标签", "历史问题标签"}
        ]
        matched = 0
        for cells in values[1:]:
            padded = list(cells) + [""] * (len(headers) - len(cells))
            text_values = [cell_to_text(padded[index]).strip() for index in range(2, len(headers))]
            if not any(text_values):
                continue
            values_by_header = {}
            values_by_key = {}
            for index, header in enumerate(headers):
                if not header:
                    continue
                value = cell_to_text(padded[index]).strip()
                values_by_header.setdefault(header, value)
                values_by_key.setdefault(normalize_header_key(header), value)
            prompt_id = first_header_value_by_name(values_by_header, values_by_key, padded, ["prompt_id", "promptid", "Prompt ID"], 0)
            prompt_cn = first_header_value_by_name(values_by_header, values_by_key, padded, ["prompt_cn", "原始输入", "原始prompt"], 2)
            gt_url = first_url(padded[gt_index]) if gt_index is not None and gt_index < len(padded) else ""
            emitted = False
            for group in groups:
                labels = cell_to_text(padded[group["labelIndex"]]).strip() if group["labelIndex"] < len(padded) else ""
                if "空间关系" not in labels:
                    continue
                result_index = result_index_for_base(headers, group["base"])
                url = first_url(padded[result_index]) if result_index is not None and result_index < len(padded) else ""
                if not url:
                    continue
                prompt_index = next((index for index, header in enumerate(headers) if normalize_header(header) == group["base"]), None)
                prompt_text = cell_to_text(padded[prompt_index]).strip() if prompt_index is not None and prompt_index < len(padded) else ""
                records.append({"promptid": prompt_id, "prompt_cn": prompt_cn, "pe_prompt": prompt_text, "model": group["base"], "result_url": url, "gt_url": gt_url})
                matched += 1
                emitted = True
            if not emitted and legacy_indexes:
                legacy_text = "；".join(cell_to_text(padded[index]).strip() for index in legacy_indexes if index < len(padded))
                if "空间关系" in legacy_text:
                    result_index = fallback_result_index(headers)
                    url = first_url(padded[result_index]) if result_index is not None and result_index < len(padded) else ""
                    if url:
                        model = headers[result_index].replace("-结果", "") if result_index is not None else ""
                        prompt_index = next((index for index, header in enumerate(headers) if normalize_header(header) == model), None)
                        prompt_text = cell_to_text(padded[prompt_index]).strip() if prompt_index is not None and prompt_index < len(padded) else ""
                        records.append({"promptid": prompt_id, "prompt_cn": prompt_cn, "pe_prompt": prompt_text, "model": model, "result_url": url, "gt_url": gt_url})
                        matched += 1
        debug.append({"title": source.get("title"), "matched": matched})
    return records, debug


def write_spatial_relation_cases():
    """覆盖目标表 A:F，并把 E/F 结果图写成飞书单元格图片。"""
    records, debug = collect_spatial_relation_cases()
    row_count = max(200, len(records) + 1)
    values = [["promptid", "原始prompt", "PE prompt", "模型名称", "prompt结果", "GT图"]]
    values.extend([
        [
            record["promptid"],
            record["prompt_cn"],
            record.get("pe_prompt", ""),
            record.get("model", ""),
            "",
            "",
        ]
        for record in records
    ])
    while len(values) < row_count:
        values.append(["", "", "", "", "", ""])
    run_lark(
        [
            "sheets",
            "+cells-set",
            "--url",
            SPATIAL_TARGET_URL,
            "--sheet-id",
            SPATIAL_TARGET_SHEET_ID,
            "--range",
            f"A1:F{row_count}",
            "--cells",
            json.dumps(cells_payload(values), ensure_ascii=False),
        ]
    )
    # 调整列宽和行高，让单元格图片形成稳定的大图预览。
    for payload in [
        {
            "dimension": {"sheetId": SPATIAL_TARGET_SHEET_ID, "majorDimension": "COLUMNS", "startIndex": 5, "endIndex": 5},
            "dimensionProperties": {"visible": True, "fixedSize": 420},
        },
        {
            "dimension": {"sheetId": SPATIAL_TARGET_SHEET_ID, "majorDimension": "COLUMNS", "startIndex": 6, "endIndex": 6},
            "dimensionProperties": {"visible": True, "fixedSize": 420},
        },
        {
            "dimension": {"sheetId": SPATIAL_TARGET_SHEET_ID, "majorDimension": "ROWS", "startIndex": 2, "endIndex": row_count},
            "dimensionProperties": {"visible": True, "fixedSize": 260},
        },
    ]:
        run_lark(
            [
                "api",
                "PUT",
                f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token_from_url(SPATIAL_TARGET_URL)}/dimension_range",
                "--data",
                json.dumps(payload, ensure_ascii=False),
            ]
        )

    images_written = 0
    image_errors = []
    for row_number, record in enumerate(records, start=2):
        for column, key in (("E", "result_url"), ("F", "gt_url")):
            try:
                result = write_cell_image(column, row_number, record.get(key, ""))
                if result.get("ok"):
                    images_written += 1
            except Exception as exc:
                image_errors.append(
                    {
                        "row": row_number,
                        "column": column,
                        "promptid": record.get("promptid", ""),
                        "error": str(exc)[:300],
                    }
                )
    return {"count": len(records), "imagesWritten": images_written, "imageErrors": image_errors, "debug": debug}


def write_source_cell(source, row_key, column, value, headers=None):
    """统一写单元格：sheet 按列字母+行号写，base 把列字母映射回字段名后 upsert。"""
    if source_type(source) == "base":
        names = headers if headers is not None else read_headers(source)
        index = column_index(column)
        if index is None or index >= len(names) or not names[index]:
            raise ValueError(f"列 {column} 在多维表格中无对应字段，无法写入")
        run_lark(
            [
                "base",
                "+record-upsert",
                "--base-token",
                source["baseToken"],
                "--table-id",
                source["tableId"],
                "--record-id",
                str(row_key),
                "--json",
                json.dumps({names[index]: value}, ensure_ascii=False),
            ]
        )
        return
    run_lark(
        [
            "sheets",
            "+cells-set",
            "--url",
            source["url"],
            "--sheet-id",
            source["sheetId"],
            "--range",
            f"{column}{row_key}:{column}{row_key}",
            "--cells",
            json.dumps(cells_payload([[value]]), ensure_ascii=False),
        ]
    )


def write_sheet_cells(source, row_key, cell_values):
    """Batch-write several cells on the same sheet row with one lark-cli call."""
    if not cell_values:
        return
    if source_type(source) == "base":
        headers = read_headers(source)
        fields = {}
        for column, value in cell_values:
            index = column_index(column)
            if index is None or index >= len(headers) or not headers[index]:
                raise ValueError(f"列 {column} 在多维表格中无对应字段，无法写入")
            fields[headers[index]] = value
        run_lark(
            [
                "base",
                "+record-upsert",
                "--base-token",
                source["baseToken"],
                "--table-id",
                source["tableId"],
                "--record-id",
                str(row_key),
                "--json",
                json.dumps(fields, ensure_ascii=False),
            ]
        )
        return

    indexes = [column_index(column) for column, _ in cell_values]
    if any(index is None for index in indexes):
        raise ValueError("写入列不能为空")
    min_index = min(indexes)
    max_index = max(indexes)
    # Only batch a dense range. If someone manually placed QC columns apart, avoid
    # overwriting unrelated cells between them with blanks.
    if sorted(indexes) != list(range(min_index, max_index + 1)):
        for column, value in cell_values:
            write_source_cell(source, row_key, column, value)
        return
    values = [["" for _ in range(max_index - min_index + 1)]]
    for column, value in cell_values:
        values[0][column_index(column) - min_index] = value
    start_col = column_letter(min_index)
    end_col = column_letter(max_index)
    run_lark(
        [
            "sheets",
            "+cells-set",
            "--url",
            source["url"],
            "--sheet-id",
            source["sheetId"],
            "--range",
            f"{start_col}{row_key}:{end_col}{row_key}",
            "--cells",
            json.dumps(cells_payload(values), ensure_ascii=False),
        ]
    )


def write_tag(row_number, tags, source=None):
    """将 PE 问题总结写回第二行列名为 PE问题总结 的列；旧表兜底 P 列。"""
    source = source or active_source()
    headers = read_headers(source)
    target_column = header_column_by_name(headers, ["PE问题总结", "PE 问题总结"], "P")
    normalized = []
    for tag in tags:
        tag = str(tag).strip()
        if tag and tag not in normalized:
            normalized.append(tag)

    value = "；".join(normalized)
    write_source_cell(source, row_number, target_column, value, headers=headers)
    return value


def read_taxonomy_sheet(sheet_id, include_category_without_label=False):
    """读取标签体系表 A/B 两列；i2i 单列标签表可将 A 列直接作为标签。"""
    result = run_lark(
        [
            "sheets",
            "+cells-get",
            "--url",
            LABEL_TAXONOMY_URL,
            "--sheet-id",
            sheet_id,
            "--range",
            f"{sheet_id}!A1:B200",
        ]
    )
    values = cells_get_values(result)
    options = []
    current_category = ""
    for row in values[1:]:
        category = cell_to_text(row[0]).strip() if len(row) > 0 else ""
        label = cell_to_text(row[1]).strip() if len(row) > 1 else ""
        if category:
            current_category = category
        if current_category and label:
            value = label if current_category == label else f"{current_category}-{label}"
            if value not in options:
                options.append(value)
        elif include_category_without_label and category:
            if category not in options:
                options.append(category)
    return options


def read_label_taxonomy():
    """读取图像效果标签与 PE 问题标签体系，用于前端只读选择。"""
    t2i_image = read_taxonomy_sheet(IMAGE_LABEL_SHEET_ID)
    t2i_pe = read_taxonomy_sheet(PE_LABEL_SHEET_ID)
    i2i_image = read_taxonomy_sheet(I2I_IMAGE_LABEL_SHEET_ID, include_category_without_label=True)
    i2i_pe = read_taxonomy_sheet(I2I_PE_LABEL_SHEET_ID, include_category_without_label=True)
    return {
        "imageEffectLabels": t2i_image,
        "peProblemLabels": t2i_pe,
        "i2iImageEffectLabels": i2i_image,
        "i2iPeProblemLabels": i2i_pe,
        "taxonomies": {
            "t2i": {"imageEffectLabels": t2i_image, "peProblemLabels": t2i_pe},
            "i2i": {"imageEffectLabels": i2i_image, "peProblemLabels": i2i_pe},
        },
    }


def read_label_taxonomy_cached(force_refresh=False):
    now = time.time()
    if not force_refresh:
        with _CACHE_LOCK:
            if _LABEL_TAXONOMY_CACHE["payload"] is not None and _LABEL_TAXONOMY_CACHE["expires_at"] > now:
                return _LABEL_TAXONOMY_CACHE["payload"], True
    payload = read_label_taxonomy()
    with _CACHE_LOCK:
        _LABEL_TAXONOMY_CACHE["payload"] = payload
        _LABEL_TAXONOMY_CACHE["expires_at"] = now + LABEL_TAXONOMY_CACHE_TTL_SECONDS
    return payload, False


def write_issue(
    row_number,
    label_column,
    summary_column,
    labels,
    summary,
    write_labels=True,
    write_summary=True,
    source=None,
    image_label_column="",
    image_labels=None,
    write_image_labels=True,
):
    """将单个问题分析栏的图像效果标签、PE问题标签和问题分析分别写回对应列。"""
    source = source or active_source()
    headers = read_headers(source)
    normalized_labels = []
    for label in labels:
        label = str(label).strip()
        if label and label not in normalized_labels:
            normalized_labels.append(label)
    normalized_image_labels = []
    for label in image_labels or []:
        label = str(label).strip()
        if label and label not in normalized_image_labels:
            normalized_image_labels.append(label)

    writes = []
    if write_image_labels and image_label_column:
        writes.append((image_label_column, "；".join(normalized_image_labels)))
    if write_labels and label_column:
        writes.append((label_column, "；".join(normalized_labels)))
    if write_summary and summary_column:
        writes.append((summary_column, str(summary or "").strip()))

    for column, value in writes:
        write_source_cell(source, row_number, column, value, headers=headers)
    return {
        "imageLabels": "；".join(normalized_image_labels),
        "labels": "；".join(normalized_labels),
        "summary": str(summary or "").strip(),
    }


def write_annotations(row_number, annotations_json, annotation_column="R", source=None):
    """将某个 prompt 的划线备注 JSON 整体覆盖写回对应 PE 标注记录列。"""
    source = source or active_source()
    headers = read_headers(source)
    target_index = column_index(annotation_column)
    target_header = headers[target_index] if target_index is not None and target_index < len(headers) else ""
    if "标注记录" not in target_header:
        raise ValueError(f"{annotation_column} 列不是标注记录列，禁止写入划线备注")
    if not isinstance(annotations_json, str) or not annotations_json.strip():
        value = ""
    else:
        parsed = json.loads(annotations_json)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("annotations"), list):
            raise ValueError("PE 标注记录必须是包含 annotations 数组的 JSON")
        normalized = {"annotations": parsed["annotations"]}
        check_value = str(parsed.get("check", "")).strip().replace("：", ":").lower()
        if check_value in {"0", "check:0"}:
            normalized["check"] = 0
        if check_value in {"1", "check:1"} or parsed.get("checked") in {1, True}:
            normalized["checked"] = 1
        value = "" if not normalized["annotations"] and "check" not in normalized and "checked" not in normalized else json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

    write_source_cell(source, row_number, annotation_column, value, headers=headers)
    return value


def write_ab_eval(row_number, column, value, source=None):
    """A/B 评测模板写回：仅允许写入模板声明的可写列（考点结论 + 备注）。"""
    source = source or active_source()
    cached_mapping, rows = _cached_ab_rows_payload(source)
    headers, mapping, _, _ = _ab_eval_context(source)
    if cached_mapping:
        mapping = {**mapping, **cached_mapping}
    if mapping.get("mode") != "ab-eval":
        raise ValueError("当前数据源不是 A/B 评测模板，禁止写入")
    column = str(column or "").upper()
    if column not in set(mapping.get("writableColumns", [])):
        raise ValueError(f"列 {column} 不在 A/B 评测模板的可写列内，禁止写入")

    existing = ""
    row = None
    try:
        row = _find_row_in_list(rows, row_number)
        if not row:
            _, fresh_mapping, fresh_rows = _ab_eval_rows_payload(source)
            cached_mapping, rows = fresh_mapping, fresh_rows
            row = _find_row_in_list(rows, row_number)
        if row:
            criterion = next((item for item in row.get("criteria") or [] if item.get("column") == column), None)
            if criterion:
                existing = criterion.get("verdict") or ""
            elif column == row.get("remarkColumnA"):
                existing = row.get("remarkA") or ""
            elif column == row.get("remarkColumnB"):
                existing = row.get("remarkB") or ""
        if not row:
            raw_rows = read_data_rows(source, int(row_number), int(row_number), 1)
            cells = raw_rows[0][1] if raw_rows else []
            padded = list(cells) + [""] * (len(headers) - len(cells))
            existing = value_at(padded, column_index(column))
            row = {"excelRow": row_number, "promptId": value_at(padded, column_index(mapping.get("promptIdColumn", ""))), "reviewer": value_at(padded, column_index(mapping.get("reviewerColumn", "")))}
    except Exception:
        row = row or {"excelRow": row_number}

    write_source_cell(source, row_number, column, str(value or ""), headers=headers)
    new_value = str(value or "")
    if str(existing or "") != new_value:
        title = next((item.get("title") for item in mapping.get("criteria") or [] if item.get("column") == column), "")
        if not title and column == mapping.get("remarkAColumn"):
            title = "评测备注-模型A"
        if not title and column == mapping.get("remarkBColumn"):
            title = "评测备注-模型B"
        append_qc_change_log_rows(source, [option_change_row(
            int(time.time() * 1000),
            "system",
            "update",
            row or {"excelRow": row_number},
            title or column,
            column,
            existing,
            new_value,
            "ab-eval",
        )])
        if row and "criteria" in row:
            item = next((it for it in row.get("criteria") or [] if it.get("column") == column), None)
            if item:
                item["verdict"] = new_value
            elif column == row.get("remarkColumnA"):
                row["remarkA"] = new_value
            elif column == row.get("remarkColumnB"):
                row["remarkB"] = new_value
    return str(value or "")


def _ab_eval_context(source):
    """取 A/B 模板的 (headers, mapping, data_start_row)，非 A/B 源报错。"""
    template, headers = detect_template_for_source(source)
    if template["id"] != "ab-eval":
        raise ValueError("当前数据源不是 A/B 评测模板，质检功能不可用")
    mapping = template["build"](headers)
    return headers, mapping, template["headerRow"] + 1, template["headerRow"]


def ensure_ab_qc_columns(source):
    """质检列若不存在则在 Prompt 列右侧依次补列名；返回最新 (headers, mapping)。"""
    if source_type(source) == "base":
        raise ValueError("多维表格暂不支持质检自动建列，请使用 /sheets/ A/B 评测表")
    headers, mapping, _, header_row = _ab_eval_context(source)
    missing = [name for name in QC_COLUMNS if not mapping.get(_qc_mapping_key(name))]
    if not missing:
        return headers, mapping
    existing = {normalize_header_key(h) for h in headers if h}
    prompt_index = column_index(mapping.get("promptCnColumn", ""))
    last_named = max((i for i, h in enumerate(headers) if h and h.strip()), default=-1)
    next_index = (prompt_index + 1) if prompt_index is not None else (last_named + 1)
    with _WRITE_LOCK:
        for name in QC_COLUMNS:
            if normalize_header_key(name) in existing:
                continue
            while next_index < len(headers) and headers[next_index] and headers[next_index].strip():
                next_index += 1
            col = column_letter(next_index)
            write_source_cell(source, header_row, col, name, headers=headers)
            if next_index < len(headers):
                headers[next_index] = name
            next_index += 1
        clear_runtime_caches(source)
    # 重新读取表头与映射
    headers = read_headers(source, header_row=header_row)
    mapping = build_ab_eval_mapping(headers)
    return headers, mapping


def _qc_key(column_name):
    """质检列名 → mapping 字段后缀。"""
    return {
        "质检结论": "Verdict",
        "质检意见": "Comment",
        "质检人": "Reviewer",
        "质检时间": "Time",
        "质检阶段": "Stage",
        "返工轮次": "ReworkRound",
        "质检变更记录": "ChangeLog",
        "已检": "Checked",
    }[column_name]


def _qc_mapping_key(column_name):
    if column_name == "返工轮次":
        return "reworkRoundColumn"
    return f"qc{_qc_key(column_name)}Column"


def _qc_change_log_entry(action, row, reviewer, verdict=None, comment=None, timestamp=None, extra=None):
    """Build one append-only QC operation record for the sheet cell log."""
    return {
        "time": str(timestamp or int(time.time() * 1000)),
        "action": action,
        "operator": str(reviewer or "").strip(),
        "promptId": row.get("promptId") or row.get("id") or "",
        "old": {
            "verdict": row.get("qcVerdict") or "",
            "comment": row.get("qcComment") or "",
            "time": row.get("qcTime") or "",
            "stage": row.get("qcStage") or "",
            "reworkRound": row.get("reworkRound") or "",
        },
        "new": {
            "verdict": "" if verdict is None else str(verdict or ""),
            "comment": "" if comment is None else str(comment or ""),
            "time": str(timestamp or ""),
        },
        **(extra or {}),
    }


def _append_qc_change_log(existing, entry):
    """Append a compact JSON-line entry, preserving any legacy text already in the cell."""
    existing = str(existing or "").strip()
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    return f"{existing}\n{line}" if existing else line


def _sheet_id_from_workbook_info(item):
    return item.get("sheet_id") or item.get("sheetId") or item.get("id") or ""


def _sheet_title_from_workbook_info(item):
    return item.get("sheet_name") or item.get("title") or item.get("sheetName") or ""


def _workbook_sheets(source):
    result = run_lark(["sheets", "+workbook-info", "--url", source["url"]])
    return list_from_payload(result, "sheets")


def ensure_qc_change_log_sheet(source):
    """Ensure the option-level QC change log sheet exists and has the expected header."""
    if source_type(source) == "base":
        return ""
    sheets = _workbook_sheets(source)
    log_sheet = next((item for item in sheets if _sheet_title_from_workbook_info(item) == QC_CHANGE_LOG_SHEET_NAME), None)
    sheet_id = _sheet_id_from_workbook_info(log_sheet or {})
    if not sheet_id:
        created = run_lark([
            "sheets",
            "+sheet-create",
            "--url",
            source["url"],
            "--title",
            QC_CHANGE_LOG_SHEET_NAME,
            "--row-count",
            "200",
            "--col-count",
            str(len(QC_CHANGE_LOG_HEADERS)),
        ])
        data = created.get("data") or {}
        sheet_id = data.get("sheet_id") or data.get("sheetId") or ""
        if not sheet_id:
            sheets = _workbook_sheets(source)
            log_sheet = next((item for item in sheets if _sheet_title_from_workbook_info(item) == QC_CHANGE_LOG_SHEET_NAME), None)
            sheet_id = _sheet_id_from_workbook_info(log_sheet or {})
    if not sheet_id:
        raise ValueError("无法创建或定位质检变更日志子表")
    run_lark([
        "sheets",
        "+csv-put",
        "--url",
        source["url"],
        "--sheet-id",
        sheet_id,
        "--start-cell",
        "A1",
        "--csv",
        csv_text([QC_CHANGE_LOG_HEADERS]),
    ])
    return sheet_id


def append_qc_change_log_rows(source, rows):
    """Append option-level change rows to the QC change log sheet."""
    if source_type(source) == "base" or not rows:
        return 0
    sheet_id = ensure_qc_change_log_sheet(source)
    read_result = run_lark([
        "sheets",
        "+csv-get",
        "--url",
        source["url"],
        "--sheet-id",
        sheet_id,
        "--range",
        "A1:L2000",
        "--rows-json",
    ])
    existing = ((read_result.get("data") or {}).get("rows") or [])
    non_empty = []
    for item in existing:
        values = item.get("values") or {}
        if any(str(values.get(col, "")).strip() for col in "ABCDEFGHIJKL"):
            non_empty.append(item)
    start_row = max([int(item.get("row_number") or 0) for item in non_empty] or [1]) + 1
    values = [[str(row.get(header, "") or "") for header in QC_CHANGE_LOG_HEADERS] for row in rows]
    run_lark([
        "sheets",
        "+csv-put",
        "--url",
        source["url"],
        "--sheet-id",
        sheet_id,
        "--start-cell",
        f"A{start_row}",
        "--csv",
        csv_text(values),
    ])
    return len(values)


def option_change_row(timestamp, operator, action, row, item_name, column, old_value, new_value, source_name, remark=""):
    return {
        "操作时间": str(timestamp or int(time.time() * 1000)),
        "操作人": str(operator or "").strip(),
        "操作类型": action,
        "prompt_id": row.get("promptId") or row.get("id") or "",
        "原始行号": row.get("excelRow") or row.get("recordId") or "",
        "评测人": row.get("reviewer") or "",
        "变更项": item_name,
        "字段列": column,
        "原值": str(old_value or ""),
        "新值": str(new_value or ""),
        "备注": remark,
        "来源": source_name,
    }


def _qc_config_from_query(query):
    cfg = dict(_QC_CONFIG)
    for key in ("T", "R1", "R2"):
        raw = (query.get(key) or query.get(key.lower()) or [None])[0]
        if raw not in (None, ""):
            cfg[key] = float(raw)
    return qc_sampling._normalize_pool_cfg(cfg)


def update_qc_config(payload):
    cfg = dict(_QC_CONFIG)
    for key in ("T", "R1", "R2"):
        if key in payload and payload[key] not in (None, ""):
            cfg[key] = float(payload[key])
        lower = key.lower()
        if lower in payload and payload[lower] not in (None, ""):
            cfg[key] = float(payload[lower])
    cfg = qc_sampling._normalize_pool_cfg(cfg)
    _QC_CONFIG.clear()
    _QC_CONFIG.update(cfg)
    return dict(_QC_CONFIG)


def _ab_eval_rows_payload(source):
    """读取 A/B 行（带质检字段），自动补质检列。"""
    headers, mapping = ensure_ab_qc_columns(source)
    _, _, data_start_row, _ = _ab_eval_context(source)
    return headers, mapping, read_ab_eval_rows(source, headers, mapping, data_start_row)["rows"]


def _cached_ab_rows_payload(source):
    """优先返回 /api/rows 已加载的缓存；没有缓存时才读全表。"""
    source = source or active_source()
    source = normalize_source(source)
    candidates = [source]
    if source_type(source) == "sheet" and source.get("rowCount"):
        candidates.append({**source, "rowCount": DEFAULT_ROW_COUNT})
    if source_type(source) == "sheet":
        active = normalize_source(active_source())
        if active.get("url") == source.get("url") and active.get("sheetId") == source.get("sheetId"):
            candidates.append(active)
    with _CACHE_LOCK:
        for candidate in candidates:
            cached = _ROWS_CACHE.get(cache_source_key(candidate))
            payload = cached.get("payload") if cached else None
            if isinstance(payload, dict) and payload.get("columnMapping", {}).get("mode") == "ab-eval":
                return payload.get("columnMapping", {}), payload.get("rows") or []
    headers, mapping, rows = _ab_eval_rows_payload(source)
    with _CACHE_LOCK:
        _ROWS_CACHE[cache_source_key(source)] = {
            "expires_at": time.time() + ROWS_CACHE_TTL_SECONDS,
            "payload": {"rows": rows, "columnMapping": mapping},
        }
    return mapping, rows


def _set_cached_ab_rows_payload(source, mapping, rows):
    with _CACHE_LOCK:
        _ROWS_CACHE[cache_source_key(source)] = {
            "expires_at": time.time() + ROWS_CACHE_TTL_SECONDS,
            "payload": {"rows": rows, "columnMapping": mapping},
        }


def _round_int(value):
    try:
        return int(float(cell_to_text(value).strip() or 0))
    except (TypeError, ValueError):
        return 0


def _group_by_reviewer(rows):
    """按评测人分组；若有返工轮次，仅评估该人的最新轮次批。"""
    by_round = {}
    for row in rows:
        reviewer = (row.get("reviewer") or "").strip() or "未知"
        round_no = _round_int(row.get("reworkRound"))
        by_round.setdefault(reviewer, {}).setdefault(round_no, []).append(row)

    groups = {}
    for reviewer, rounds in by_round.items():
        current_round = max(rounds) if rounds else 0
        bucket = groups.setdefault(reviewer, {"checked": [], "unchecked": [], "uncheckedIds": [], "total": 0, "reworkRound": current_round})
        for row in rounds[current_round]:
            prompt_id = row.get("promptId") or row.get("id")
            item = {
                "promptId": prompt_id,
                "excelRow": row.get("excelRow"),
                "recordId": row.get("recordId"),
                "reviewer": reviewer,
                "reworkRound": current_round,
            }
            bucket["total"] += 1
            if row.get("qcChecked"):
                bucket["checked"].append({
                    **item,
                    "verdict": row.get("qcVerdict"),
                    "qcTime": row.get("qcTime"),
                })
            else:
                bucket["unchecked"].append(item)
                bucket["uncheckedIds"].append(prompt_id)
    return groups


def _legacy_group_by_reviewer(rows):
    groups = {}
    for row in rows:
        reviewer = (row.get("reviewer") or "").strip() or "未知"
        bucket = groups.setdefault(reviewer, {"checked": [], "uncheckedIds": [], "total": 0})
        bucket["total"] += 1
        if row.get("qcChecked"):
            bucket["checked"].append({
                "promptId": row.get("promptId") or row.get("id"),
                "verdict": row.get("qcVerdict"),
                "qcTime": row.get("qcTime"),
            })
        else:
            bucket["uncheckedIds"].append(row.get("promptId") or row.get("id"))
    return groups


def compute_qc_stats(source, cfg=None):
    """读全表 → 按评测人当前轮次分组 → evaluate_pool 判定责任池。"""
    cfg = qc_sampling._normalize_pool_cfg(cfg or _QC_CONFIG)
    _, _, rows = _ab_eval_rows_payload(source)
    return _compute_qc_stats_from_rows(rows, cfg)


def _compute_qc_stats_from_rows(rows, cfg=None):
    """基于已读取 rows 计算质检统计，避免同一次请求内重复读全表。"""
    cfg = qc_sampling._normalize_pool_cfg(cfg or _QC_CONFIG)
    groups = _group_by_reviewer(rows)
    result = qc_sampling.evaluate_pool(groups, cfg)
    _apply_qc_leases(result)
    result["params"] = result.get("config", cfg)
    return result


def _lease_key(source, prompt_id):
    return (cache_source_key(source), str(prompt_id or ""))


def _cleanup_qc_leases(source=None):
    now = time.time()
    source_key = cache_source_key(source) if source is not None else None
    with _CACHE_LOCK:
        for key, lease in list(_QC_LEASES.items()):
            if lease.get("expiresAt", 0) <= now or (source_key is not None and key[0] == source_key and lease.get("released")):
                _QC_LEASES.pop(key, None)


def _lease_prompt(source, prompt_id, owner):
    if not prompt_id or not owner:
        return
    _cleanup_qc_leases(source)
    with _CACHE_LOCK:
        _QC_LEASES[_lease_key(source, prompt_id)] = {"owner": owner, "expiresAt": time.time() + QC_LEASE_TTL_SECONDS}


def _release_lease(source, prompt_id, owner=None):
    if not prompt_id:
        return
    with _CACHE_LOCK:
        key = _lease_key(source, prompt_id)
        lease = _QC_LEASES.get(key)
        if lease and (owner is None or lease.get("owner") == owner):
            _QC_LEASES.pop(key, None)


def _apply_qc_leases(stats):
    """过滤被他人短租约占用的 nextQueue/suggestQueue，降低多人抢同一条概率。"""
    now = time.time()
    with _CACHE_LOCK:
        active = {key[1]: lease for key, lease in _QC_LEASES.items() if lease.get("expiresAt", 0) > now}
    def blocked(item):
        prompt_id = str(item.get("promptId") or item.get("id") or "") if isinstance(item, dict) else str(item or "")
        return prompt_id in active
    for annotator in stats.get("annotators") or []:
        queue = [it for it in annotator.get("suggestQueue") or [] if not blocked(it)]
        annotator["suggestQueue"] = queue
        first = queue[0] if queue else None
        annotator["nextSuggestedId"] = (first.get("promptId") or first.get("id")) if isinstance(first, dict) else first
    queue = [it for it in stats.get("nextQueue") or [] if not blocked(it)]
    stats["nextQueue"] = queue


def _annotator_state_for_row(source, reviewer, params=None):
    """写回后重算单个标注员最新状态（供 /api/qc 响应顺带返回）。基于刚写入后的最新全表。"""
    stats = compute_qc_stats(source, params)
    for annotator in stats["annotators"]:
        if annotator["reviewer"] == ((reviewer or "").strip() or "未知"):
            return annotator
    return None


def _annotator_state_for_written_row(source, row_number, cfg=None):
    _, _, rows = _ab_eval_rows_payload(source)
    stats = _compute_qc_stats_from_rows(rows, cfg or _QC_CONFIG)
    target = next((row for row in rows if str(row.get("excelRow")) == str(row_number) or str(row.get("recordId")) == str(row_number)), None)
    reviewer = ((target or {}).get("reviewer") or "").strip() or "未知"
    for annotator in stats.get("annotators") or []:
        if annotator.get("reviewer") == reviewer:
            return annotator, stats
    return None, stats


def _annotator_from_stats(stats, reviewer):
    key = (reviewer or "").strip() or "未知"
    for annotator in stats.get("annotators") or []:
        if annotator.get("reviewer") == key:
            return annotator
    return None


def _find_cached_ab_row(source, row_number):
    with _CACHE_LOCK:
        cached = _ROWS_CACHE.get(cache_source_key(source))
        payload = cached.get("payload") if cached else None
        rows = payload.get("rows") if isinstance(payload, dict) else []
        for row in rows or []:
            if str(row.get("excelRow")) == str(row_number) or str(row.get("recordId")) == str(row_number):
                return row
    return None


def _update_cached_qc_row(source, row_number, updates):
    with _CACHE_LOCK:
        cached = _ROWS_CACHE.get(cache_source_key(source))
        payload = cached.get("payload") if cached else None
        rows = payload.get("rows") if isinstance(payload, dict) else []
        for row in rows or []:
            if str(row.get("excelRow")) == str(row_number) or str(row.get("recordId")) == str(row_number):
                row.update(updates)
                cached["expires_at"] = time.time() + ROWS_CACHE_TTL_SECONDS
                return True
    return False


def _find_row_in_list(rows, row_number):
    for row in rows or []:
        if str(row.get("excelRow")) == str(row_number) or str(row.get("recordId")) == str(row_number):
            return row
    return None


def _annotator_state_from_cached_rows(source, reviewer, params=None):
    """旧 SPRT 缓存重算入口已不再用于新三阶段流程；保留占位避免外部引用报错。"""
    return None
    with _CACHE_LOCK:
        cached = _ROWS_CACHE.get(cache_source_key(source))
        payload = cached.get("payload") if cached else None
        rows = list(payload.get("rows") or []) if isinstance(payload, dict) else []
    if not rows:
        return None
    groups = _group_by_reviewer(rows)
    key = ((reviewer or "").strip() or "未知")
    bucket = groups.get(key)
    if not bucket:
        return None
    result = qc_sampling.evaluate_annotator(bucket["total"], bucket["checked"], bucket["uncheckedIds"], params or qc_sampling.SprtParams())
    result["reviewer"] = key
    return result


def _qc_person_state_for_sheet(state):
    return state if state in {qc_sampling.PASS_STATE, qc_sampling.SUSPECT, qc_sampling.REWORK, qc_sampling.PENDING} else qc_sampling.PENDING


def _validate_qc_mapping(mapping):
    required = {
        "qcVerdictColumn": "质检结论",
        "qcCommentColumn": "质检意见",
        "qcReviewerColumn": "质检人",
        "qcTimeColumn": "质检时间",
    }
    missing = [name for key, name in required.items() if not mapping.get(key)]
    if missing:
        raise ValueError("缺少质检必要列：" + "、".join(missing))


def write_qc(row_number, verdict, comment, reviewer, source=None, params=None, client_qc_time=None, client_qc_reviewer=None):
    """多人快路径：写飞书事实后增量更新缓存，用缓存 evaluate_pool，不再每次重读全表。"""
    timings = []
    t_all = time.perf_counter()

    def mark(label, started):
        timings.append((label, round((time.perf_counter() - started) * 1000, 1)))

    source = source or active_source()
    verdict = str(verdict or "").strip()
    if verdict not in (qc_sampling.PASS, qc_sampling.FAIL):
        raise ValueError(f"质检结论只能是「{qc_sampling.PASS}」或「{qc_sampling.FAIL}」")
    if not (reviewer or "").strip():
        raise ValueError("缺少质检人，请先在页面顶部填写质检人姓名")
    reviewer = str(reviewer).strip()

    t = time.perf_counter()
    cached_mapping, rows = _cached_ab_rows_payload(source)
    cached_row = _find_row_in_list(rows, row_number)
    mark("load_cached_rows", t)

    # 快路径：页面 /api/rows 已加载过 A/B mapping 时，不再每次读表头 ensure。
    if cached_mapping and cached_mapping.get("qcVerdictColumn") and cached_mapping.get("qcReviewerColumn") and cached_mapping.get("qcTimeColumn"):
        mapping = cached_mapping
        mark("ensure_columns_skipped", time.perf_counter())
    else:
        t = time.perf_counter()
        headers, mapping = ensure_ab_qc_columns(source)
        mark("ensure_columns", t)
    _validate_qc_mapping(mapping)

    if not cached_row:
        # 页面未先 /api/rows 加载或缓存过期时才走一次全表读取兜底。
        t = time.perf_counter()
        _, mapping, rows = _ab_eval_rows_payload(source)
        cached_row = _find_row_in_list(rows, row_number)
        _set_cached_ab_rows_payload(source, mapping, rows)
        mark("fallback_read_rows", t)
    writable = set(mapping.get("writableColumns", []))
    if not cached_row:
        raise ValueError(f"找不到待质检行：{row_number}")

    previous_reviewer = cached_row.get("qcReviewer") or ""
    previous_time = cached_row.get("qcTime") or ""
    previous_verdict = cached_row.get("qcVerdict") or ""
    previous_comment = cached_row.get("qcComment") or ""
    # 乐观并发：客户端看到的 qcTime/Reviewer 必须仍与服务端缓存一致。
    if str(client_qc_time or "") != str(previous_time or "") or str(client_qc_reviewer or "") != str(previous_reviewer or ""):
        raise ConflictError(f"该条已被 {previous_reviewer or '其他人'} 于 {previous_time or '刚刚'} 质检，请刷新后再操作")

    operation_time = str(int(time.time() * 1000))
    effective_qc_time = previous_time.strip() if previous_time.strip() else operation_time
    prompt_id = cached_row.get("promptId") or cached_row.get("id")
    annotator_reviewer = (cached_row.get("reviewer") or "").strip() or "未知"
    change_action = "create" if not previous_verdict and not previous_reviewer and not previous_time else "update"
    option_log_rows = []
    if str(previous_verdict or "") != str(verdict or ""):
        option_log_rows.append(option_change_row(operation_time, reviewer, change_action, cached_row, "质检结论", mapping.get("qcVerdictColumn"), previous_verdict, verdict, "qc"))
    if str(previous_comment or "") != str(comment or ""):
        option_log_rows.append(option_change_row(operation_time, reviewer, change_action, cached_row, "质检意见", mapping.get("qcCommentColumn"), previous_comment, str(comment or ""), "qc"))
    qc_change_log = _append_qc_change_log(
        cached_row.get("qcChangeLog") or "",
        _qc_change_log_entry(
            change_action,
            cached_row,
            reviewer,
            verdict=verdict,
            comment=comment,
            timestamp=operation_time,
            extra={
                "new": {
                    "verdict": verdict,
                    "comment": str(comment or ""),
                    "time": effective_qc_time,
                    "operationTime": operation_time,
                },
                "changedFields": [
                    name for name, old_value, new_value in (
                        ("verdict", previous_verdict, verdict),
                        ("comment", previous_comment, str(comment or "")),
                    ) if str(old_value or "") != str(new_value or "")
                ],
            },
        ),
    )

    plan = [
        (mapping.get("qcVerdictColumn"), verdict),
        (mapping.get("qcReviewerColumn"), reviewer),
        (mapping.get("qcTimeColumn"), effective_qc_time),
    ]
    if mapping.get("qcCommentColumn"):
        plan.append((mapping.get("qcCommentColumn"), str(comment or "")))
    if mapping.get("qcChangeLogColumn"):
        plan.append((mapping.get("qcChangeLogColumn"), qc_change_log))
    if mapping.get("qcCheckedColumn"):
        plan.append((mapping.get("qcCheckedColumn"), "1"))

    with _WRITE_LOCK:
        for column, value in plan:
            if not column or column not in writable:
                raise ValueError(f"质检列 {column} 不在可写列内或未建立")
        t = time.perf_counter()
        write_sheet_cells(source, row_number, plan)
        mark("write_verdict", t)
        if option_log_rows:
            t = time.perf_counter()
            append_qc_change_log_rows(source, option_log_rows)
            mark("write_log", t)

        # 增量更新内存缓存；后续统计直接用这份 rows。
        cached_row.update({
            "qcChecked": True,
            "qcVerdict": verdict,
            "qcComment": str(comment or ""),
            "qcReviewer": reviewer,
            "qcTime": effective_qc_time,
            "qcChangeLog": qc_change_log,
        })
        _release_lease(source, prompt_id, reviewer)
        t = time.perf_counter()
        stats = _compute_qc_stats_from_rows(rows, params or _QC_CONFIG)
        annotator = _annotator_from_stats(stats, annotator_reviewer)
        mark("compute_from_cache", t)

        qc_stage = (annotator or {}).get("currentStage") or stats.get("stage") or qc_sampling.STAGE1
        state_plan = []
        if mapping.get("qcStageColumn"):
            state_plan.append((mapping.get("qcStageColumn"), qc_stage))
        if state_plan:
            for column, _ in state_plan:
                if not column or column not in writable:
                    raise ValueError(f"质检列 {column} 不在可写列内或未建立")
            t = time.perf_counter()
            write_sheet_cells(source, row_number, state_plan)
            mark("write_state", t)
            cached_row.update({"qcStage": qc_stage})

        # 为返回的下一条建议加短租约，避免多人拿到同一条。
        next_id = (annotator or {}).get("nextSuggestedId")
        if next_id:
            _lease_prompt(source, next_id, reviewer)
        _set_cached_ab_rows_payload(source, mapping, rows)

    timings.append(("total", round((time.perf_counter() - t_all) * 1000, 1)))
    print("[qc-timing]", " ".join(f"{label}={ms}ms" for label, ms in timings), flush=True)
    return {
        "verdict": verdict,
        "qcTime": effective_qc_time,
        "qcReviewer": reviewer,
        "qcStage": qc_stage,
        "previousReviewer": previous_reviewer,
        "previousTime": previous_time,
        "annotator": annotator,
        "stats": stats,
    }


def write_qc_rework(source=None, cfg=None):
    """将 evaluate_pool 给出的可疑范围未质检条目标记为返工，递增 reworkRound 并清旧 verdict。"""
    source = source or active_source()
    headers, mapping = ensure_ab_qc_columns(source)
    _, rows = _cached_ab_rows_payload(source)
    stats = compute_qc_stats(source, cfg or _QC_CONFIG)
    items = stats.get("reworkItems") or []
    if stats.get("stage") != qc_sampling.REWORK:
        return {"count": 0, "items": [], "stats": stats, "message": "当前未进入返工阶段"}
    writable = set(mapping.get("writableColumns", []))
    count = 0
    log_rows = []
    with _WRITE_LOCK:
        for item in items:
            row_key = item.get("recordId") or item.get("excelRow")
            if not row_key:
                continue
            cached_row = _find_row_in_list(rows, row_key) or item
            current_round = _round_int(item.get("reworkRound"))
            plan = []
            if mapping.get("qcVerdictColumn"):
                plan.append((mapping.get("qcVerdictColumn"), ""))
            if mapping.get("qcReviewerColumn"):
                plan.append((mapping.get("qcReviewerColumn"), ""))
            if mapping.get("qcTimeColumn"):
                plan.append((mapping.get("qcTimeColumn"), ""))
            if mapping.get("qcStageColumn"):
                plan.append((mapping.get("qcStageColumn"), qc_sampling.STAGE1))
            if mapping.get("reworkRoundColumn"):
                plan.append((mapping.get("reworkRoundColumn"), str(current_round + 1)))
            if mapping.get("qcChangeLogColumn"):
                change_log = _append_qc_change_log(
                    cached_row.get("qcChangeLog") or "",
                    _qc_change_log_entry(
                        "rework",
                        cached_row,
                        "system",
                        verdict="",
                        comment="",
                        timestamp=str(int(time.time() * 1000)),
                        extra={"new": {"reworkRound": str(current_round + 1)}},
                    ),
                )
                plan.append((mapping.get("qcChangeLogColumn"), change_log))
            log_rows.append(option_change_row(
                int(time.time() * 1000),
                "system",
                "rework",
                cached_row,
                "返工轮次",
                mapping.get("reworkRoundColumn"),
                current_round,
                current_round + 1,
                "rework",
            ))
            if mapping.get("qcCheckedColumn"):
                plan.append((mapping.get("qcCheckedColumn"), ""))
            for column, _ in plan:
                if not column or column not in writable:
                    raise ValueError(f"质检列 {column} 不在可写列内或未建立")
            write_sheet_cells(source, row_key, plan)
            count += 1
        append_qc_change_log_rows(source, log_rows)
    clear_runtime_caches(source)
    return {"count": count, "items": items, "stats": compute_qc_stats(source, cfg or _QC_CONFIG)}


MACHINE_LABEL_MIGRATION = {
    "空间规划矛盾": "PE不合理-不合理_空间规划矛盾",
    "空间规划缺失": "PE不合理-不合理_空间规划缺失",
    "空间描述零散": "PE不合理-不合理_空间描述零散",
    "违背规律/常理": "PE违背规律/常理-违背规律/常理",
    "实体描述矛盾": "PE不合理-不合理_实体描述矛盾",
    "违背指令_动作": "PE违背指令-违背指令_动作",
    "暂无": "PE遗漏指令-指令遗漏",
    "主体趋同": "PE不合理-不合理_其他",
}


def split_problem_labels(value):
    """将标签文本拆成候选项；兼容旧数据里的顿号、逗号、分号与换行。"""
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.split(r"[；;，,、\n]+", cell_to_text(value or ""))
    labels = []
    for item in raw_items:
        label = cell_to_text(item).strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def migrate_label_list(labels):
    """按最新 PE 标签体系迁移旧机器标签，未知标签原样保留。"""
    migrated = []
    changed = False
    for label in split_problem_labels(labels):
        mapped = MACHINE_LABEL_MIGRATION.get(label, label)
        if mapped != label:
            changed = True
        if mapped and mapped not in migrated:
            migrated.append(mapped)
    return migrated, changed


def migrate_problem_label_text(value):
    """迁移 PE 问题标签列，保留 check:0/check:1 状态前缀。"""
    text = cell_to_text(value or "").strip()
    if not text:
        return "", False
    tokens = split_problem_labels(text)
    check_tokens = [item for item in tokens if item.lower().replace("：", ":") in {"check:0", "check:1"}]
    labels = [item for item in tokens if item.lower().replace("：", ":") not in {"check:0", "check:1"}]
    migrated, changed = migrate_label_list(labels)
    output = check_tokens + migrated
    new_text = "；".join(output)
    return new_text, changed or new_text != text


def migrate_machine_annotation_json(value):
    """迁移机器标注 JSON 中的 PE_label 字段，兼容历史数组/平铺对象格式。"""
    text = cell_to_text(value or "").strip()
    if not text:
        return "", False
    try:
        data = json.loads(text)
    except Exception:
        return text, False

    changed = False

    def migrate_container(container):
        nonlocal changed
        if not isinstance(container, dict):
            return
        for key in ("PE_label", "pe_label", "labels"):
            if key in container:
                migrated, did_change = migrate_label_list(container.get(key))
                if did_change or container.get(key) != migrated:
                    container[key] = migrated
                    changed = True

    if isinstance(data, dict):
        migrate_container(data)
        annotations = data.get("annotations")
        if isinstance(annotations, list):
            for annotation in annotations:
                migrate_container(annotation)
        has_machine_content = bool(data.get("PE_label")) or bool(annotations)
        if has_machine_content and "check" not in data and not data.get("checked"):
            data["check"] = 0
            changed = True
    elif isinstance(data, list):
        for item in data:
            migrate_container(item)
            if isinstance(item, dict) and (item.get("PE_label") or item.get("start") is not None) and "check" not in item and not item.get("checked"):
                item["check"] = 0
                changed = True
    else:
        return text, False

    if not changed:
        return text, False
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")), True


def migrate_machine_labels_current_source():
    """维护接口：将当前表旧机器标签迁移到最新 PE 标签体系。"""
    source = active_source()
    if source_type(source) == "base":
        raise ValueError("机器标签迁移暂仅支持飞书表格数据源")
    headers = read_headers(source)
    mapping = build_column_mapping(headers)
    row_count = int(source.get("rowCount") or DEFAULT_ROW_COUNT)
    result = run_lark(
        [
            "sheets",
            "+cells-get",
            "--url",
            source["url"],
            "--sheet-id",
            source["sheetId"],
            "--range",
            f"A3:{MAX_READ_COLUMN}{row_count}",
        ]
    )
    values = cells_get_values(result)

    label_columns = []
    for group in mapping.get("issueGroups", []):
        column = group.get("labelColumn", "")
        if column:
            label_columns.append({"column": column, "index": column_index(column), "title": group.get("labelTitle", column)})

    machine_columns = []
    for group in mapping.get("groups", []):
        column = group.get("machineAnnotationColumn", "")
        if column:
            machine_columns.append({"column": column, "index": column_index(column), "title": group.get("machineAnnotationTitle", column)})

    changed_cells = []
    for offset, cells in enumerate(values, start=3):
        padded = list(cells) + [""] * (len(headers) - len(cells))
        for column_info in label_columns:
            index = column_info["index"]
            if index is None or index >= len(padded):
                continue
            new_value, changed = migrate_problem_label_text(padded[index])
            if changed:
                changed_cells.append({**column_info, "row": offset, "value": new_value, "kind": "PE问题标签"})
        for column_info in machine_columns:
            index = column_info["index"]
            if index is None or index >= len(padded):
                continue
            new_value, changed = migrate_machine_annotation_json(padded[index])
            if changed:
                changed_cells.append({**column_info, "row": offset, "value": new_value, "kind": "机器标注记录"})

    for cell in changed_cells:
        run_lark(
            [
                "sheets",
                "+cells-set",
                "--url",
                source["url"],
                "--sheet-id",
                source["sheetId"],
                "--range",
                f"{cell['column']}{cell['row']}:{cell['column']}{cell['row']}",
                "--cells",
                json.dumps(cells_payload([[cell["value"]]]), ensure_ascii=False),
            ]
        )

    by_column = {}
    for cell in changed_cells:
        by_column[cell["column"]] = by_column.get(cell["column"], 0) + 1
    return {
        "source": source,
        "changedCells": len(changed_cells),
        "byColumn": by_column,
        "sample": changed_cells[:12],
    }


_CELL_IMAGE_CACHE = {}
_CELL_IMAGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{10,50}$")


def download_cell_image(token, spec="big"):
    """用 lark-cli 凭证下载飞书内嵌图片，返回 (字节, content_type)，结果内存缓存。"""
    if not _CELL_IMAGE_TOKEN_RE.match(token or ""):
        raise ValueError("非法的图片 token")
    cache_key = (token, spec)
    if cache_key in _CELL_IMAGE_CACHE:
        return _CELL_IMAGE_CACHE[cache_key]
    with tempfile.TemporaryDirectory(prefix="pe-cell-img-", dir=str(APP_ROOT)) as tmp_dir:
        rel_dir = os.path.relpath(tmp_dir, APP_ROOT)
        rel_output = os.path.join(rel_dir, "img")
        result = run_lark(
            [
                "drive",
                "+cover",
                "--file-token",
                token,
                "--spec",
                spec,
                "--output",
                rel_output,
                "--if-exists",
                "overwrite",
                "--format",
                "json",
            ]
        )
        data = result.get("data") if isinstance(result, dict) else {}
        output_path = (data or {}).get("output_path") or ""
        content_type = (data or {}).get("content_type") or "image/jpeg"
        if not output_path or not os.path.exists(output_path):
            raise ValueError("图片下载失败")
        payload = (Path(output_path).read_bytes(), content_type)
    if len(_CELL_IMAGE_CACHE) < 500:
        _CELL_IMAGE_CACHE[cache_key] = payload
    return payload


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        path = urlparse(self.path).path
        if path.startswith("/api/") or path in {"/", "/index.html"} or path.startswith("/assets/"):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=600")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_cached_json(self, status, payload, max_age=30):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"private, max-age={max_age}")
        super(Handler, self).end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if path == "/api/sources":
            self.send_json(200, load_source_config())
            return
        if path == "/api/label-taxonomy":
            payload, from_cache = read_label_taxonomy_cached((query.get("refresh") or [""])[0] == "1")
            self.send_cached_json(200, {"ok": True, "cached": from_cache, **payload}, max_age=300)
            return
        if path == "/api/cell-image":
            token = (query.get("token") or [""])[0]
            spec = (query.get("spec") or ["big"])[0]
            try:
                data, content_type = download_cell_image(token, spec)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                super(Handler, self).end_headers()
                self.wfile.write(data)
            except Exception as exc:
                self.send_json(502, {"error": str(exc)})
            return
        if path == "/api/qc-stats":
            try:
                source = active_source()
                cfg = _qc_config_from_query(query)
                self.send_json(200, {"ok": True, **compute_qc_stats(source, cfg)})
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": str(exc)})
            return
        if path != "/api/rows":
            return super().do_GET()
        try:
            source = active_source()
            payload, from_cache = read_rows_cached((query.get("refresh") or [""])[0] == "1")
            self.send_json(200, {"rows": payload["rows"], "columnMapping": payload["columnMapping"], "source": source["url"], "sourceInfo": source, "cached": from_cache})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in {"/api/tags", "/api/issues", "/api/annotations", "/api/ab-eval", "/api/qc", "/api/qc-config", "/api/qc-rework", "/api/sources", "/api/spatial-relations", "/api/migrate-machine-labels"}:
            self.send_json(404, {"error": "Not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/migrate-machine-labels":
                result = migrate_machine_labels_current_source()
                clear_runtime_caches()
                self.send_json(200, {"ok": True, **result})
                return
            if path == "/api/spatial-relations":
                result = write_spatial_relation_cases()
                clear_runtime_caches()
                self.send_json(200, {"ok": True, **result})
                return
            if path == "/api/sources":
                config = add_or_select_source(payload.get("url"), payload.get("activeUrl"), payload.get("removeUrl"))
                self.send_json(200, {"ok": True, **config})
                return
            if path == "/api/qc-config":
                config = update_qc_config(payload)
                self.send_json(200, {"ok": True, "config": config})
                return

            source = source_from_payload(payload)
            if path == "/api/qc-rework":
                result = write_qc_rework(source=source, cfg=payload.get("config") or _QC_CONFIG)
                self.send_json(200, {"ok": True, **result})
                return
            if source_type(source) == "base":
                row_number = payload.get("recordId") or payload.get("excelRow")
                if not row_number:
                    raise ValueError("多维表格写入缺少 recordId")
                row_number = str(row_number)
            else:
                row_number = int(payload["excelRow"])
            if path == "/api/tags":
                tags = payload.get("tags", [])
                value = write_tag(row_number, tags, source=source)
            elif path == "/api/qc":
                result = write_qc(
                    row_number,
                    payload.get("verdict", ""),
                    payload.get("comment", ""),
                    payload.get("reviewer", ""),
                    source=source,
                      params=payload.get("config") or _QC_CONFIG,
                    client_qc_time=payload.get("clientQcTime", ""),
                    client_qc_reviewer=payload.get("clientQcReviewer", ""),
                )
                self.send_json(200, {"ok": True, "excelRow": row_number, **result})
                return
            elif path == "/api/ab-eval":
                value = write_ab_eval(row_number, payload.get("column", ""), payload.get("value", ""), source=source)
            elif path == "/api/issues":
                value = write_issue(
                    row_number,
                    payload.get("labelColumn", ""),
                    payload.get("summaryColumn", ""),
                    payload.get("labels", []),
                    payload.get("summary", ""),
                    payload.get("writeLabels", True),
                    payload.get("writeSummary", True),
                    source=source,
                    image_label_column=payload.get("imageLabelColumn", ""),
                    image_labels=payload.get("imageLabels", []),
                    write_image_labels=payload.get("writeImageLabels", True),
                )
            else:
                value = write_annotations(row_number, payload.get("annotationsJson", ""), payload.get("annotationColumn") or "R", source=source)
            clear_runtime_caches(source)
            self.send_json(200, {"ok": True, "excelRow": row_number, "value": value})
        except ConflictError as exc:
            self.send_json(409, {"ok": False, "error": str(exc), "conflict": True})
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)})


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"PE Prompt reviewer is running locally at http://127.0.0.1:{PORT}/index.html")
    print(f"LAN access URL: http://{local_ip()}:{PORT}/index.html")
    config = load_source_config()
    if config["sources"]:
        print("Data source:", config["activeUrl"])
    else:
        print("Data source: 未配置，请在页面顶部粘贴飞书表格或多维表格链接")
    server.serve_forever()

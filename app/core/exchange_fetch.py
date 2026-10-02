"""Official exchange capture with complete pagination, stable IDs and raw evidence.

No Selenium/browser dependency. A capture is returned only after every province and
page is reconciled with the official totals. ``capture_evidence`` travels with the
DataFrame; callers must preserve it when saving or combining captures.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

import pandas as pd

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/137.0.0.0 Safari/537.36'
SSE_URL = 'https://query.sse.com.cn/commonSoaQuery.do'
SZSE_URL = 'https://bond.szse.cn/api/report/ShowReport/data'
BSE_URL = 'https://www.bse.cn/xyzProjectInfoController/infoResult.do?callback=p1'
BSE_DICT_URL = 'https://www.bse.cn/listedDictionary/getDic.do?callback=cb'
BSE_REFERER = 'https://www.bse.cn/xyz_issue/xyz_project_info.html'
SSE_TYPE_MAP = {'0': '小公募', '5': '小公募', '1': '私募', '6': '私募',
                '2': 'ABS', '3': '大公募', '7': '大公募', '4': '公募REITs'}
SSE_STATUS_MAP = {'0': '已申报', '1': '已受理', '2': '已反馈', '3': '已接收反馈意见',
                  '4': '通过', '5': '未通过', '8': '终止', '9': '中止',
                  '10': '已回复交易所意见', '11': '提交注册', '12': '注册生效'}
# Explicit exceptions verified in the official Guangdong page 119 on 2026-09-05.
# These historical ABS are outside the corporate-bond weekly report. New missing
# dates must be reviewed; this is intentionally not an all-ABS exception.
SZSE_VERIFIED_MISSING_DATE_IDS = frozenset({
    '00016FB29471D43FA75182CD6016A03F', 'D035F1DAF13048E79C8F7717D0909C94',
    'A9BCF9D1E7EF43D0A3471780C5DA0EC5',
})
STANDARD_COLUMNS = ['project_id', 'project_name', 'bond_type', 'amount', 'status',
                    'province', 'update_date', 'source_response_index', 'date_missing_reason']
SSE_COLUMNS = ['编号', '债券名称/公募REITs名称', '承销商/管理人', '品种',
               '拟发行金额(亿元)', '项目状态', '更新日期', '受理日期', '省份'] + STANDARD_COLUMNS
SZSE_COLUMNS = ['序号', '项目名称', '项目编号', '承销商/管理人', '债券类别',
                '申请规模(亿元)', '项目进度', '受理日期', '更新日期', '省份'] + STANDARD_COLUMNS
BSE_COLUMNS = ['序号', '项目名称', '承销商', '发行人省份', '债券类型',
               '计划发行金额(亿元)', '办理状态', '更新日期', '受理日期', '省份',
               'type_code', 'status_code'] + STANDARD_COLUMNS


class ExchangeFetchError(RuntimeError):
    """An official capture cannot be certified complete; never means zero projects."""


def _now():
    return datetime.now().astimezone().isoformat()


def to_short_province(value):
    return re.sub(r'(壮族自治区|回族自治区|维吾尔自治区|特别行政区|自治区|省|市)$', '', str(value).strip())


def _text(value):
    return '' if value is None else str(value).strip()


def strip_html(value):
    return html.unescape(re.sub(r'<[^>]+>', '', _text(value))).strip()


def _require(obj, fields, context):
    if not isinstance(obj, dict):
        raise ExchangeFetchError(f'{context}: 应为对象，实际为 {type(obj).__name__}')
    missing = [f for f in fields if f not in obj]
    if missing:
        raise ExchangeFetchError(f'{context}: 官方响应缺少字段 {missing}')


def _integer(value, label, minimum=0):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ExchangeFetchError(f'{label}: 非有效整数 {value!r}')
    n = int(value)
    if n < minimum:
        raise ExchangeFetchError(f'{label}: 小于 {minimum}')
    return n


def _decode(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        # Validate the whole JSONP envelope; HTML error pages are not an empty list.
        m = re.fullmatch(r'\s*[\w.$]+\s*\((.*)\)\s*;?\s*', text, flags=re.S)
        if not m:
            raise ExchangeFetchError('官方响应不是有效 JSON/JSONP')
        try:
            return json.loads(m.group(1))
        except ValueError as exc:
            raise ExchangeFetchError('官方 JSONP 内容无法解析') from exc


def _http_request(method, url, parameters, headers, timeout):
    # requests uses its installed CA bundle on macOS Python builds that do not
    # ship a system OpenSSL certificate path. TLS verification remains enabled.
    # Import inside the request so missing dependencies produce a capture error.
    import requests
    response = requests.request(method, url, params=parameters if method == 'GET' else None,
                                data=parameters if method == 'POST' else None,
                                headers=headers, timeout=timeout)
    return {'status_code': response.status_code, 'text': response.content.decode('utf-8'),
            'url': response.url}


def _parameter_mapping(parameters):
    if isinstance(parameters, dict):
        return dict(parameters)
    result = {}
    for key, value in parameters:
        if key not in result:
            result[key] = value
        elif isinstance(result[key], list):
            result[key].append(value)
        else:
            result[key] = [result[key], value]
    return result


class _Capture:
    def __init__(self, source, provinces, logger=None, http_client=None):
        self.log = logger or logging.getLogger('bond')
        self.client = http_client or _http_request
        self.evidence = {'schema_version': 1, 'source': source, 'status': 'capturing',
                         'scope': [to_short_province(p) for p in provinces],
                         'started_at': _now(), 'complete': False, 'provinces': [], 'responses': []}
        if not provinces or len(self.evidence['scope']) != len(set(self.evidence['scope'])):
            raise ExchangeFetchError('抓取省份必须非空且不能重复')

    def request(self, method, url, parameters, referer, kind='page'):
        headers = {'User-Agent': UA, 'Referer': referer}
        if method == 'POST':
            headers.update({'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest'})
        captured = _now()
        try:
            response = self.client(method, url, parameters, headers, 35)
            if response['status_code'] != 200:
                raise ExchangeFetchError(f'HTTP {response["status_code"]}')
            raw = response['text']
            if not isinstance(raw, str):
                raise ExchangeFetchError('响应正文不是文本')
        except Exception as exc:
            raise ExchangeFetchError(f'{self.evidence["source"]} {kind} 请求失败: {url}: {exc}') from exc
        index = len(self.evidence['responses'])
        self.evidence['responses'].append({
            'url': url, 'final_url': response.get('url', url), 'method': method,
            'parameters': _parameter_mapping(parameters), 'captured_at': captured, 'http_status': 200, 'kind': kind,
            'response_text': raw, 'response_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest()})
        return _decode(raw), index

    def finish(self, rows, columns):
        keys = [(r['province'], r['project_id']) for r in rows]
        self.evidence['identity_conflicts'] = _identity_conflicts(rows, self.evidence['source'], self.evidence)
        validate_identity(rows, self.evidence['source'], self.evidence)
        official_total = sum(p['official_total'] for p in self.evidence['provinces'])
        if len(rows) != official_total:
            raise ExchangeFetchError('全省累计数量与官方总数不一致')
        self.evidence.update(status='verified', complete=True, finished_at=_now(),
                             fetched_count=len(rows), row_count=len(rows), official_total=official_total)
        df = pd.DataFrame(rows, columns=columns)
        df.attrs['capture_evidence'] = self.evidence
        self.log.info('%s 抓取及分页对账完成：%s 条', self.evidence['source'], len(df))
        return df


def _validate_page(meta, items, requested, names, initial, source, zero_based=False):
    """Validate exact page sizes/counts, including the tail; no default-to-empty."""
    _require(meta, names.values(), source)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ExchangeFetchError(f'{source}: 项目列表缺失或结构异常')
    total = _integer(meta[names['total']], f'{source} 总数')
    count = _integer(meta[names['count']], f'{source} 页数')
    size = _integer(meta[names['size']], f'{source} 每页条数', 1)
    page = _integer(meta[names['page']], f'{source} 页码')
    expected_count = math.ceil(total / size)
    if count not in ({0, 1} if total == 0 else {expected_count}):
        raise ExchangeFetchError(f'{source}: 官方总数/页数/每页条数不一致')
    if page != requested:
        raise ExchangeFetchError(f'{source}: 请求第 {requested} 页，返回第 {page} 页')
    signature = (total, count, size)
    if initial is not None and signature != initial:
        raise ExchangeFetchError(f'{source}: 翻页期间官方总数/页数/页容量变化')
    offset = requested if zero_based else requested - 1
    expected_rows = max(0, min(size, total - offset * size))
    if len(items) != expected_rows:
        raise ExchangeFetchError(f'{source}: 第 {page} 页实际 {len(items)} 条，应为 {expected_rows} 条')
    return signature


def _page_evidence(cap, province, page, size, items, response_index, parameters):
    cap.log.info('%s %s 第%s页已接收%s条并校验', cap.evidence['source'], province, page, len(items))
    return {'province': province, 'page': page, 'page_size': size, 'actual_count': len(items),
            'response_index': response_index,
            'response_sha256': cap.evidence['responses'][response_index]['response_sha256'],
            'parameters': _parameter_mapping(parameters)}


def _date(value, label, allow_missing=False):
    if isinstance(value, dict):
        _require(value, ['time'], label)
        try:
            return datetime.fromtimestamp(float(value['time']) / 1000,
                                          timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise ExchangeFetchError(f'{label}: 官方日期无法解析') from exc
    text = _text(value)
    if not text or text == '-':
        if allow_missing:
            return ''
        raise ExchangeFetchError(f'{label}: 官方更新日期缺失')
    try:
        datetime.strptime(text[:10], '%Y-%m-%d')
    except ValueError as exc:
        raise ExchangeFetchError(f'{label}: 官方日期格式异常 {text!r}') from exc
    return text[:10]


def _standard(project_id, name, btype, amount, status, province, date, response_index, reason=''):
    fields = {'project_id': _text(project_id), 'project_name': name, 'bond_type': btype,
              'status': status, 'province': province}
    if any(not _text(v) or _text(v) == '-' for v in fields.values()):
        raise ExchangeFetchError(f'项目ID/名称/品种/状态/省份为空：{fields}')
    return dict(fields, amount=amount, update_date=date, source_response_index=response_index,
                date_missing_reason=reason)


def _sse_record(item, short, ix):
    _require(item, ['BOND_NUM', 'AUDIT_NAME', 'BOND_TYPE', 'PLAN_ISSUE_AMOUNT',
          'AUDIT_STATUS', 'PUBLISH_DATE', 'SHORT_NAME'], 'SSE 项目')
    tcode = _text(item['BOND_TYPE']); scode = _text(item['AUDIT_STATUS'])
    if tcode not in SSE_TYPE_MAP or (scode not in SSE_STATUS_MAP and _text(item.get('AUDIT_SUB_STATUS')) != '901'):
        raise ExchangeFetchError(f'SSE 未知品种/状态码：{tcode}/{scode}')
    btype = SSE_TYPE_MAP[tcode]
    status = '承销商/管理人超期中止' if _text(item.get('AUDIT_SUB_STATUS')) == '901' else SSE_STATUS_MAP[scode]
    name = _text(item['AUDIT_NAME']); date = _date(item['PUBLISH_DATE'], 'SSE')
    row = {'编号': item.get('NUM', ''), '债券名称/公募REITs名称': name,
           '承销商/管理人': _text(item['SHORT_NAME']), '品种': btype,
           '拟发行金额(亿元)': item['PLAN_ISSUE_AMOUNT'], '项目状态': status,
           '更新日期': date, '受理日期': _date(item.get('ACCEPT_DATE'), 'SSE 受理日期', True), '省份': short}
    row.update(_standard(item['BOND_NUM'], name, btype, item['PLAN_ISSUE_AMOUNT'], status, short, date, ix))
    return row


# Exact original-row signatures (only dynamic NUM omitted) from official SSE
# responses on 2026-09-05. These exceptions preserve all rows and do not resolve
# their business status. A report whose period includes them must be blocked.
SSE_VERIFIED_IDENTITY_SIGNATURES = {
    ('河南', '20793'): ['5d6de94ff14eed902d49b831b619bc0c41a22807121f92edca9d90e5ed5b7e76',
                        'd7df3abfbd69f8ebd1f5166a5da724270a3bad924bc7a98f0ad79bf5b64af9a6'],
    ('河南', '18530'): ['14bfbe61de27046a500a42935b9b1e3897c4c7a27517ca42a69d20afaa26642a'] * 2,
    ('新疆', '18208'): ['457670c17f6ae86e2262cedbe70ead3c950b0f12c8824698f86f9b4443430b74'] * 2,
}


def _raw_signature(row):
    stable = {k: v for k, v in row.items() if k != 'NUM'}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _identity_conflicts(records, source, evidence):
    groups = {}
    for position, record in enumerate(records):
        key = (_text(record.get('province') or record.get('省份')), _text(record.get('project_id')))
        groups.setdefault(key, []).append((position, record))
    conflicts = []
    for (province, pid), members in groups.items():
        if len(members) == 1:
            continue
        if source != 'SSE' or (province, pid) not in SSE_VERIFIED_IDENTITY_SIGNATURES:
            raise ExchangeFetchError(f'{source}: 未核实的同省官方项目ID重复：{province}/{pid}')
        raw_rows = []; entries = []
        for position, record in members:
            try:
                ix = int(record['source_response_index'])
                response = evidence['responses'][ix]
                original_rows = _decode(response['response_text'])['pageHelp']['data']
                matches = [r for r in original_rows if _text(r.get('NUM')) == _text(record['编号'])
                           and _text(r.get('BOND_NUM')) == pid]
                if len(matches) != 1:
                    raise ValueError('原行无法唯一定位')
                raw = matches[0]
                expected = _sse_record(raw, province, ix)
                for field in SSE_COLUMNS:
                    if field in ('更新日期', '受理日期', 'update_date'):
                        equal = _text(record.get(field))[:10] == _text(expected.get(field))[:10]
                    else:
                        equal = _text(record.get(field)) == _text(expected.get(field))
                    if not equal:
                        raise ValueError(f'原行与快照字段不一致: {field}')
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ExchangeFetchError(f'SSE 历史重复 {province}/{pid} 缺少对应原始响应: {exc}') from exc
            raw_rows.append(raw)
            entries.append({'record_position': position, 'official_row_number': _text(raw['NUM']),
                            'source_response_index': ix, 'update_date': expected['update_date'],
                            'status': expected['status'], 'raw_signature': _raw_signature(raw),
                            'raw_payload': raw})
        actual = sorted(e['raw_signature'] for e in entries)
        if actual != sorted(SSE_VERIFIED_IDENTITY_SIGNATURES[(province, pid)]):
            raise ExchangeFetchError(f'SSE 历史重复 {province}/{pid} 的精确签名变化，需重新核查')
        differences = {key: [r.get(key) for r in raw_rows] for key in set().union(*(r.keys() for r in raw_rows))
                       if len({json.dumps(r.get(key), ensure_ascii=False, sort_keys=True) for r in raw_rows}) > 1}
        conflicts.append({'source': source, 'province': province, 'project_id': pid,
                          'verified_exception': True, 'requires_period_exclusion': True,
                          'dates': sorted({e['update_date'] for e in entries}),
                          'statuses': sorted({e['status'] for e in entries}),
                          'rows': entries, 'field_differences': differences})
    return conflicts


def validate_identity(records, source, evidence):
    """Recheck IDs and the three exact official exceptions against raw responses.

    Shared by capture and snapshot replay; never deduplicates or changes IDs.
    """
    expected = _identity_conflicts(records, source, evidence)
    if expected != evidence.get('identity_conflicts', []):
        raise ExchangeFetchError(f'{source}: 身份冲突记录与原始响应/实际行不一致')
    return expected


def fetch_sse(province_list, logger=None, *, http_client=None, pause_seconds=1):
    provinces = list(province_list)
    cap = _Capture('SSE', provinces, logger, http_client)
    try:
        rows = []
        for prov in provinces:
            short = to_short_province(prov)
            page = 1; initial = None; pages = []; before = len(rows)
            while True:
                params = {'jsonCallBack': 'cb', 'isPagination': 'true', 'sqlId': 'ZQ_XMLB',
                          'pageHelp.pageSize': '200', 'pageHelp.pageNo': str(page), 'area': short}
                data, ix = cap.request('GET', SSE_URL, params, 'https://bond.sse.com.cn/bridge/information/')
                _require(data, ['pageHelp'], 'SSE')
                ph = data['pageHelp']; _require(ph, ['data'], 'SSE pageHelp')
                items = ph['data']
                initial = _validate_page(ph, items, page, {'total': 'total', 'count': 'pageCount',
                     'size': 'pageSize', 'page': 'pageNo'}, initial, f'SSE {short}')
                for item in items:
                    rows.append(_sse_record(item, short, ix))
                pages.append(_page_evidence(cap, short, page, initial[2], items, ix, params))
                if page >= max(initial[1], 1): break
                page += 1
                if pause_seconds: time.sleep(pause_seconds)
            cap.evidence['provinces'].append({'province': short, 'official_total': initial[0],
                 'total_pages': initial[1], 'actual_count': len(rows) - before, 'pages': pages})
        return cap.finish(rows, SSE_COLUMNS)
    except Exception as exc:
        cap.evidence.update(status="failed", complete=False, finished_at=_now(), error=str(exc))
        exc.capture_evidence = cap.evidence
        raise


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.urls = []
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ('href', 'a-param') and value:
                self.urls.append(value)


def szse_project_id(markup):
    parser = _LinkParser(); parser.feed(_text(markup))
    ids = set()
    for url in parser.urls:
        for key, values in parse_qs(urlsplit(url).query, keep_blank_values=True).items():
            if key.lower() == 'xmbh':
                ids.update(value for value in values if value)
    if len(ids) != 1:
        raise ExchangeFetchError(f'SZSE 官方链接必须有唯一完整 xmbh，实际 {sorted(ids)}')
    result = ids.pop()
    if result != result.strip() or any(ch.isspace() for ch in result):
        raise ExchangeFetchError('SZSE 官方项目ID含空白')
    return result


def fetch_szse(province_list, logger=None, *, http_client=None, pause_seconds=1.5):
    provinces = list(province_list)
    cap = _Capture('SZSE', provinces, logger, http_client)
    try:
        rows = []
        for prov in provinces:
            short = to_short_province(prov)
            page = 1; initial = None; pages = []; before = len(rows)
            while True:
                params = {'SHOWTYPE': 'JSON', 'CATALOGID': 'xmjdxx', 'TABKEY': 'tab1',
                          'selectdq': short, 'PAGENO': str(page)}
                payload, ix = cap.request('GET', SZSE_URL, params, 'https://bond.szse.cn/disclosure/progressinfo/index.html')
                if not isinstance(payload, list) or len(payload) != 1:
                    raise ExchangeFetchError('SZSE 响应必须含且仅含请求的 tab1')
                el = payload[0]; _require(el, ['metadata', 'data'], 'SZSE')
                meta = el['metadata']; items = el['data']
                initial = _validate_page(meta, items, page, {'total': 'recordcount', 'count': 'pagecount',
                     'size': 'pagesize', 'page': 'pageno'}, initial, f'SZSE {short}')
                for item in items:
                    _require(item, ['zqmc', 'cxsjc', 'zqlb', 'nfxje', 'xmzt', 'xmztgxrq'], 'SZSE 项目')
                    pid = szse_project_id(item['zqmc']); name = strip_html(item['zqmc'])
                    btype = strip_html(item['zqlb']); status = strip_html(item['xmzt'])
                    missing = not _text(item['xmztgxrq'])
                    allowed = short == '广东' and btype == 'ABS' and pid in SZSE_VERIFIED_MISSING_DATE_IDS
                    date = _date(item['xmztgxrq'], f'SZSE {pid}', allow_missing=allowed)
                    reason = '2026-09-05官方广东历史ABS响应已核实更新日期为空；保留原始响应，排除出公司债周报' if missing and allowed else ''
                    row = {'序号': item.get('rowid', ''), '项目名称': name, '项目编号': pid,
                           '承销商/管理人': strip_html(item['cxsjc']), '债券类别': btype,
                           '申请规模(亿元)': item['nfxje'], '项目进度': status,
                           '受理日期': _date(item.get('xmslrq'), 'SZSE 受理日期', True), '更新日期': date, '省份': short}
                    row.update(_standard(pid, name, btype, item['nfxje'], status, short, date, ix, reason))
                    rows.append(row)
                pages.append(_page_evidence(cap, short, page, initial[2], items, ix, params))
                if page >= max(initial[1], 1): break
                page += 1
                if pause_seconds: time.sleep(pause_seconds)
            cap.evidence['provinces'].append({'province': short, 'official_total': initial[0],
                 'total_pages': initial[1], 'actual_count': len(rows) - before, 'pages': pages})
        return cap.finish(rows, SZSE_COLUMNS)
    except Exception as exc:
        cap.evidence.update(status="failed", complete=False, finished_at=_now(), error=str(exc))
        exc.capture_evidence = cap.evidence
        raise


def _bse_dictionary(cap, code):
    payload, ix = cap.request('POST', BSE_DICT_URL, {'dictCode': code}, BSE_REFERER, 'dictionary')
    if not isinstance(payload, list) or not payload:
        raise ExchangeFetchError(f'BSE {code}: 官方字典为空或结构异常')
    mapping = {}
    for entry in payload:
        _require(entry, ['code', 'dkey', 'dvalue'], f'BSE {code}')
        label = _text(entry['dkey']); value = _text(entry['dvalue'])
        # Official dkey contains the Chinese label; dvalue is the numeric code.
        if entry['code'] != code or not value or not re.search(r'[\u3400-\u9fff]', label) or value in mapping:
            raise ExchangeFetchError(f'BSE {code}: 官方字典条目无效或重复')
        mapping[value] = label
    return mapping, ix


def fetch_bse(province_full_list, logger=None, *, http_client=None, pause_seconds=0.5):
    provinces = list(province_full_list)
    cap = _Capture('BSE', provinces, logger, http_client)
    try:
        types, type_ix = _bse_dictionary(cap, 'xyz_type')
        statuses, status_ix = _bse_dictionary(cap, 'xyz_project_status')
        cap.evidence['dictionaries'] = {'type': {'mapping': types, 'response_index': type_ix},
                                         'status': {'mapping': statuses, 'response_index': status_ix}}
        rows = []
        for prov in provinces:
            short = to_short_province(prov)
            page = 0; initial = None; pages = []; before = len(rows)
            while True:
                params = [('provinces[]', prov), ('xyzStatus[]', ''), ('xyzTypes[]', ''), ('xyzName', ''),
                          ('underwriter', ''), ('issueAmount', ''), ('page', str(page)),
                          ('sortfield', 'updateDate'), ('sorttype', 'desc')]
                params += [('needFields[]', f) for f in ['id', 'name', 'underwriter', 'province', 'type',
                             'issueAmount', 'issueAmountTmp', 'status', 'updateDate', 'acceptDate']]
                payload, ix = cap.request('POST', BSE_URL, params, BSE_REFERER)
                if not isinstance(payload, list) or len(payload) != 1:
                    raise ExchangeFetchError('BSE 响应结构异常')
                _require(payload[0], ['listInfo'], 'BSE')
                info = payload[0]['listInfo']
                _require(info, ['content', 'numberOfElements', 'firstPage', 'lastPage'], 'BSE listInfo')
                items = info['content']
                initial = _validate_page(info, items, page, {'total': 'totalElements', 'count': 'totalPages',
                     'size': 'size', 'page': 'number'}, initial, f'BSE {short}', zero_based=True)
                if (_integer(info['numberOfElements'], 'BSE 本页条数') != len(items)
                        or info['firstPage'] is not (page == 0)
                        or info['lastPage'] is not (page == max(initial[1] - 1, 0))):
                    raise ExchangeFetchError('BSE 首尾页/本页数量标记不一致')
                for item in items:
                    _require(item, ['id', 'name', 'underwriter', 'province', 'type', 'status', 'updateDate'], 'BSE 项目')
                    if 'issueAmount' not in item and 'issueAmountTmp' not in item:
                        raise ExchangeFetchError('BSE 项目缺少金额字段')
                    if _text(item['province']) != prov:
                        raise ExchangeFetchError(f'BSE 返回了请求省份以外的记录：{item["province"]}')
                    tcode = _text(item['type']); scode = _text(item['status'])
                    if tcode not in types or scode not in statuses:
                        raise ExchangeFetchError(f'BSE 官方字典未覆盖品种/状态码 {tcode}/{scode}')
                    btype = types[tcode]; status = statuses[scode]; name = _text(item['name'])
                    amount = item.get('issueAmountTmp')
                    if amount is None: amount = item.get('issueAmount', '')
                    date = _date(item['updateDate'], 'BSE')
                    row = {'序号': len(rows) + 1, '项目名称': name, '承销商': _text(item['underwriter']),
                           '发行人省份': prov, '债券类型': btype, '计划发行金额(亿元)': amount,
                           '办理状态': status, '更新日期': date,
                           '受理日期': _date(item.get('acceptDate'), 'BSE 受理日期', True), '省份': short,
                           'type_code': tcode, 'status_code': scode}
                    row.update(_standard(item['id'], name, btype, amount, status, short, date, ix))
                    rows.append(row)
                pages.append(_page_evidence(cap, short, page, initial[2], items, ix, params))
                if page >= max(initial[1] - 1, 0): break
                page += 1
                if pause_seconds: time.sleep(pause_seconds)
            cap.evidence['provinces'].append({'province': short, 'official_total': initial[0],
                 'total_pages': initial[1], 'actual_count': len(rows) - before, 'pages': pages})
        return cap.finish(rows, BSE_COLUMNS)
    except Exception as exc:
        cap.evidence.update(status="failed", complete=False, finished_at=_now(), error=str(exc))
        exc.capture_evidence = cap.evidence
        raise

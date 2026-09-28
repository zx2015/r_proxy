"""统一错误响应。

对应设计：docs/design/DD_WEB.md §7.4、需求：WEBUI_SPEC.md §3.6。

响应体统一为 ``{"error": {"code", "message", "details"?}}``。前端因此只需要
一条解析路径，而不必区分 FastAPI 的 ``detail`` 与我们自己的结构。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from r_proxy.storage.rules_store import RulesConflict
from r_proxy.web.config_writer import ConfigConflict, ConfigInvalid, RuleExists, issue_details

logger = logging.getLogger(__name__)

_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    503: "UNAVAILABLE",
}


def version_conflict(exc: ConfigConflict) -> HTTPException:
    """并发修改 → `409`。

    回上磁盘的当前版本，客户端可以据此判断「我该刷新了」，而不必再发一次 GET。
    """
    return HTTPException(
        409,
        detail={
            "code": "VERSION_CONFLICT",
            "message": "配置已被其他会话修改，请刷新后重试",
            "details": {"expected": exc.expected, "actual": exc.actual},
        },
    )


def revision_conflict(exc: RulesConflict) -> HTTPException:
    """规则被并发修改 → `409`。库未改动，客户端刷新后可直接重试。"""
    return HTTPException(
        409,
        detail={
            "code": "VERSION_CONFLICT",
            "message": "规则已被其他会话修改，请刷新后重试",
            "details": {"expected": exc.expected, "actual": exc.actual},
        },
    )


def rule_exists(exc: RuleExists) -> HTTPException:
    """要插入的条件已经在表里 → `409`。库未改动。

    回上已有那条的位置与出口，界面才能说「去规则页改 rules[3]」而不是让用户
    自己去表里找。规则条件与出口名都是管理员自己写的，不属于要防泄露的运行数据。
    """
    return HTTPException(
        409,
        detail={
            "code": "RULE_EXISTS",
            "message": f"规则已存在：rules[{exc.position}]，请到规则页修改那一条",
            "details": {
                "position": exc.position,
                "condition": exc.condition,
                "upstream": exc.upstream,
            },
        },
    )


def invalid_config(exc: ConfigInvalid) -> HTTPException:
    """校验失败 → `400`，并带上逐条问题。

    用 `400` 而不是 `422`：`422` 在本项目里专指 FastAPI 的模型校验（§3.6），
    而这里的失败来自配置语义层——请求本身是合法的。
    """
    return HTTPException(
        400,
        detail={
            "code": "CONFIG_INVALID",
            "message": "配置校验未通过，磁盘文件未改动",
            "details": issue_details(exc.issues),
        },
    )


def invalid_rules(exc: ConfigInvalid) -> HTTPException:
    """规则校验失败 → `400`，库未改动。

    与 `invalid_config` 分开只为了错误码与文案：界面在规则页显示的是「规则未
    改动」，说成「磁盘文件未改动」会让用户去找那个并不存在的文件。
    """
    return HTTPException(
        400,
        detail={
            "code": "INVALID_RULES",
            "message": "规则校验未通过，规则未改动",
            "details": issue_details(exc.issues),
        },
    )


def install(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(sqlite3.OperationalError, _storage_busy)
    app.add_exception_handler(Exception, _unhandled_error)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    # 处理器按异常类型注册，签名却是宽的 Exception（Starlette 的约定）。
    # 类型不符时当成内部错误，而不是让 AttributeError 冒泡成另一个 500。
    if not isinstance(exc, HTTPException):
        return await _unhandled_error(request, exc)
    detail = exc.detail
    if isinstance(detail, dict):
        body = {
            "code": detail.get("code", _CODES.get(exc.status_code, "ERROR")),
            "message": detail.get("message", ""),
        }
        if (details := detail.get("details")) is not None:
            body["details"] = details
    else:
        body = {"code": _CODES.get(exc.status_code, "ERROR"), "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": body}, headers=exc.headers)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """只回位置与原因，不回显收到的值。

    值可能来自不可信输入，原样放进响应体等于给了一个反射点。
    """
    if not isinstance(exc, RequestValidationError):
        return await _unhandled_error(request, exc)
    details = [
        {"location": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {"code": "VALIDATION_ERROR", "message": "请求参数校验失败", "details": details}
        },
    )


async def _storage_busy(request: Request, exc: Exception) -> JSONResponse:
    """SQLite 忙锁（慢磁盘、WAL checkpoint 耗时较长）→ `503`。

    只读查询的 ``busy_timeout`` 特意设得比写者短（DD_STORAGE §7）：宁可快速
    失败也不挂住线程池。快速失败之后，客户端应该知道这是「稍后重试」而不是
    「服务坏了」，`503` 比落进兜底的 `500` 更准确，前端也可以据此自动重试。
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return await _unhandled_error(request, exc)
    request_id = uuid.uuid4().hex[:16]
    logger.warning("存储查询繁忙 [%s] %s %s: %s", request_id, request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "UNAVAILABLE",
                "message": "存储暂时繁忙，请稍后重试",
                "request_id": request_id,
            }
        },
    )


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """堆栈只进日志，响应体只给 ``request_id``。

    与代理侧的失败响应策略一致（PRD §4.3.9）：路径、出口名、内部结构都不能
    出现在响应里，出问题时靠 ``request_id`` 去日志里对。
    """
    request_id = uuid.uuid4().hex[:16]
    logger.exception(
        "Web 内部错误 [%s] %s %s", request_id, request.method, request.url.path, exc_info=exc
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
                "request_id": request_id,
            }
        },
    )

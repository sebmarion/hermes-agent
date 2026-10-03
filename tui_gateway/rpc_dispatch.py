"""JSON-RPC admission and worker dispatch. Rebound onto the server namespace."""

from __future__ import annotations

from .method_ctx import bind_module


def handle_request(req: dict, *, skip_result_contract: bool = False) -> dict | None:
    from hermes_cli.backend_retirement import retirement

    with retirement.work() as admitted:
        if not admitted:
            return _err(req.get("id"), 5035, "backend is retiring; reconnect to continue")
        return _handle_admitted_request(req, skip_result_contract=skip_result_contract)


def _handle_admitted_request(req: dict, *, skip_result_contract: bool = False) -> dict | None:
    normalized = _normalize_request(req)
    if isinstance(normalized, dict):
        return normalized
    rid, method, params = normalized
    if not (fn := _methods.get(method)):
        return _err(rid, -32601, f"unknown method: {method} — the client and the Hermes backend are out of sync "
                    "(different versions); run `hermes update` and restart both")
    # Test doubles register straight into ``_methods`` without a contract; every production
    # handler comes through ``register_method`` and therefore has one.
    contract = _contracts.METHODS.get(method)
    if contract is not None:
        params, problem = _contracts.validate_params(contract, params)
        if problem is not None:
            return _err(rid, 4000, problem)
    token = _current_rpc_method.set(method)
    try:
        response = fn(rid, params)
    except ProfileUnavailableError as exc:
        return _err(rid, 4064, str(exc))
    finally:
        _current_rpc_method.reset(token)
    if contract is not None and isinstance(response, dict) and isinstance(response.get("result"), dict):
        _contracts.check_params_accepted(contract, params)
        if not skip_result_contract:
            _contracts.check_result(contract, response["result"])
    return response


def dispatch(req: dict, transport: Optional[Transport] = None) -> dict | None:
    """Route inbound RPCs — long handlers to the pool (returns None; the worker writes its own
    response via the bound transport), everything else inline (returns the response dict).
    *transport* pins every write of this request — events included — to that transport;
    omitted → the module stdio transport (``tui_gateway.entry`` behaviour)."""
    t = transport or _stdio_transport
    token = bind_transport(t)
    try:
        from tui_gateway import server_requests
        if server_requests.is_response_frame(req):
            # The renderer answering one of OUR requests (clarify, approval, …): no response frame goes back.
            if not server_requests.resolve_response(req, t) and not _relay_compute_host_response(req):
                logger.debug("dropping response for unknown server request id=%r", req.get("id"))
            return None
        normalized = _normalize_request(req)
        if isinstance(normalized, dict):
            return normalized
        _rid, method, _params = normalized
        scope = getattr(t, "auth_scope", None)
        if isinstance(scope, dict) and scope.get("kind") == "promotion-canary":
            from tui_gateway.reconnect_auth import CANARY_RPC_ALLOWLIST
            if method not in CANARY_RPC_ALLOWLIST:
                return _err(_rid, 4030, "RPC is not allowed for promotion canary")
            requested = (_params or {}).get("session_id")
            requested_operation = (_params or {}).get("operation_id")
            if requested_operation != scope.get("operation_id"):
                return _err(_rid, 4030, "promotion canary binding mismatch")
            if method == "session.resume":
                if scope.get("runtime_session_id") is not None or requested != scope.get("session_id"):
                    return _err(_rid, 4030, "stored session is not bound to promotion canary")
                if scope.get("resume_pending"):
                    return _err(_rid, 4092, "session.resume already in flight")
                scope["resume_pending"] = True
            elif requested != scope.get("runtime_session_id"):
                return _err(_rid, 4030, "runtime session is not bound to promotion canary")
        canary_resume = (
            isinstance(scope, dict) and scope.get("kind") == "promotion-canary"
            and method == "session.resume"
        )
        dispatch_req = req
        if canary_resume:
            dispatch_req = dict(req)
            dispatch_req["params"] = dict(_params)
            dispatch_req["params"].pop("operation_id", None)
        if method not in _LONG_HANDLERS:
            response = handle_request(dispatch_req, skip_result_contract=canary_resume)
            if method == "session.resume":
                _capture_canary_resume_projection(t, response)
                if isinstance(scope, dict):
                    scope["resume_pending"] = False
            return response
        from hermes_cli.backend_retirement import retirement

        # Reserve BEFORE enqueueing: a queued handler has accepted work even though no worker runs yet.
        if not retirement.acquire():
            return _err(req.get("id"), 5035, "backend is retiring; reconnect to continue")
        try:
            ctx = contextvars.copy_context()  # the pool worker must see the bound transport
            owner = normalized[2].get("owner")
            if normalized[1] in _CONNECTOR_RPC_METHODS and isinstance(owner, dict) and owner.get("type") == "session":
                ctx.run(_capture_connector_rpc_owner, normalized[2])

            def run():
                try:
                    try:
                        resp = _handle_admitted_request(dispatch_req, skip_result_contract=canary_resume)
                    except Exception as exc:
                        resp = _err(req.get("id"), -32000, f"handler error: {exc}")
                    if resp is not None:
                        if method == "session.resume":
                            _capture_canary_resume_projection(t, resp)
                        t.write(resp)
                finally:
                    if method == "session.resume" and isinstance(scope, dict):
                        scope["resume_pending"] = False
            future = _pool.submit(lambda: ctx.run(run))
        except BaseException:
            retirement.release()
            if method == "session.resume" and isinstance(scope, dict):
                scope["resume_pending"] = False
            raise
        # Also releases cancelled queued futures; the worker's own finally would never execute.
        future.add_done_callback(lambda _: retirement.release())
        return None
    finally:
        reset_transport(token)


def register(server):
    bind_module(globals(), server)

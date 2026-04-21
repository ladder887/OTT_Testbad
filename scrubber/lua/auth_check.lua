local cjson = require "cjson.safe"
local http = require "resty.http"

local function first_ip(raw)
    if not raw or raw == "" then
        return ""
    end

    local ip = raw
    local comma = string.find(raw, ",", 1, true)
    if comma then
        ip = string.sub(raw, 1, comma - 1)
    end

    ip = ip:gsub("^%s+", ""):gsub("%s+$", "")
    if string.sub(ip, 1, 7) == "::ffff:" then
        ip = string.sub(ip, 8)
    end
    return ip
end

local function reject(status_code, reason)
    ngx.status = status_code
    ngx.header.content_type = "application/json"
    ngx.say(cjson.encode({ valid = false, reason = reason }))
    return ngx.exit(status_code)
end

local uri = ngx.var.uri or ""
local is_stream_request = string.find(uri, ".m3u8", 1, true)
    or string.find(uri, ".ts", 1, true)
    or string.find(uri, ".mp4", 1, true)

if not is_stream_request then
    return
end

local edge_id = os.getenv("EDGE_ID") or "edge-local"
local verify_api_url = os.getenv("VERIFY_API_URL") or "http://scrubber-api:3001/api/playback/verify"
local token = ngx.var.arg_token
local sig = ngx.var.arg_sig
local client_ip = ""

if client_ip == "" then
    client_ip = first_ip(ngx.var.http_x_real_ip)
end
if client_ip == "" then
    client_ip = first_ip(ngx.var.http_x_forwarded_for)
end
if client_ip == "" then
    client_ip = first_ip(ngx.var.remote_addr)
end

ngx.var.client_real_ip = client_ip ~= "" and client_ip or "-"
ngx.var.edge_server = edge_id
ngx.var.token_user_id = "-"
ngx.var.token_session_id = "-"
ngx.var.token_content_id = "-"
ngx.var.token_expires = "-"
ngx.var.token_valid = "false"
ngx.var.token_edge_match = "false"
ngx.var.token_label = "normal"
ngx.var.token_run_id = "-"
ngx.var.token_scenario_id = "-"
ngx.var.token_dataset_label = "-"

if not token or token == "" or not sig or sig == "" then
    return reject(403, "missing_token_or_signature")
end

local httpc = http.new()
httpc:set_timeout(1500)

local verify_payload = {
    token = token,
    sig = sig,
    edge_id = edge_id,
    request_uri = ngx.var.request_uri,
    client_ip = client_ip,
}

local res, err = httpc:request_uri(verify_api_url, {
    method = "POST",
    body = cjson.encode(verify_payload),
    headers = {
        ["Content-Type"] = "application/json",
    },
})

if not res then
    ngx.log(ngx.ERR, "playback verify request failed: ", err)
    return reject(503, "verification_service_unavailable")
end

local body = cjson.decode(res.body or "") or {}

if res.status ~= 200 or not body.valid then
    local reason = body.reason or "verification_failed"
    if body.token_edge_match then
        ngx.var.token_edge_match = "true"
    end
    return reject(403, reason)
end

ngx.var.token_user_id = tostring(body.token_user_id or "-")
ngx.var.token_session_id = tostring(body.token_session_id or "-")
ngx.var.token_content_id = tostring(body.token_content_id or "-")
ngx.var.token_expires = tostring(body.token_expires or "-")
ngx.var.token_valid = "true"
ngx.var.token_edge_match = body.token_edge_match and "true" or "false"
ngx.var.token_label = tostring(body.token_label or scenario_label or "normal")
ngx.var.token_run_id = tostring(body.token_run_id or "-")
ngx.var.token_scenario_id = tostring(body.token_scenario_id or "-")
ngx.var.token_dataset_label = tostring(body.token_dataset_label or "-")

ngx.req.set_header("X-User-ID", ngx.var.token_user_id)
ngx.req.set_header("X-Playback-Session", ngx.var.token_session_id)
ngx.req.set_header("X-Content-ID", ngx.var.token_content_id)
ngx.req.set_header("X-Scenario-Label", ngx.var.token_label)
ngx.req.set_header("X-Run-ID", ngx.var.token_run_id)
ngx.req.set_header("X-Scenario-ID", ngx.var.token_scenario_id)
ngx.req.set_header("X-Dataset-Label", ngx.var.token_dataset_label)

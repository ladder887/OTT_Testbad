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

local function hls_path_from_uri(raw_uri)
    if not raw_uri or raw_uri == "" then
        return ""
    end

    local path = string.match(raw_uri, "^/hls/([^/%?]+)")
    return path or ""
end

local uri = ngx.var.uri or ""
local is_stream_request = string.find(uri, ".m3u8", 1, true)
    or string.find(uri, ".ts", 1, true)
    or string.find(uri, ".mp4", 1, true)

if not is_stream_request then
    return
end

local edge_id = os.getenv("EDGE_ID") or "edge-local"
local verify_api_url = os.getenv("VERIFY_API_URL") or "http://access-api:3001/api/playback/verify"
local verify_timeout_ms = tonumber(os.getenv("VERIFY_API_TIMEOUT_MS") or "5000") or 5000
local verify_cache_ttl_sec = tonumber(os.getenv("VERIFY_CACHE_TTL_SEC") or "10") or 10
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
ngx.var.token_jti = "-"
ngx.var.cdn_token_id = "-"
ngx.var.token_owner_account_id = "-"
ngx.var.token_owner_auth_session_id = "-"
ngx.var.token_playback_id = "-"
ngx.var.token_owner_device_id = "-"
ngx.var.token_content_id = "-"
ngx.var.token_issued_at = "-"
ngx.var.token_expires = "-"
ngx.var.token_ttl_sec = "0"
ngx.var.token_ttl_remaining_sec = "0"
ngx.var.token_valid = "false"
ngx.var.token_edge_match = "false"

if not token or token == "" or not sig or sig == "" then
    return reject(403, "missing_token_or_signature")
end

local verify_payload = {
    token = token,
    sig = sig,
    edge_id = edge_id,
    request_uri = ngx.var.request_uri,
    client_ip = client_ip,
}

local verify_cache = ngx.shared.token_verify_cache
local cache_key = edge_id .. "|" .. hls_path_from_uri(uri) .. "|" .. sig .. "|" .. token
local cached_body = verify_cache and verify_cache:get(cache_key)
local body = nil

if cached_body then
    body = cjson.decode(cached_body) or {}
else
    local httpc = http.new()
    httpc:set_timeout(verify_timeout_ms)

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

    body = cjson.decode(res.body or "") or {}

    if res.status ~= 200 or not body.valid then
        local reason = body.reason or "verification_failed"
        if body.token_edge_match then
            ngx.var.token_edge_match = "true"
        end
        return reject(403, reason)
    end

    if verify_cache and verify_cache_ttl_sec > 0 then
        verify_cache:set(cache_key, res.body or cjson.encode(body), verify_cache_ttl_sec)
    end
end

ngx.var.token_jti = tostring(body.token_jti or "-")
ngx.var.cdn_token_id = tostring(body.cdn_token_id or "-")
ngx.var.token_owner_account_id = tostring(body.token_owner_account_id or "-")
ngx.var.token_owner_auth_session_id = tostring(body.token_owner_auth_session_id or "-")
ngx.var.token_playback_id = tostring(body.token_playback_id or "-")
ngx.var.token_owner_device_id = tostring(body.token_owner_device_id or "-")
ngx.var.token_content_id = tostring(body.token_content_id or "-")
ngx.var.token_issued_at = tostring(body.token_issued_at or "-")
ngx.var.token_expires = tostring(body.token_expires or "-")
ngx.var.token_ttl_sec = tostring(body.token_ttl_sec or "0")
ngx.var.token_ttl_remaining_sec = tostring(body.token_ttl_remaining_sec or "0")
ngx.var.token_valid = "true"
ngx.var.token_edge_match = body.token_edge_match and "true" or "false"

ngx.req.set_header("X-User-ID", ngx.var.token_owner_account_id)
ngx.req.set_header("X-Playback-Session", ngx.var.token_playback_id)
ngx.req.set_header("X-Content-ID", ngx.var.token_content_id)
ngx.req.set_header("X-Token-Issued-At", ngx.var.token_issued_at)
ngx.req.set_header("X-Token-TTL-Sec", ngx.var.token_ttl_sec)
ngx.req.set_header("X-Token-TTL-Remaining-Sec", ngx.var.token_ttl_remaining_sec)
ngx.req.set_header("X-CDN-Token-ID", ngx.var.cdn_token_id)

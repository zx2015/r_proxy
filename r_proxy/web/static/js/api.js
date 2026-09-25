/**
 * REST 调用封装：token、错误归一、查询串构造。
 *
 * 对应设计：docs/design/DD_WEB.md §10.5，需求：WEBUI_SPEC.md §3.6、§7.1.2。
 */

const TOKEN_KEY = "r-proxy-token";

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details ?? null;
  }
}

/** sessionStorage 而非 localStorage：关标签页即失效（WEBUI_SPEC §7.1.2）。*/
export function getToken() {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch (err) {
    // 隐私模式下 sessionStorage 可能不可用。没有 token 也能用回环部署。
    return null;
  }
}

export function setToken(token) {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch (err) {
    /* 存不下就只在本次会话的内存里用，不影响功能 */
  }
}

export function hasToken() {
  return Boolean(getToken());
}

async function request(method, path, body) {
  const headers = {};
  const token = getToken();
  // 无 token 时**不发**这个头：回环 + 未配置 token 的部署本来就不需要它，
  // 发一个空的反而会被当成一次认证失败计入限流。
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "omit",
      cache: "no-store",
    });
  } catch (err) {
    throw new ApiError(0, "NETWORK_ERROR", "无法连接到管理接口，服务可能已停止");
  }

  if (response.status === 204) return null;

  const payload = await readJson(response);
  if (!response.ok) {
    const error = payload && payload.error ? payload.error : {};
    throw new ApiError(
      response.status,
      error.code ?? `HTTP_${response.status}`,
      error.message ?? `请求失败（HTTP ${response.status}）`,
      error.details,
    );
  }
  return payload;
}

async function readJson(response) {
  const body = await response.text();
  if (!body) return null;
  try {
    return JSON.parse(body);
  } catch (err) {
    return null;
  }
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  status: () => request("GET", "/api/status"),
  health: () => request("GET", "/api/health"),
  resetCircuit: (name) => request("POST", `/api/health/${encodeURIComponent(name)}/reset`),
  logs: (params) => request("GET", `/api/logs${query(params)}`),
  switches: (params) => request("GET", `/api/logs/switches${query(params)}`),
  hostTraffic: (params) => request("GET", `/api/traffic/hosts${query(params)}`),

  upstreams: () => request("GET", "/api/upstreams"),
  createUpstream: (body) => request("POST", "/api/upstreams", body),
  updateUpstream: (name, body) =>
    request("PUT", `/api/upstreams/${encodeURIComponent(name)}`, body),
  deleteUpstream: (name, version) =>
    request(
      "DELETE",
      `/api/upstreams/${encodeURIComponent(name)}${query({ config_version: version })}`,
    ),
  setPriorities: (body) => request("PUT", "/api/upstreams/priorities", body),
  probeUpstream: (name) => request("POST", `/api/upstreams/${encodeURIComponent(name)}/test`),

  sticky: (params) => request("GET", `/api/sticky${query(params)}`),
  bindSticky: (host, upstream) =>
    request("PUT", `/api/sticky/${encodeURIComponent(host)}`, { upstream }),
  clearSticky: (host) => request("DELETE", `/api/sticky/${encodeURIComponent(host)}`),
  promoteSticky: (host, body) =>
    request("POST", `/api/sticky/${encodeURIComponent(host)}/promote`, body),
  clearStickyBatch: (body) => request("DELETE", "/api/sticky", body),
  routeBlocks: (params) => request("GET", `/api/route-blocks${query(params)}`),
  clearRouteBlock: (host, upstream) =>
    request(
      "DELETE",
      `/api/route-blocks/${encodeURIComponent(host)}/${encodeURIComponent(upstream)}`,
    ),

  rules: () => request("GET", "/api/rules"),
  saveRules: (body) => request("PUT", "/api/rules", body),
  validateRules: (body) => request("POST", "/api/rules/validate", body),
  routeTest: (url) => request("POST", "/api/route-test", { url }),

  settings: () => request("GET", "/api/settings"),
  updateSettings: (body) => request("PUT", "/api/settings", body),
  reload: () => request("POST", "/api/reload"),
  backups: () => request("GET", "/api/config/backups"),
  restore: (body) => request("POST", "/api/config/restore", body),
  audit: (params) => request("GET", `/api/audit${query(params)}`),
};

/** 显示格式：时长、字节、时间戳、成功率。纯函数，无 DOM。*/

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${total % 60} 秒`;
  return `${total} 秒`;
}

export function ago(seconds) {
  if (seconds === null || seconds === undefined) return "从未";
  if (seconds < 1) return "刚刚";
  return `${duration(seconds)}前`;
}

export function bytes(value) {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? size : size.toFixed(1)} ${units[unit]}`;
}

/** 服务端的 created_at 是 Unix 秒。*/
export function timestamp(unixSeconds) {
  if (!unixSeconds) return "—";
  const date = new Date(unixSeconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

export function percent(rate) {
  if (rate === null || rate === undefined) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

export function millis(value) {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value)} ms`;
}

/** 熔断状态的中文与配色档位。*/
export function circuit(state) {
  switch (state) {
    case "closed":
      return { label: "正常", kind: "ok" };
    case "half_open":
      return { label: "半开", kind: "warn" };
    case "open":
      return { label: "熔断", kind: "bad" };
    default:
      return { label: state ?? "未知", kind: "muted" };
  }
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

export const formatNumber = (value: number) => new Intl.NumberFormat("en-US").format(value);

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 1) return "< 1s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
}

export function formatTimestamp(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "2-digit",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(epochSeconds * 1000));
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${formatNumber(count)} ${count === 1 ? one : many}`;
}

/** Turn the cleaner's classification codes into readable labels. */
export function humanize(code: string): string {
  const known: Record<string, string> = {
    BANNER: "Titles & banners",
    FOOTER: "Footers & notes",
    SUBTOTAL: "Subtotals",
    GRAND_TOTAL: "Grand totals",
    UNVERIFIED_TOTAL: "Unverified totals",
    REPEATED_HEADER: "Repeated headers",
    EXCEL_ERROR: "Excel error rows",
  };
  if (known[code]) return known[code];
  const text = code.replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function parseFlags(flag: string | null | undefined): { level: "CHECK" | "INFO"; text: string }[] {
  if (!flag || flag === "NA") return [];
  return flag
    .split(" | ")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const match = /^(?:(.+?): )?(CHECK|INFO): (.*)$/.exec(part);
      if (!match) return { level: "INFO" as const, text: part };
      const prefix = match[1] ? `${match[1]}: ` : "";
      return { level: match[2] as "CHECK" | "INFO", text: prefix + match[3] };
    });
}

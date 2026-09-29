/** Exavalu Data Cleaning Studio design tokens.
 *
 * Brand red is reserved for the primary action, the active step and focus; everything
 * else sits on a neutral slate scale so status colours stay meaningful.
 * Spacing uses Tailwind's 4px scale restricted in practice to 1,2,3,4,6,8,10,12
 * (4/8/12/16/24/32/40/48 px).
 */
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#FDF3F4",
          100: "#FBE4E7",
          200: "#F5C3CA",
          300: "#EC95A2",
          500: "#D72A44",
          600: "#C8102E",
          700: "#A30D25",
          800: "#7F0A1D",
        },
        ink: {
          900: "#0F172A",
          800: "#1E293B",
          700: "#334155",
          600: "#475569",
          500: "#64748B",
          400: "#94A3B8",
          300: "#CBD5E1",
          200: "#E2E8F0",
          100: "#F1F5F9",
          50: "#F8FAFC",
        },
        nav: { DEFAULT: "#111827", raised: "#1B2332", line: "#273142" },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "Consolas", "monospace"],
      },
      fontSize: {
        // Type scale: caption / table / body / card heading / section / page title
        caption: ["12px", { lineHeight: "16px" }],
        table: ["13px", { lineHeight: "20px" }],
        body: ["14px", { lineHeight: "20px" }],
        card: ["15px", { lineHeight: "22px", fontWeight: "600" }],
        section: ["17px", { lineHeight: "24px", fontWeight: "600" }],
        page: ["24px", { lineHeight: "32px", fontWeight: "650" }],
      },
      borderRadius: { DEFAULT: "6px", md: "6px", lg: "8px", xl: "10px" },
      boxShadow: {
        card: "0 1px 2px rgba(15, 23, 42, 0.04)",
        pop: "0 8px 24px rgba(15, 23, 42, 0.12), 0 2px 6px rgba(15, 23, 42, 0.06)",
        focus: "0 0 0 3px rgba(200, 16, 46, 0.25)",
      },
      keyframes: {
        "fade-in": { from: { opacity: 0, transform: "translateY(4px)" }, to: { opacity: 1, transform: "none" } },
        "scale-in": { from: { opacity: 0, transform: "scale(0.96)" }, to: { opacity: 1, transform: "none" } },
        shimmer: { "100%": { transform: "translateX(100%)" } },
        "progress-stripes": { from: { backgroundPosition: "24px 0" }, to: { backgroundPosition: "0 0" } },
        "draw-check": { from: { strokeDashoffset: 24 }, to: { strokeDashoffset: 0 } },
        "slide-in-right": { from: { transform: "translateX(16px)", opacity: 0 }, to: { transform: "none", opacity: 1 } },
        indeterminate: { "0%": { left: "-35%" }, "100%": { left: "100%" } },
      },
      animation: {
        "fade-in": "fade-in 200ms ease-out both",
        "scale-in": "scale-in 180ms ease-out both",
        shimmer: "shimmer 1.4s infinite",
        stripes: "progress-stripes 1s linear infinite",
        "draw-check": "draw-check 400ms ease-out 100ms both",
        "slide-in-right": "slide-in-right 200ms ease-out both",
        indeterminate: "indeterminate 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
